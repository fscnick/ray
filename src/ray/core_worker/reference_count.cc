// Copyright 2017 The Ray Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//  http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include "ray/core_worker/reference_count.h"

#include <memory>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

#define PRINT_REF_COUNT(it) \
  RAY_LOG(DEBUG) << "REF " << it->first << ": " << it->second.DebugString();

namespace ray {
namespace core {

size_t ReferenceCounter::Size() const {
  absl::MutexLock lock(&mutex_);
  return object_id_refs_.size();
}

bool ReferenceCounter::OwnedByUs(const ObjectID &object_id) const {
  absl::MutexLock lock(&mutex_);
  auto it = object_id_refs_.find(object_id);
  if (it != object_id_refs_.end()) {
    return it->second.owned_by_us;
  }
  return false;
}

void ReferenceCounter::DrainAndShutdown(std::function<void()> shutdown) {
  absl::MutexLock lock(&mutex_);
  if (object_id_refs_.empty()) {
    shutdown();
  } else {
    RAY_LOG(WARNING)
        << "This worker is still managing " << object_id_refs_.size()
        << " objects, waiting for them to go out of scope before shutting down.";
    shutdown_hook_ = std::move(shutdown);
  }
}

void ReferenceCounter::ShutdownIfNeeded() {
  if (shutdown_hook_ && object_id_refs_.empty()) {
    RAY_LOG(WARNING)
        << "All object references have gone out of scope, shutting down worker.";
    shutdown_hook_();
  }
}

ReferenceCounter::ReferenceTable ReferenceCounter::ReferenceTableFromProto(
    const ReferenceTableProto &proto) {
  ReferenceTable refs;
  refs.reserve(proto.size());
  for (const auto &ref : proto) {
    refs.emplace(ObjectID::FromBinary(ref.reference().object_id()),
                 Reference::FromProto(ref));
  }
  return refs;
}

void ReferenceCounter::ReferenceTableToProto(ReferenceProtoTable &table,
                                             ReferenceTableProto *proto) {
  for (auto &[id, ref] : table) {
    auto *proto_ref = proto->Add();
    *proto_ref = std::move(ref);
    proto_ref->mutable_reference()->set_object_id(id.Binary());
  }
}

bool ReferenceCounter::AddBorrowedObject(const ObjectID &object_id,
                                         const ObjectID &outer_id,
                                         const rpc::Address &owner_address,
                                         bool foreign_owner_already_monitoring) {
  absl::MutexLock lock(&mutex_);
  return AddBorrowedObjectInternal(
      object_id, outer_id, owner_address, foreign_owner_already_monitoring);
}

bool ReferenceCounter::AddBorrowedObjectInternal(const ObjectID &object_id,
                                                 const ObjectID &outer_id,
                                                 const rpc::Address &owner_address,
                                                 bool foreign_owner_already_monitoring) {
  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    it = object_id_refs_.emplace(object_id, Reference()).first;
  }

  RAY_LOG(DEBUG) << "Adding borrowed object " << object_id;
  it->second.owner_address = owner_address;
  it->second.foreign_owner_already_monitoring |= foreign_owner_already_monitoring;

  if (!outer_id.IsNil()) {
    auto outer_it = object_id_refs_.find(outer_id);
    if (outer_it != object_id_refs_.end() && !outer_it->second.owned_by_us) {
      RAY_LOG(DEBUG) << "Setting borrowed inner ID " << object_id
                     << " contained_in_borrowed: " << outer_id;
      RAY_CHECK_NE(object_id, outer_id);
      it->second.mutable_nested()->contained_in_borrowed_ids.insert(outer_id);
      outer_it->second.mutable_nested()->contains.insert(object_id);
      // The inner object ref is in use. We must report our ref to the object's
      // owner.
      if (it->second.RefCount() > 0) {
        SetNestedRefInUseRecursive(it);
      }
    }
  }

  if (it->second.RefCount() == 0) {
    DeleteReferenceInternal(it, nullptr);
  }
  return true;
}

void ReferenceCounter::AddObjectRefStats(
    const absl::flat_hash_map<ObjectID, std::pair<int64_t, std::string>> &pinned_objects,
    rpc::CoreWorkerStats *stats,
    const int64_t limit) const {
  absl::MutexLock lock(&mutex_);
  auto total = object_id_refs_.size();
  auto count = 0;

  for (const auto &ref : object_id_refs_) {
    if (limit != -1 && count >= limit) {
      break;
    }
    count += 1;

    auto ref_proto = stats->add_object_refs();
    ref_proto->set_object_id(ref.first.Binary());
    ref_proto->set_call_site(ref.second.call_site);
    ref_proto->set_object_size(ref.second.object_size);
    ref_proto->set_local_ref_count(ref.second.local_ref_count);
    ref_proto->set_submitted_task_ref_count(ref.second.submitted_task_ref_count);
    auto it = pinned_objects.find(ref.first);
    if (it != pinned_objects.end()) {
      ref_proto->set_pinned_in_memory(true);
      // If some info isn't available, fallback to getting it from the pinned info.
      if (ref.second.object_size <= 0) {
        ref_proto->set_object_size(it->second.first);
      }
      if (ref.second.call_site.empty()) {
        ref_proto->set_call_site(it->second.second);
      }
    }
    for (const auto &obj_id : ref.second.nested().contained_in_owned) {
      ref_proto->add_contained_in_owned(obj_id.Binary());
    }

    if (ref.second.owned_by_us && !ref.second.pending_creation) {
      // For finished tasks only, we set the status here instead of in the
      // TaskManager in case the task spec has already been GCed.
      ref_proto->set_task_status(rpc::TaskStatus::FINISHED);
    }
  }
  // Also include any unreferenced objects that are pinned in memory.
  for (const auto &entry : pinned_objects) {
    if (object_id_refs_.find(entry.first) == object_id_refs_.end()) {
      if (limit != -1 && count >= limit) {
        break;
      }
      count += 1;
      total += 1;

      auto ref_proto = stats->add_object_refs();
      ref_proto->set_object_id(entry.first.Binary());
      ref_proto->set_object_size(entry.second.first);
      ref_proto->set_call_site(entry.second.second);
      ref_proto->set_pinned_in_memory(true);
    }
  }

  stats->set_objects_total(total);
}

void ReferenceCounter::AddOwnedObject(const ObjectID &object_id,
                                      const std::vector<ObjectID> &inner_ids,
                                      const rpc::Address &owner_address,
                                      const std::string &call_site,
                                      const int64_t object_size,
                                      bool is_reconstructable,
                                      bool add_local_ref,
                                      const std::optional<NodeID> &pinned_at_raylet_id,
                                      rpc::TensorTransport tensor_transport) {
  absl::MutexLock lock(&mutex_);
  RAY_CHECK(AddOwnedObjectInternal(object_id,
                                   inner_ids,
                                   owner_address,
                                   call_site,
                                   object_size,
                                   is_reconstructable,
                                   add_local_ref,
                                   pinned_at_raylet_id,
                                   tensor_transport))
      << "Tried to create an owned object that already exists: " << object_id;
}

void ReferenceCounter::AddDynamicReturn(const ObjectID &object_id,
                                        const ObjectID &generator_id) {
  absl::MutexLock lock(&mutex_);
  auto outer_it = object_id_refs_.find(generator_id);
  if (outer_it == object_id_refs_.end()) {
    // Outer object already went out of scope. Either:
    // 1. The inner object was never deserialized and has already gone out of
    // scope.
    // 2. The inner object was deserialized and we already added it as a
    // dynamic return.
    // Either way, we shouldn't add the inner object to the ref count.
    return;
  }
  RAY_LOG(DEBUG) << "Adding dynamic return " << object_id
                 << " contained in generator object " << generator_id;
  RAY_CHECK(outer_it->second.owned_by_us);
  RAY_CHECK(outer_it->second.owner_address.has_value());
  rpc::Address owner_address(outer_it->second.owner_address.value());
  RAY_UNUSED(AddOwnedObjectInternal(object_id,
                                    {},
                                    owner_address,
                                    outer_it->second.call_site,
                                    /*object_size=*/-1,
                                    outer_it->second.is_reconstructable,
                                    /*add_local_ref=*/false,
                                    std::optional<NodeID>()));
  AddNestedObjectIdsInternal(generator_id, {object_id}, owner_address);
}

void ReferenceCounter::OwnDynamicStreamingTaskReturnRef(const ObjectID &object_id,
                                                        const ObjectID &generator_id) {
  absl::MutexLock lock(&mutex_);
  // NOTE: The upper layer (the layer that manages the object ref stream)
  // should make sure the generator ref is not GC'ed until the
  // stream is deleted.
  auto outer_it = object_id_refs_.find(generator_id);
  if (outer_it == object_id_refs_.end()) {
    // Generator object already went out of scope.
    // It means the generator is already GC'ed. No need to
    // update the reference.
    RAY_LOG(DEBUG)
        << "Ignore OwnDynamicStreamingTaskReturnRef. The dynamic return reference "
        << object_id << " is registered after the generator id " << generator_id
        << " went out of scope.";
    return;
  }
  RAY_LOG(DEBUG) << "Adding dynamic return " << object_id
                 << " contained in generator object " << generator_id;
  RAY_CHECK(outer_it->second.owned_by_us);
  RAY_CHECK(outer_it->second.owner_address.has_value());
  rpc::Address owner_address(outer_it->second.owner_address.value());
  // We add a local reference here. The ref removal will be handled
  // by the ObjectRefStream.
  RAY_UNUSED(AddOwnedObjectInternal(object_id,
                                    {},
                                    owner_address,
                                    outer_it->second.call_site,
                                    /*object_size=*/-1,
                                    outer_it->second.is_reconstructable,
                                    /*add_local_ref=*/true,
                                    std::optional<NodeID>()));
}

void ReferenceCounter::TryReleaseLocalRefs(const std::vector<ObjectID> &object_ids,
                                           std::vector<ObjectID> *deleted) {
  absl::MutexLock lock(&mutex_);
  for (const auto &object_id : object_ids) {
    auto it = object_id_refs_.find(object_id);
    if (it == object_id_refs_.end()) {
      // Unconsumed ref has already been released.
      continue;
    }

    if (it->second.local_ref_count == 0) {
      // Unconsumed ref has already been released.
      continue;
    }
    RemoveLocalReferenceInternal(object_id, deleted);
  }
}

bool ReferenceCounter::CheckGeneratorRefsLineageOutOfScope(
    const ObjectID &generator_id, int64_t num_objects_generated) {
  absl::MutexLock lock(&mutex_);
  if (object_id_refs_.contains(generator_id)) {
    return false;
  }

  auto task_id = generator_id.TaskId();
  for (int64_t i = 0; i < num_objects_generated; i++) {
    // Add 2 because task returns start from index 1 and the
    // first return object is the generator ID.
    const auto return_id = ObjectID::FromIndex(task_id, i + 2);
    if (object_id_refs_.contains(return_id)) {
      return false;
    }
  }

  return true;
}

bool ReferenceCounter::AddOwnedObjectInternal(
    const ObjectID &object_id,
    const std::vector<ObjectID> &inner_ids,
    const rpc::Address &owner_address,
    const std::string &call_site,
    const int64_t object_size,
    bool is_reconstructable,
    bool add_local_ref,
    const std::optional<NodeID> &pinned_at_raylet_id,
    rpc::TensorTransport tensor_transport) {
  if (object_id_refs_.contains(object_id)) {
    return false;
  }
  if (ObjectID::IsActorID(object_id)) {
    num_actors_owned_by_us_++;
  } else {
    num_objects_owned_by_us_++;
  }
  RAY_LOG(DEBUG) << "Adding owned object " << object_id;
  // If the entry doesn't exist, we initialize the direct reference count to zero
  // because this corresponds to a submitted task whose return ObjectID will be created
  // in the frontend language, incrementing the reference count.
  // TODO(swang): Objects that are not reconstructable should not increment
  // their arguments' lineage ref counts.
  auto it = object_id_refs_
                .emplace(object_id,
                         Reference(owner_address,
                                   call_site,
                                   object_size,
                                   is_reconstructable,
                                   pinned_at_raylet_id,
                                   tensor_transport))
                .first;
  if (!inner_ids.empty()) {
    // Mark that this object ID contains other inner IDs. Then, we will not GC
    // the inner objects until the outer object ID goes out of scope.
    AddNestedObjectIdsInternal(object_id, inner_ids, rpc_address_);
  }
  if (pinned_at_raylet_id.has_value()) {
    // We eagerly add the pinned location to the set of object locations.
    AddObjectLocationInternal(it, pinned_at_raylet_id.value());
  }

  reconstructable_owned_objects_.emplace_back(object_id);
  auto back_it = reconstructable_owned_objects_.end();
  back_it--;
  RAY_CHECK(reconstructable_owned_objects_index_.emplace(object_id, back_it).second);

  if (add_local_ref) {
    it->second.local_ref_count++;
  }
  PRINT_REF_COUNT(it);
  return true;
}

void ReferenceCounter::UpdateObjectSize(const ObjectID &object_id, int64_t object_size) {
  absl::MutexLock lock(&mutex_);
  auto it = object_id_refs_.find(object_id);
  if (it != object_id_refs_.end()) {
    it->second.object_size = object_size;
    PushToLocationSubscribers(it);
  }
}

void ReferenceCounter::AddLocalReference(const ObjectID &object_id,
                                         const std::string &call_site) {
  if (object_id.IsNil()) {
    return;
  }
  absl::MutexLock lock(&mutex_);
  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    // NOTE: ownership info for these objects must be added later via AddBorrowedObject.
    it = object_id_refs_.emplace(object_id, Reference(call_site, -1)).first;
  }
  bool was_in_use = it->second.RefCount() > 0;
  it->second.local_ref_count++;
  RAY_LOG(DEBUG) << "Add local reference " << object_id;
  PRINT_REF_COUNT(it);
  if (!was_in_use && it->second.RefCount() > 0) {
    SetNestedRefInUseRecursive(it);
  }
}

void ReferenceCounter::SetNestedRefInUseRecursive(ReferenceTable::iterator inner_ref_it) {
  for (const auto &contained_in_borrowed_id :
       inner_ref_it->second.nested().contained_in_borrowed_ids) {
    auto contained_in_it = object_id_refs_.find(contained_in_borrowed_id);
    RAY_CHECK(contained_in_it != object_id_refs_.end());
    if (!contained_in_it->second.has_nested_refs_to_report) {
      contained_in_it->second.has_nested_refs_to_report = true;
      SetNestedRefInUseRecursive(contained_in_it);
    }
  }
}

void ReferenceCounter::ReleaseAllLocalReferences() {
  absl::MutexLock lock(&mutex_);
  std::vector<ObjectID> refs_to_remove;
  for (auto &ref : object_id_refs_) {
    for (int i = ref.second.local_ref_count; i > 0; --i) {
      refs_to_remove.push_back(ref.first);
    }
  }
  for (const auto &object_id_to_remove : refs_to_remove) {
    RemoveLocalReferenceInternal(object_id_to_remove, nullptr);
  }
}

void ReferenceCounter::RemoveLocalReference(const ObjectID &object_id,
                                            std::vector<ObjectID> *deleted) {
  if (object_id.IsNil()) {
    return;
  }
  absl::MutexLock lock(&mutex_);
  RemoveLocalReferenceInternal(object_id, deleted);
}

void ReferenceCounter::RemoveLocalReferenceInternal(const ObjectID &object_id,
                                                    std::vector<ObjectID> *deleted) {
  RAY_CHECK(!object_id.IsNil());
  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    RAY_LOG_EVERY_MS(WARNING, 5000)
        << "Tried to decrease ref count for nonexistent object ID: " << object_id;
    return;
  }
  if (it->second.local_ref_count == 0) {
    RAY_LOG_EVERY_MS(WARNING, 5000)
        << "Tried to decrease ref count for object ID that has count 0 " << object_id
        << ". This should only happen if ray.internal.free was called earlier.";
    return;
  }
  it->second.local_ref_count--;
  RAY_LOG(DEBUG) << "Remove local reference " << object_id;
  PRINT_REF_COUNT(it);
  if (it->second.RefCount() == 0) {
    DeleteReferenceInternal(it, deleted);
  } else {
    PRINT_REF_COUNT(it);
  }
}

void ReferenceCounter::UpdateSubmittedTaskReferences(
    const std::vector<ObjectID> &return_ids,
    const std::vector<ObjectID> &argument_ids_to_add,
    const std::vector<ObjectID> &argument_ids_to_remove,
    std::vector<ObjectID> *deleted) {
  absl::MutexLock lock(&mutex_);
  for (const auto &return_id : return_ids) {
    UpdateObjectPendingCreationInternal(return_id, true);
  }
  for (const ObjectID &argument_id : argument_ids_to_add) {
    RAY_LOG(DEBUG) << "Increment ref count for submitted task argument " << argument_id;
    auto it = object_id_refs_.find(argument_id);
    if (it == object_id_refs_.end()) {
      // This happens if a large argument is transparently passed by reference
      // because we don't hold a Python reference to its ObjectID.
      it = object_id_refs_.emplace(argument_id, Reference()).first;
    }
    bool was_in_use = it->second.RefCount() > 0;
    it->second.submitted_task_ref_count++;
    // The lineage ref will get released once the task finishes and cannot be
    // retried again.
    it->second.lineage_ref_count++;
    if (!was_in_use && it->second.RefCount() > 0) {
      SetNestedRefInUseRecursive(it);
    }
  }
  // Release the submitted task ref and the lineage ref for any argument IDs
  // whose values were inlined.
  RemoveSubmittedTaskReferences(
      argument_ids_to_remove, /*release_lineage=*/true, deleted);
}

void ReferenceCounter::UpdateResubmittedTaskReferences(
    const std::vector<ObjectID> &argument_ids) {
  absl::MutexLock lock(&mutex_);
  for (const ObjectID &argument_id : argument_ids) {
    auto it = object_id_refs_.find(argument_id);
    RAY_CHECK(it != object_id_refs_.end());
    bool was_in_use = it->second.RefCount() > 0;
    it->second.submitted_task_ref_count++;
    if (!was_in_use && it->second.RefCount() > 0) {
      SetNestedRefInUseRecursive(it);
    }
  }
}

void ReferenceCounter::UpdateFinishedTaskReferences(
    const std::vector<ObjectID> &return_ids,
    const std::vector<ObjectID> &argument_ids,
    bool release_lineage,
    const rpc::Address &worker_addr,
    const ReferenceTableProto &borrowed_refs,
    std::vector<ObjectID> *deleted) {
  absl::MutexLock lock(&mutex_);
  for (const auto &return_id : return_ids) {
    UpdateObjectPendingCreationInternal(return_id, false);
  }
  // Must merge the borrower refs before decrementing any ref counts. This is
  // to make sure that for serialized IDs, we increment the borrower count for
  // the inner ID before decrementing the submitted_task_ref_count for the
  // outer ID.
  const auto refs = ReferenceTableFromProto(borrowed_refs);
  if (!refs.empty()) {
    RAY_CHECK(!WorkerID::FromBinary(worker_addr.worker_id()).IsNil());
  }
  for (const ObjectID &argument_id : argument_ids) {
    MergeRemoteBorrowers(argument_id, worker_addr, refs);
  }

  RemoveSubmittedTaskReferences(argument_ids, release_lineage, deleted);
}

int64_t ReferenceCounter::ReleaseLineageReferences(ReferenceTable::iterator ref) {
  int64_t lineage_bytes_evicted = 0;
  std::vector<ObjectID> argument_ids;
  if (on_lineage_released_ && ref->second.owned_by_us) {
    RAY_LOG(DEBUG) << "Releasing lineage for object " << ref->first;
    lineage_bytes_evicted += on_lineage_released_(ref->first, &argument_ids);
    // The object is still in scope by the application and it was
    // reconstructable with lineage. Mark that its lineage has been evicted so
    // we can return the right error during reconstruction.
    if (!ref->second.OutOfScope(lineage_pinning_enabled_) &&
        ref->second.is_reconstructable) {
      ref->second.lineage_evicted = true;
      ref->second.is_reconstructable = false;
    }
  }

  for (const ObjectID &argument_id : argument_ids) {
    auto arg_it = object_id_refs_.find(argument_id);
    if (arg_it == object_id_refs_.end()) {
      continue;
    }

    if (arg_it->second.lineage_ref_count == 0) {
      continue;
    }

    RAY_LOG(DEBUG) << "Releasing lineage internal for argument " << argument_id;
    arg_it->second.lineage_ref_count--;
    if (arg_it->second.OutOfScope(lineage_pinning_enabled_)) {
      OnObjectOutOfScopeOrFreed(arg_it);
    }
    if (arg_it->second.ShouldDelete(lineage_pinning_enabled_)) {
      RAY_CHECK(arg_it->second.on_ref_removed == nullptr);
      lineage_bytes_evicted += ReleaseLineageReferences(arg_it);
      EraseReference(arg_it);
    }
  }
  return lineage_bytes_evicted;
}

void ReferenceCounter::RemoveSubmittedTaskReferences(
    const std::vector<ObjectID> &argument_ids,
    bool release_lineage,
    std::vector<ObjectID> *deleted) {
  for (const ObjectID &argument_id : argument_ids) {
    RAY_LOG(DEBUG) << "Releasing ref for submitted task argument " << argument_id;
    auto it = object_id_refs_.find(argument_id);
    if (it == object_id_refs_.end()) {
      RAY_LOG(WARNING) << "Tried to decrease ref count for nonexistent object ID: "
                       << argument_id;
      return;
    }
    RAY_CHECK(it->second.submitted_task_ref_count > 0);
    it->second.submitted_task_ref_count--;
    if (release_lineage) {
      if (it->second.lineage_ref_count > 0) {
        it->second.lineage_ref_count--;
      }
    }
    if (it->second.RefCount() == 0) {
      DeleteReferenceInternal(it, deleted);
    }
  }
}

bool ReferenceCounter::HasOwner(const ObjectID &object_id) const {
  absl::MutexLock lock(&mutex_);
  return object_id_refs_.find(object_id) != object_id_refs_.end();
}

bool ReferenceCounter::GetOwner(const ObjectID &object_id,
                                rpc::Address *owner_address) const {
  absl::MutexLock lock(&mutex_);
  return GetOwnerInternal(object_id, owner_address);
}

bool ReferenceCounter::GetOwnerInternal(const ObjectID &object_id,
                                        rpc::Address *owner_address) const {
  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    return false;
  }

  if (it->second.owner_address) {
    *owner_address = *it->second.owner_address;
    return true;
  } else {
    return false;
  }
}

std::vector<rpc::Address> ReferenceCounter::GetOwnerAddresses(
    const std::vector<ObjectID> &object_ids) const {
  absl::MutexLock lock(&mutex_);
  std::vector<rpc::Address> owner_addresses;
  for (const auto &object_id : object_ids) {
    rpc::Address owner_addr;
    bool has_owner = GetOwnerInternal(object_id, &owner_addr);
    if (!has_owner) {
      RAY_LOG(WARNING)
          << " Object IDs generated randomly (ObjectID.from_random()) or out-of-band "
             "(ObjectID.from_binary(...)) cannot be passed to ray.get(), ray.wait(), or "
             "as "
             "a task argument because Ray does not know which task created them. "
             "If this was not how your object ID was generated, please file an issue "
             "at https://github.com/ray-project/ray/issues/";
      // TODO(swang): Java does not seem to keep the ref count properly, so the
      // entry may get deleted.
      owner_addresses.emplace_back();
    } else {
      owner_addresses.push_back(owner_addr);
    }
  }
  return owner_addresses;
}

bool ReferenceCounter::IsPlasmaObjectFreed(const ObjectID &object_id) const {
  absl::MutexLock lock(&mutex_);
  return freed_objects_.contains(object_id);
}

bool ReferenceCounter::TryMarkFreedObjectInUseAgain(const ObjectID &object_id) {
  absl::MutexLock lock(&mutex_);
  if (!object_id_refs_.contains(object_id)) {
    return false;
  }
  return freed_objects_.erase(object_id) != 0u;
}

void ReferenceCounter::FreePlasmaObjects(const std::vector<ObjectID> &object_ids) {
  absl::MutexLock lock(&mutex_);
  for (const ObjectID &object_id : object_ids) {
    auto it = object_id_refs_.find(object_id);
    if (it == object_id_refs_.end()) {
      RAY_LOG(WARNING) << "Tried to free an object " << object_id
                       << " that is already out of scope";
      continue;
    }
    // The object is still in scope. It will be removed from this set
    // once its Reference has been deleted.
    freed_objects_.insert(object_id);
    if (!it->second.owned_by_us) {
      RAY_LOG(WARNING)
          << "Tried to free an object " << object_id
          << " that we did not create. The object value may not be released.";
      continue;
    }
    // Free only the plasma value. We must keep the reference around so that we
    // have the ownership information.
    OnObjectOutOfScopeOrFreed(it);
  }
}

void ReferenceCounter::DeleteReferenceInternal(ReferenceTable::iterator it,
                                               std::vector<ObjectID> *deleted) {
  const ObjectID id = it->first;
  RAY_LOG(DEBUG) << "Attempting to delete object " << id;
  if (it->second.RefCount() == 0 && it->second.on_ref_removed) {
    RAY_LOG(DEBUG) << "Calling on_ref_removed for object " << id;
    it->second.on_ref_removed(id);
    it->second.on_ref_removed = nullptr;
  }

  PRINT_REF_COUNT(it);

  // Whether it is safe to unpin the value.
  if (it->second.OutOfScope(lineage_pinning_enabled_)) {
    for (const auto &inner_id : it->second.nested().contains) {
      auto inner_it = object_id_refs_.find(inner_id);
      if (inner_it != object_id_refs_.end()) {
        RAY_LOG(DEBUG) << "Try to delete inner object " << inner_id;
        if (it->second.owned_by_us) {
          // If this object ID was nested in an owned object, make sure that
          // the outer object counted towards the ref count for the inner
          // object.
          RAY_CHECK(inner_it->second.mutable_nested()->contained_in_owned.erase(id));
        } else {
          RAY_CHECK(
              inner_it->second.mutable_nested()->contained_in_borrowed_ids.erase(id));
        }
        // NOTE: a NestedReferenceCount struct is created after the first
        // mutable_nested() call, but the struct will not be deleted until the
        // enclosing Reference struct is deleted.
        DeleteReferenceInternal(inner_it, deleted);
      }
    }
    OnObjectOutOfScopeOrFreed(it);
    if (deleted != nullptr) {
      deleted->push_back(id);
    }

    auto index_it = reconstructable_owned_objects_index_.find(id);
    if (index_it != reconstructable_owned_objects_index_.end()) {
      reconstructable_owned_objects_.erase(index_it->second);
      reconstructable_owned_objects_index_.erase(index_it);
    }
  }

  if (it->second.ShouldDelete(lineage_pinning_enabled_)) {
    RAY_LOG(DEBUG) << "Deleting Reference to object " << id;
    // TODO(swang): Update lineage_ref_count for nested objects?
    ReleaseLineageReferences(it);
    EraseReference(it);
  }
}

void ReferenceCounter::EraseReference(ReferenceTable::iterator it) {
  // NOTE(swang): We have to publish failure to subscribers in case they
  // subscribe after the ref is already deleted.
  object_info_publisher_->PublishFailure(
      rpc::ChannelType::WORKER_OBJECT_LOCATIONS_CHANNEL, it->first.Binary());

  RAY_CHECK(it->second.ShouldDelete(lineage_pinning_enabled_));
  auto index_it = reconstructable_owned_objects_index_.find(it->first);
  if (index_it != reconstructable_owned_objects_index_.end()) {
    reconstructable_owned_objects_.erase(index_it->second);
    reconstructable_owned_objects_index_.erase(index_it);
  }
  freed_objects_.erase(it->first);
  if (it->second.owned_by_us) {
    if (ObjectID::IsActorID(it->first)) {
      num_actors_owned_by_us_--;
    } else {
      num_objects_owned_by_us_--;
    }
  }
  if (it->second.on_object_ref_delete) {
    it->second.on_object_ref_delete(it->first);
  }
  object_id_refs_.erase(it);
  ShutdownIfNeeded();
}

int64_t ReferenceCounter::EvictLineage(int64_t min_bytes_to_evict) {
  absl::MutexLock lock(&mutex_);
  int64_t lineage_bytes_evicted = 0;
  while (!reconstructable_owned_objects_.empty() &&
         lineage_bytes_evicted < min_bytes_to_evict) {
    ObjectID object_id = std::move(reconstructable_owned_objects_.front());
    reconstructable_owned_objects_.pop_front();
    reconstructable_owned_objects_index_.erase(object_id);

    auto it = object_id_refs_.find(object_id);
    RAY_CHECK(it != object_id_refs_.end());
    lineage_bytes_evicted += ReleaseLineageReferences(it);
  }
  return lineage_bytes_evicted;
}

void ReferenceCounter::OnObjectOutOfScopeOrFreed(ReferenceTable::iterator it) {
  RAY_LOG(DEBUG) << "Calling on_object_out_of_scope_or_freed_callbacks for object "
                 << it->first << " num callbacks: "
                 << it->second.on_object_out_of_scope_or_freed_callbacks.size();
  for (const auto &callback : it->second.on_object_out_of_scope_or_freed_callbacks) {
    callback(it->first);
  }
  it->second.on_object_out_of_scope_or_freed_callbacks.clear();
  UnsetObjectPrimaryCopy(it);
}

void ReferenceCounter::UnsetObjectPrimaryCopy(ReferenceTable::iterator it) {
  it->second.pinned_at_raylet_id.reset();
  if (it->second.spilled && !it->second.spilled_node_id.IsNil()) {
    it->second.spilled = false;
    it->second.spilled_url = "";
    it->second.spilled_node_id = NodeID::Nil();
  }
}

bool ReferenceCounter::SetObjectRefDeletedCallback(
    const ObjectID &object_id, const std::function<void(const ObjectID &)> callback) {
  absl::MutexLock lock(&mutex_);
  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    return false;
  }
  it->second.on_object_ref_delete = callback;
  return true;
}

bool ReferenceCounter::AddObjectOutOfScopeOrFreedCallback(
    const ObjectID &object_id, const std::function<void(const ObjectID &)> callback) {
  absl::MutexLock lock(&mutex_);
  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    return false;
  } else if (it->second.OutOfScope(lineage_pinning_enabled_) &&
             !it->second.ShouldDelete(lineage_pinning_enabled_)) {
    // The object has already gone out of scope but cannot be deleted yet. Do
    // not set the deletion callback because it may never get called.
    return false;
  } else if (freed_objects_.contains(object_id)) {
    // The object has been freed by the language frontend, so it
    // should be deleted immediately.
    return false;
  }

  it->second.on_object_out_of_scope_or_freed_callbacks.emplace_back(callback);
  return true;
}

void ReferenceCounter::ResetObjectsOnRemovedNode(const NodeID &raylet_id) {
  absl::MutexLock lock(&mutex_);
  for (auto it = object_id_refs_.begin(); it != object_id_refs_.end(); it++) {
    const auto &object_id = it->first;
    if (it->second.pinned_at_raylet_id.value_or(NodeID::Nil()) == raylet_id ||
        it->second.spilled_node_id == raylet_id) {
      UnsetObjectPrimaryCopy(it);
      if (!it->second.OutOfScope(lineage_pinning_enabled_)) {
        objects_to_recover_.push_back(object_id);
      }
    }
    RemoveObjectLocationInternal(it, raylet_id);
  }
}

std::vector<ObjectID> ReferenceCounter::FlushObjectsToRecover() {
  absl::MutexLock lock(&mutex_);
  std::vector<ObjectID> objects_to_recover = std::move(objects_to_recover_);
  objects_to_recover_.clear();
  return objects_to_recover;
}

void ReferenceCounter::UpdateObjectPinnedAtRaylet(const ObjectID &object_id,
                                                  const NodeID &raylet_id) {
  absl::MutexLock lock(&mutex_);
  auto it = object_id_refs_.find(object_id);
  if (it != object_id_refs_.end()) {
    if (freed_objects_.contains(object_id)) {
      // The object has been freed by the language frontend.
      return;
    }

    // The object is still in scope. Track the raylet location until the object
    // has gone out of scope or the raylet fails, whichever happens first.
    if (it->second.pinned_at_raylet_id.has_value()) {
      RAY_LOG(INFO).WithField(object_id)
          << "Updating primary location for object to node " << raylet_id
          << ", but it already has a primary location " << *it->second.pinned_at_raylet_id
          << ". This should only happen during reconstruction";
    }
    // Only the owner tracks the location.
    RAY_CHECK(it->second.owned_by_us);
    if (!it->second.OutOfScope(lineage_pinning_enabled_)) {
      if (check_node_alive_(raylet_id)) {
        it->second.pinned_at_raylet_id = raylet_id;
      } else {
        UnsetObjectPrimaryCopy(it);
        objects_to_recover_.push_back(object_id);
      }
    }
  }
}

bool ReferenceCounter::IsPlasmaObjectPinnedOrSpilled(const ObjectID &object_id,
                                                     bool *owned_by_us,
                                                     NodeID *pinned_at,
                                                     bool *spilled) const {
  absl::MutexLock lock(&mutex_);
  auto it = object_id_refs_.find(object_id);
  if (it != object_id_refs_.end()) {
    if (it->second.owned_by_us) {
      *owned_by_us = true;
      *spilled = it->second.spilled;
      *pinned_at = it->second.pinned_at_raylet_id.value_or(NodeID::Nil());
    }
    return true;
  }
  return false;
}

bool ReferenceCounter::HasReference(const ObjectID &object_id) const {
  absl::MutexLock lock(&mutex_);
  return object_id_refs_.find(object_id) != object_id_refs_.end();
}

size_t ReferenceCounter::NumObjectIDsInScope() const {
  absl::MutexLock lock(&mutex_);
  return object_id_refs_.size();
}

size_t ReferenceCounter::NumObjectsOwnedByUs() const {
  absl::MutexLock lock(&mutex_);
  return num_objects_owned_by_us_;
}

size_t ReferenceCounter::NumActorsOwnedByUs() const {
  absl::MutexLock lock(&mutex_);
  return num_actors_owned_by_us_;
}

std::unordered_set<ObjectID> ReferenceCounter::GetAllInScopeObjectIDs() const {
  absl::MutexLock lock(&mutex_);
  std::unordered_set<ObjectID> in_scope_object_ids;
  in_scope_object_ids.reserve(object_id_refs_.size());
  for (const auto &[id, ref] : object_id_refs_) {
    in_scope_object_ids.insert(id);
  }
  return in_scope_object_ids;
}

std::unordered_map<ObjectID, std::pair<size_t, size_t>>
ReferenceCounter::GetAllReferenceCounts() const {
  absl::MutexLock lock(&mutex_);
  std::unordered_map<ObjectID, std::pair<size_t, size_t>> all_ref_counts;
  all_ref_counts.reserve(object_id_refs_.size());
  for (const auto &[id, ref] : object_id_refs_) {
    all_ref_counts.emplace(
        id, std::pair<size_t, size_t>(ref.local_ref_count, ref.submitted_task_ref_count));
  }
  return all_ref_counts;
}

void ReferenceCounter::PopAndClearLocalBorrowers(
    const std::vector<ObjectID> &borrowed_ids,
    ReferenceCounter::ReferenceTableProto *proto,
    std::vector<ObjectID> *deleted) {
  absl::MutexLock lock(&mutex_);
  ReferenceProtoTable borrowed_refs;
  for (const auto &borrowed_id : borrowed_ids) {
    // Setting `deduct_local_ref` to true to decrease the ref count for each of the
    // borrowed IDs. This is because we artificially increment each borrowed ID to
    // keep it pinned during task execution. However, this should not count towards
    // the final ref count / existence of local ref returned to the task's caller.
    RAY_CHECK(GetAndClearLocalBorrowersInternal(borrowed_id,
                                                /*for_ref_removed=*/false,
                                                /*deduct_local_ref=*/true,
                                                &borrowed_refs))
        << borrowed_id;
  }
  ReferenceTableToProto(borrowed_refs, proto);

  for (const auto &borrowed_id : borrowed_ids) {
    RAY_LOG(DEBUG).WithField(borrowed_id) << "Remove local reference to borrowed object.";
    auto it = object_id_refs_.find(borrowed_id);
    if (it == object_id_refs_.end()) {
      RAY_LOG(WARNING).WithField(borrowed_id)
          << "Tried to decrease ref count for nonexistent object.";
      continue;
    }
    if (it->second.local_ref_count == 0) {
      RAY_LOG(WARNING).WithField(borrowed_id)
          << "Tried to decrease ref count for object ID that has count 0. This should "
             "only happen if ray.internal.free was called earlier.";
    } else {
      it->second.local_ref_count--;
    }
    PRINT_REF_COUNT(it);
    if (it->second.RefCount() == 0) {
      DeleteReferenceInternal(it, deleted);
    }
  }
}

bool ReferenceCounter::GetAndClearLocalBorrowersInternal(
    const ObjectID &object_id,
    bool for_ref_removed,
    bool deduct_local_ref,
    ReferenceProtoTable *borrowed_refs) {
  RAY_LOG(DEBUG).WithField(object_id) << "Pop object for_ref_removed " << for_ref_removed;
  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    return false;
  }

  auto &ref = it->second;
  // We only borrow objects that we do not own. This is not an assertion
  // because it is possible to receive a reference to an object that we already
  // own, e.g., if we execute a task that has an object ID in its arguments
  // that we created in an earlier task.
  if (ref.owned_by_us) {
    // Return true because we have the ref, but there is no need to return it
    // since we own the object.
    return true;
  }

  if (for_ref_removed || !ref.foreign_owner_already_monitoring) {
    auto [borrowed_ref_it, inserted] = borrowed_refs->try_emplace(object_id);
    if (inserted) {
      ref.ToProto(&borrowed_ref_it->second, deduct_local_ref);
      // Clear the local list of borrowers that we have accumulated. The receiver
      // of the returned borrowed_refs must merge this list into their own list
      // until all active borrowers are merged into the owner.
      //
      // If a foreign owner process is waiting for this ref to be removed already,
      // then don't clear its stored metadata. Clearing this will prevent the
      // foreign owner from learning about the parent task borrowing this value.
      ref.borrow_info.reset();
    }
  }
  // Attempt to pop children.
  for (const auto &contained_id : it->second.nested().contains) {
    GetAndClearLocalBorrowersInternal(
        contained_id, for_ref_removed, /*deduct_local_ref=*/false, borrowed_refs);
  }
  // We've reported our nested refs.
  ref.has_nested_refs_to_report = false;

  return true;
}

void ReferenceCounter::MergeRemoteBorrowers(const ObjectID &object_id,
                                            const rpc::Address &worker_addr,
                                            const ReferenceTable &borrowed_refs) {
  RAY_LOG(DEBUG).WithField(object_id) << "Merging ref";
  auto borrower_it = borrowed_refs.find(object_id);
  if (borrower_it == borrowed_refs.end()) {
    return;
  }
  const auto &borrower_ref = borrower_it->second;
  RAY_LOG(DEBUG).WithField(object_id)
      << "Borrower ref has " << borrower_ref.borrow().borrowers.size() << " borrowers"
      << ", local: " << borrower_ref.local_ref_count
      << ", submitted: " << borrower_ref.submitted_task_ref_count
      << ", contained_in_owned: " << borrower_ref.nested().contained_in_owned.size()
      << ", stored_in_objects: " << borrower_ref.borrow().stored_in_objects.size();

  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    it = object_id_refs_.emplace(object_id, Reference()).first;
  }
  std::vector<rpc::Address> new_borrowers;

  // The worker is still using the reference, so it is still a borrower.
  if (borrower_ref.RefCount() > 0) {
    auto inserted = it->second.mutable_borrow()->borrowers.insert(worker_addr).second;
    // If we are the owner of id, then send WaitForRefRemoved to borrower.
    if (inserted) {
      RAY_LOG(DEBUG)
              .WithField(WorkerID::FromBinary(worker_addr.worker_id()))
              .WithField(object_id)
          << "Adding borrower " << worker_addr.ip_address() << ":" << worker_addr.port()
          << " to object";
      new_borrowers.push_back(worker_addr);
    }
  }

  // Add any other workers that this worker passed the ID to as new borrowers.
  for (const auto &nested_borrower : borrower_ref.borrow().borrowers) {
    auto inserted = it->second.mutable_borrow()->borrowers.insert(nested_borrower).second;
    if (inserted) {
      RAY_LOG(DEBUG)
              .WithField(WorkerID::FromBinary(nested_borrower.worker_id()))
              .WithField(object_id)
          << "Adding borrower " << nested_borrower.ip_address() << ":"
          << nested_borrower.port() << " to object";
      new_borrowers.push_back(nested_borrower);
    }
  }

  // This ref was nested inside another object. Copy this information to our
  // local table.
  for (const auto &contained_in_borrowed_id :
       borrower_it->second.nested().contained_in_borrowed_ids) {
    RAY_CHECK(borrower_ref.owner_address);
    AddBorrowedObjectInternal(object_id,
                              contained_in_borrowed_id,
                              *borrower_ref.owner_address,
                              /*foreign_owner_already_monitoring=*/false);
  }

  // If we own this ID, then wait for all new borrowers to reach a ref count
  // of 0 before GCing the object value.
  if (it->second.owned_by_us) {
    for (const auto &addr : new_borrowers) {
      WaitForRefRemoved(it, addr);
    }
  } else {
    // We received ref counts from another borrower. Make sure we forward it
    // back to the owner.
    SetNestedRefInUseRecursive(it);
  }

  // If the borrower stored this object ID inside another object ID that it did
  // not own, then mark that the object ID is nested inside another.
  for (const auto &stored_in_object : borrower_ref.borrow().stored_in_objects) {
    AddNestedObjectIdsInternal(
        stored_in_object.first, {object_id}, stored_in_object.second);
  }

  // Recursively merge any references that were contained in this object, to
  // handle any borrowers of nested objects.
  for (const auto &inner_id : borrower_ref.nested().contains) {
    MergeRemoteBorrowers(inner_id, worker_addr, borrowed_refs);
  }
  PRINT_REF_COUNT(it);
}

void ReferenceCounter::CleanupBorrowersOnRefRemoved(
    const ReferenceTable &new_borrower_refs,
    const ObjectID &object_id,
    const rpc::Address &borrower_addr) {
  absl::MutexLock lock(&mutex_);
  // Merge in any new borrowers that the previous borrower learned of.
  MergeRemoteBorrowers(object_id, borrower_addr, new_borrower_refs);

  // Erase the previous borrower.
  auto it = object_id_refs_.find(object_id);
  RAY_CHECK(it != object_id_refs_.end()) << object_id;
  RAY_CHECK(it->second.mutable_borrow()->borrowers.erase(borrower_addr));
  DeleteReferenceInternal(it, nullptr);
}

void ReferenceCounter::WaitForRefRemoved(const ReferenceTable::iterator &ref_it,
                                         const rpc::Address &addr,
                                         const ObjectID &contained_in_id) {
  const ObjectID &object_id = ref_it->first;
  RAY_LOG(DEBUG).WithField(object_id).WithField(WorkerID::FromBinary(addr.worker_id()))
      << "WaitForRefRemoved object, dest worker";
  auto sub_message = std::make_unique<rpc::SubMessage>();
  auto *request = sub_message->mutable_worker_ref_removed_message();
  // Only the owner should send requests to borrowers.
  RAY_CHECK(ref_it->second.owned_by_us);
  request->mutable_reference()->set_object_id(object_id.Binary());
  request->mutable_reference()->mutable_owner_address()->CopyFrom(
      *ref_it->second.owner_address);
  request->set_contained_in_id(contained_in_id.Binary());
  request->set_intended_worker_id(addr.worker_id());
  request->set_subscriber_worker_id(rpc_address_.worker_id());

  // If the message is published, this callback will be invoked.
  const auto message_published_callback = [this, addr, object_id](
                                              const rpc::PubMessage &msg) {
    RAY_CHECK(msg.has_worker_ref_removed_message());
    const ReferenceTable new_borrower_refs =
        ReferenceTableFromProto(msg.worker_ref_removed_message().borrowed_refs());
    RAY_LOG(DEBUG).WithField(object_id).WithField(WorkerID::FromBinary(addr.worker_id()))
        << "WaitForRefRemoved returned for object, dest worker";

    CleanupBorrowersOnRefRemoved(new_borrower_refs, object_id, addr);
    // Unsubscribe the object once the message is published.
    RAY_CHECK(object_info_subscriber_->Unsubscribe(
        rpc::ChannelType::WORKER_REF_REMOVED_CHANNEL, addr, object_id.Binary()));
  };

  // If the borrower is failed, this callback will be called.
  const auto publisher_failed_callback = [this, addr](const std::string &object_id_binary,
                                                      const Status &) {
    // When the request is failed, there's no new borrowers ref published from this
    // borrower.
    const auto object_id = ObjectID::FromBinary(object_id_binary);
    RAY_LOG(DEBUG).WithField(object_id).WithField(WorkerID::FromBinary(addr.worker_id()))
        << "WaitForRefRemoved failed for object, dest worker";
    CleanupBorrowersOnRefRemoved({}, object_id, addr);
  };

  RAY_CHECK(
      object_info_subscriber_->Subscribe(std::move(sub_message),
                                         rpc::ChannelType::WORKER_REF_REMOVED_CHANNEL,
                                         addr,
                                         object_id.Binary(),
                                         /*subscribe_done_callback=*/nullptr,
                                         message_published_callback,
                                         publisher_failed_callback));
}

void ReferenceCounter::AddNestedObjectIds(const ObjectID &object_id,
                                          const std::vector<ObjectID> &inner_ids,
                                          const rpc::Address &owner_address) {
  absl::MutexLock lock(&mutex_);
  AddNestedObjectIdsInternal(object_id, inner_ids, owner_address);
}

void ReferenceCounter::AddNestedObjectIdsInternal(const ObjectID &object_id,
                                                  const std::vector<ObjectID> &inner_ids,
                                                  const rpc::Address &owner_address) {
  RAY_CHECK(!WorkerID::FromBinary(owner_address.worker_id()).IsNil());
  auto it = object_id_refs_.find(object_id);
  if (owner_address.worker_id() == rpc_address_.worker_id()) {
    // We own object_id. This is a `ray.put()` case OR returning an object ID
    // from a task and the task's caller executed in the same process as us.
    if (it != object_id_refs_.end()) {
      RAY_CHECK(it->second.owned_by_us);
      // The outer object is still in scope. Mark the inner ones as being
      // contained in the outer object ID so we do not GC the inner objects
      // until the outer object goes out of scope.
      for (const auto &inner_id : inner_ids) {
        it->second.mutable_nested()->contains.insert(inner_id);
        RAY_LOG(DEBUG).WithField(inner_id)
            << "Setting inner ID " << inner_id << " contained_in_owned: " << object_id;
      }
      // WARNING: Following loop could invalidate `it` iterator on insertion.
      // That's why we use two loops, and we should avoid using `it` hearafter.
      for (const auto &inner_id : inner_ids) {
        auto inner_it = object_id_refs_.emplace(inner_id, Reference()).first;
        bool was_in_use = inner_it->second.RefCount() > 0;
        inner_it->second.mutable_nested()->contained_in_owned.insert(object_id);
        if (!was_in_use && inner_it->second.RefCount() > 0) {
          SetNestedRefInUseRecursive(inner_it);
        }
      }
    }
  } else {
    // We do not own object_id. This is the case where we returned an object ID
    // from a task, and the task's caller executed in a remote process.
    for (const auto &inner_id : inner_ids) {
      RAY_LOG(DEBUG).WithField(inner_id)
          << "Adding borrower " << owner_address.ip_address() << ":"
          << owner_address.port() << " to object, borrower owns outer ID " << object_id;
      auto inner_it = object_id_refs_.find(inner_id);
      if (inner_it == object_id_refs_.end()) {
        inner_it = object_id_refs_.emplace(inner_id, Reference()).first;
      }
      // Add the task's caller as a borrower.
      if (inner_it->second.owned_by_us) {
        auto inserted =
            inner_it->second.mutable_borrow()->borrowers.insert(owner_address).second;
        if (inserted) {
          // Wait for it to remove its reference.
          WaitForRefRemoved(inner_it, owner_address, object_id);
        }
      } else {
        auto inserted = inner_it->second.mutable_borrow()
                            ->stored_in_objects.emplace(object_id, owner_address)
                            .second;
        // This should be the first time that we have stored this object ID
        // inside this return ID.
        RAY_CHECK(inserted);
      }
      PRINT_REF_COUNT(inner_it);
    }
  }
}

void ReferenceCounter::HandleRefRemoved(const ObjectID &object_id) {
  RAY_LOG(DEBUG).WithField(object_id) << "HandleRefRemoved ";
  auto it = object_id_refs_.find(object_id);
  if (it != object_id_refs_.end()) {
    PRINT_REF_COUNT(it);
  }
  ReferenceProtoTable borrowed_refs;
  RAY_UNUSED(GetAndClearLocalBorrowersInternal(object_id,
                                               /*for_ref_removed=*/true,
                                               /*deduct_local_ref=*/false,
                                               &borrowed_refs));
  for (const auto &[id, ref] : borrowed_refs) {
    RAY_LOG(DEBUG).WithField(id)
        << "Object has " << ref.borrowers().size() << " borrowers, stored in "
        << ref.stored_in_objects().size();
  }

  // Send the owner information about any new borrowers.
  rpc::PubMessage pub_message;
  pub_message.set_key_id(object_id.Binary());
  pub_message.set_channel_type(rpc::ChannelType::WORKER_REF_REMOVED_CHANNEL);
  auto *worker_ref_removed_message = pub_message.mutable_worker_ref_removed_message();
  ReferenceTableToProto(borrowed_refs,
                        worker_ref_removed_message->mutable_borrowed_refs());

  RAY_LOG(DEBUG).WithField(object_id)
      << "Publishing WaitForRefRemoved message for object, message has "
      << worker_ref_removed_message->borrowed_refs().size() << " borrowed references.";
  object_info_publisher_->Publish(std::move(pub_message));
}

void ReferenceCounter::SetRefRemovedCallback(
    const ObjectID &object_id,
    const ObjectID &contained_in_id,
    const rpc::Address &owner_address,
    const ReferenceCounter::ReferenceRemovedCallback &ref_removed_callback) {
  absl::MutexLock lock(&mutex_);
  RAY_LOG(DEBUG).WithField(object_id)
      << "Received WaitForRefRemoved object contained in " << contained_in_id;

  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    it = object_id_refs_.emplace(object_id, Reference()).first;
  }

  // If we are borrowing the ID because we own an object that contains it, then
  // add the outer object to the inner ID's ref count. We will not respond to
  // the owner of the inner ID until the outer object ID goes out of scope.
  if (!contained_in_id.IsNil()) {
    AddNestedObjectIdsInternal(contained_in_id, {object_id}, rpc_address_);
  }

  if (it->second.RefCount() == 0) {
    RAY_LOG(DEBUG).WithField(object_id)
        << "Ref count for borrowed object is already 0, responding to WaitForRefRemoved";
    // We already stopped borrowing the object ID. Respond to the owner
    // immediately.
    ref_removed_callback(object_id);
    DeleteReferenceInternal(it, nullptr);
  } else {
    // We are still borrowing the object ID. Respond to the owner once we have
    // stopped borrowing it.
    if (it->second.on_ref_removed != nullptr) {
      // TODO(swang): If the owner of an object dies and and is re-executed, it
      // is possible that we will receive a duplicate request to set
      // on_ref_removed. If messages are delayed and we overwrite the
      // callback here, it's possible we will drop the request that was sent by
      // the more recent owner. We should fix this by setting multiple
      // callbacks or by versioning the owner requests.
      RAY_LOG(WARNING).WithField(object_id)
          << "on_ref_removed already set for object. The owner task must have died and "
             "been re-executed.";
    }
    it->second.on_ref_removed = ref_removed_callback;
  }
}

void ReferenceCounter::SetReleaseLineageCallback(
    const LineageReleasedCallback &callback) {
  RAY_CHECK(on_lineage_released_ == nullptr);
  on_lineage_released_ = callback;
}

bool ReferenceCounter::AddObjectLocation(const ObjectID &object_id,
                                         const NodeID &node_id) {
  absl::MutexLock lock(&mutex_);
  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    RAY_LOG(DEBUG).WithField(object_id)
        << "Tried to add an object location for an object that doesn't exist in the "
           "reference table. It can happen if the "
           "object is already evicted.";
    return false;
  }
  AddObjectLocationInternal(it, node_id);
  return true;
}

void ReferenceCounter::AddObjectLocationInternal(ReferenceTable::iterator it,
                                                 const NodeID &node_id) {
  RAY_LOG(DEBUG).WithField(node_id).WithField(it->first) << "Adding location for object";
  if (it->second.locations.emplace(node_id).second) {
    // Only push to subscribers if we added a new location. We eagerly add the pinned
    // location without waiting for the object store notification to trigger a location
    // report, so there's a chance that we already knew about the node_id location.
    PushToLocationSubscribers(it);
  }
}

bool ReferenceCounter::RemoveObjectLocation(const ObjectID &object_id,
                                            const NodeID &node_id) {
  absl::MutexLock lock(&mutex_);
  RAY_LOG(DEBUG).WithField(node_id).WithField(object_id)
      << "Removing location for object";
  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    RAY_LOG(DEBUG).WithField(object_id)
        << "Tried to remove an object location for an object that doesn't exist in the "
           "reference table. It can happen if the "
           "object is already evicted.";
    return false;
  }
  RemoveObjectLocationInternal(it, node_id);
  return true;
}

void ReferenceCounter::RemoveObjectLocationInternal(ReferenceTable::iterator it,
                                                    const NodeID &node_id) {
  it->second.locations.erase(node_id);
  PushToLocationSubscribers(it);
}

void ReferenceCounter::UpdateObjectPendingCreationInternal(const ObjectID &object_id,
                                                           bool pending_creation) {
  auto it = object_id_refs_.find(object_id);
  bool push = false;
  if (it != object_id_refs_.end()) {
    push = (it->second.pending_creation != pending_creation);
    it->second.pending_creation = pending_creation;
  }
  if (push) {
    PushToLocationSubscribers(it);
  }
}

std::optional<absl::flat_hash_set<NodeID>> ReferenceCounter::GetObjectLocations(
    const ObjectID &object_id) {
  absl::MutexLock lock(&mutex_);
  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    RAY_LOG(DEBUG).WithField(object_id)
        << "Tried to get the object locations for an object that doesn't exist in the "
           "reference table";
    return absl::nullopt;
  }
  return it->second.locations;
}

bool ReferenceCounter::HandleObjectSpilled(const ObjectID &object_id,
                                           const std::string &spilled_url,
                                           const NodeID &spilled_node_id) {
  absl::MutexLock lock(&mutex_);
  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    RAY_LOG(WARNING).WithField(object_id) << "Spilled object already out of scope";
    return false;
  }
  if (it->second.OutOfScope(lineage_pinning_enabled_) && !spilled_node_id.IsNil()) {
    // NOTE(swang): If the object is out of scope and was spilled locally by
    // its primary raylet, then we should have already sent the "object
    // evicted" notification to delete the copy at this spilled URL. Therefore,
    // we should not add this spill URL as a location.
    return false;
  }

  it->second.spilled = true;
  it->second.did_spill = true;
  bool spilled_location_alive =
      spilled_node_id.IsNil() || check_node_alive_(spilled_node_id);
  if (spilled_location_alive) {
    if (!spilled_url.empty()) {
      it->second.spilled_url = spilled_url;
    }
    if (!spilled_node_id.IsNil()) {
      it->second.spilled_node_id = spilled_node_id;
    }
    PushToLocationSubscribers(it);
  } else {
    RAY_LOG(DEBUG).WithField(spilled_node_id).WithField(object_id)
        << "Object spilled to dead node ";
    UnsetObjectPrimaryCopy(it);
    objects_to_recover_.push_back(object_id);
  }
  return true;
}

std::optional<LocalityData> ReferenceCounter::GetLocalityData(
    const ObjectID &object_id) const {
  absl::MutexLock lock(&mutex_);
  // Uses the reference table to return locality data for an object.
  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    // We don't have any information about this object so we can't return valid locality
    // data.
    RAY_LOG(DEBUG).WithField(object_id)
        << "Object not in reference table, locality data not available";
    return absl::nullopt;
  }

  // The size of this object.
  const auto object_size = it->second.object_size;
  if (object_size < 0) {
    // We don't know the object size so we can't returned valid locality data.
    RAY_LOG(DEBUG).WithField(object_id)
        << "Reference [" << it->second.call_site
        << "] for object has an unknown object size, locality data not available";
    return absl::nullopt;
  }

  // The locations of this object.
  // - If we own this object, this will contain the complete up-to-date set of object
  //   locations.
  // - If we don't own this object, this will contain a snapshot of the object locations
  //   at future resolution time.
  auto node_ids = it->second.locations;
  // Add location of the primary copy since the object must be there: either in memory or
  // spilled.
  if (it->second.pinned_at_raylet_id.has_value()) {
    node_ids.emplace(it->second.pinned_at_raylet_id.value());
  }

  // We should only reach here if we have valid locality data to return.
  std::optional<LocalityData> locality_data(
      {static_cast<uint64_t>(object_size), std::move(node_ids)});
  return locality_data;
}

bool ReferenceCounter::ReportLocalityData(const ObjectID &object_id,
                                          const absl::flat_hash_set<NodeID> &locations,
                                          uint64_t object_size) {
  absl::MutexLock lock(&mutex_);
  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    RAY_LOG(DEBUG).WithField(object_id) << "Tried to report locality data for an object "
                                           "that doesn't exist in the reference table."
                                        << " The object has probably already been freed.";
    return false;
  }
  RAY_CHECK(!it->second.owned_by_us)
      << "ReportLocalityData should only be used for borrowed references.";
  for (const auto &location : locations) {
    it->second.locations.emplace(location);
  }
  if (object_size > 0) {
    it->second.object_size = object_size;
  }
  return true;
}

void ReferenceCounter::AddBorrowerAddress(const ObjectID &object_id,
                                          const rpc::Address &borrower_address) {
  absl::MutexLock lock(&mutex_);
  auto it = object_id_refs_.find(object_id);
  RAY_CHECK(it != object_id_refs_.end());

  RAY_CHECK(it->second.owned_by_us)
      << "AddBorrowerAddress should only be used for owner references.";

  RAY_CHECK(borrower_address.worker_id() != rpc_address_.worker_id())
      << "The borrower cannot be the owner itself";

  RAY_LOG(DEBUG).WithField(object_id)
      << "Add borrower " << borrower_address.DebugString() << " for object";
  auto inserted = it->second.mutable_borrow()->borrowers.insert(borrower_address).second;
  if (inserted) {
    WaitForRefRemoved(it, borrower_address);
  }
}

bool ReferenceCounter::IsObjectReconstructable(const ObjectID &object_id,
                                               bool *lineage_evicted) const {
  if (!lineage_pinning_enabled_) {
    return false;
  }
  absl::MutexLock lock(&mutex_);
  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    return false;
  }
  *lineage_evicted = it->second.lineage_evicted;
  return it->second.is_reconstructable;
}

void ReferenceCounter::UpdateObjectPendingCreation(const ObjectID &object_id,
                                                   bool pending_creation) {
  absl::MutexLock lock(&mutex_);
  UpdateObjectPendingCreationInternal(object_id, pending_creation);
}

bool ReferenceCounter::IsObjectPendingCreation(const ObjectID &object_id) const {
  absl::MutexLock lock(&mutex_);
  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    return false;
  }
  return it->second.pending_creation;
}

void ReferenceCounter::PushToLocationSubscribers(ReferenceTable::iterator it) {
  const auto &object_id = it->first;
  const auto &locations = it->second.locations;
  auto object_size = it->second.object_size;
  const auto &spilled_url = it->second.spilled_url;
  const auto &spilled_node_id = it->second.spilled_node_id;
  const auto &optional_primary_node_id = it->second.pinned_at_raylet_id;
  const auto &primary_node_id = optional_primary_node_id.value_or(NodeID::Nil());
  RAY_LOG(DEBUG).WithField(object_id)
      << "Published message for object, " << locations.size()
      << " locations, spilled url: [" << spilled_url
      << "], spilled node ID: " << spilled_node_id << ", and object size: " << object_size
      << ", and primary node ID: " << primary_node_id << ", pending creation? "
      << it->second.pending_creation;
  rpc::PubMessage pub_message;
  pub_message.set_key_id(object_id.Binary());
  pub_message.set_channel_type(rpc::ChannelType::WORKER_OBJECT_LOCATIONS_CHANNEL);
  auto object_locations_msg = pub_message.mutable_worker_object_locations_message();
  FillObjectInformationInternal(it, object_locations_msg);

  object_info_publisher_->Publish(std::move(pub_message));
}

void ReferenceCounter::FillObjectInformation(
    const ObjectID &object_id, rpc::WorkerObjectLocationsPubMessage *object_info) {
  RAY_CHECK(object_info != nullptr);
  absl::MutexLock lock(&mutex_);
  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    RAY_LOG(WARNING).WithField(object_id)
        << "Object locations requested for object, but ref already removed. This may be "
           "a bug in the distributed "
           "reference counting protocol.";
    object_info->set_ref_removed(true);
  } else {
    FillObjectInformationInternal(it, object_info);
  }
}

void ReferenceCounter::FillObjectInformationInternal(
    ReferenceTable::iterator it, rpc::WorkerObjectLocationsPubMessage *object_info) {
  for (const auto &node_id : it->second.locations) {
    object_info->add_node_ids(node_id.Binary());
  }
  int64_t object_size = it->second.object_size;
  if (object_size > 0) {
    object_info->set_object_size(it->second.object_size);
  }
  object_info->set_spilled_url(it->second.spilled_url);
  object_info->set_spilled_node_id(it->second.spilled_node_id.Binary());
  auto primary_node_id = it->second.pinned_at_raylet_id.value_or(NodeID::Nil());
  object_info->set_primary_node_id(primary_node_id.Binary());
  object_info->set_pending_creation(it->second.pending_creation);
  object_info->set_did_spill(it->second.did_spill);
}

void ReferenceCounter::PublishObjectLocationSnapshot(const ObjectID &object_id) {
  absl::MutexLock lock(&mutex_);
  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    RAY_LOG(WARNING).WithField(object_id)
        << "Object locations requested for object, but ref already removed. This may be "
           "a bug in the distributed "
           "reference counting protocol.";
    // First let subscribers handle this error.
    rpc::PubMessage pub_message;
    pub_message.set_key_id(object_id.Binary());
    pub_message.set_channel_type(rpc::ChannelType::WORKER_OBJECT_LOCATIONS_CHANNEL);
    pub_message.mutable_worker_object_locations_message()->set_ref_removed(true);
    object_info_publisher_->Publish(pub_message);
    // Then, publish a failure to subscribers since this object is unreachable.
    object_info_publisher_->PublishFailure(
        rpc::ChannelType::WORKER_OBJECT_LOCATIONS_CHANNEL, object_id.Binary());
    return;
  }

  // Always publish the location when subscribed for the first time.
  // This will ensure that the subscriber will get the first snapshot of the
  // object location.
  PushToLocationSubscribers(it);
}

std::string ReferenceCounter::DebugString() const {
  absl::MutexLock lock(&mutex_);
  std::stringstream ss;
  ss << "ReferenceTable{size: " << object_id_refs_.size();
  if (!object_id_refs_.empty()) {
    ss << " sample: " << object_id_refs_.begin()->first << ":"
       << object_id_refs_.begin()->second.DebugString();
  }
  ss << "}";
  return ss.str();
}

std::string ReferenceCounter::Reference::DebugString() const {
  std::stringstream ss;
  ss << "Reference{borrowers: " << borrow().borrowers.size()
     << " local_ref_count: " << local_ref_count
     << " submitted_count: " << submitted_task_ref_count
     << " contained_on_owned: " << nested().contained_in_owned.size()
     << " contained_in_borrowed: " << nested().contained_in_borrowed_ids.size()
     << " contains: " << nested().contains.size()
     << " stored_in: " << borrow().stored_in_objects.size()
     << " lineage_ref_count: " << lineage_ref_count << "}";
  return ss.str();
}

ReferenceCounter::Reference ReferenceCounter::Reference::FromProto(
    const rpc::ObjectReferenceCount &ref_count) {
  Reference ref;
  ref.owner_address = ref_count.reference().owner_address();
  ref.local_ref_count = ref_count.has_local_ref() ? 1 : 0;

  for (const auto &borrower : ref_count.borrowers()) {
    ref.mutable_borrow()->borrowers.insert(borrower);
  }
  for (const auto &object : ref_count.stored_in_objects()) {
    const auto &object_id = ObjectID::FromBinary(object.object_id());
    ref.mutable_borrow()->stored_in_objects.emplace(object_id, object.owner_address());
  }
  for (const auto &id : ref_count.contains()) {
    ref.mutable_nested()->contains.insert(ObjectID::FromBinary(id));
  }
  const auto contained_in_borrowed_ids =
      IdVectorFromProtobuf<ObjectID>(ref_count.contained_in_borrowed_ids());
  ref.mutable_nested()->contained_in_borrowed_ids.insert(
      contained_in_borrowed_ids.begin(), contained_in_borrowed_ids.end());
  return ref;
}

void ReferenceCounter::Reference::ToProto(rpc::ObjectReferenceCount *ref,
                                          bool deduct_local_ref) const {
  if (owner_address) {
    ref->mutable_reference()->mutable_owner_address()->CopyFrom(*owner_address);
  }
  ref->set_has_local_ref(RefCount() > (deduct_local_ref ? 1 : 0));
  for (const auto &borrower : borrow().borrowers) {
    ref->add_borrowers()->CopyFrom(borrower);
  }
  for (const auto &object : borrow().stored_in_objects) {
    auto ref_object = ref->add_stored_in_objects();
    ref_object->set_object_id(object.first.Binary());
    ref_object->mutable_owner_address()->CopyFrom(object.second);
  }
  for (const auto &contained_in_borrowed_id : nested().contained_in_borrowed_ids) {
    ref->add_contained_in_borrowed_ids(contained_in_borrowed_id.Binary());
  }
  for (const auto &contains_id : nested().contains) {
    ref->add_contains(contains_id.Binary());
  }
}

std::optional<rpc::TensorTransport> ReferenceCounter::GetTensorTransport(
    const ObjectID &object_id) const {
  absl::MutexLock lock(&mutex_);
  auto it = object_id_refs_.find(object_id);
  if (it == object_id_refs_.end()) {
    return absl::nullopt;
  }
  return it->second.tensor_transport;
}

}  // namespace core
}  // namespace ray

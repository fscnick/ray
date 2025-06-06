import json
import os
import random
import shutil
import sys
import tempfile
import time
import unittest
from collections import Counter
from unittest.mock import MagicMock

import numpy as np
import pytest

import ray
from ray import tune
from ray.air.constants import TRAINING_ITERATION
from ray.train._internal.checkpoint_manager import _CheckpointManager
from ray.train._internal.session import _FutureTrainingResult, _TrainingResult
from ray.train._internal.storage import StorageContext
from ray.tune import Checkpoint, CheckpointConfig, PlacementGroupFactory, Trainable
from ray.tune.experiment import Trial
from ray.tune.experiment.trial import _TemporaryTrialState
from ray.tune.schedulers import (
    AsyncHyperBandScheduler,
    FIFOScheduler,
    HyperBandForBOHB,
    HyperBandScheduler,
    MedianStoppingRule,
    PopulationBasedTraining,
    TrialScheduler,
)
from ray.tune.schedulers.pbt import PopulationBasedTrainingReplay, _explore
from ray.tune.search import ConcurrencyLimiter
from ray.tune.search._mock import _MockSearcher
from ray.tune.trainable.metadata import _TrainingRunMetadata
from ray.tune.utils.mock_trainable import MOCK_TRAINABLE_NAME, register_mock_trainable


def result(t, rew):
    return dict(time_total_s=t, episode_reward_mean=rew, training_iteration=int(t))


def mock_tune_controller(trials=None):
    tune_controller = MagicMock()
    tune_controller.get_trials.return_value = trials or []
    return tune_controller


class EarlyStoppingSuite(unittest.TestCase):
    def setUp(self):
        ray.init(num_cpus=2)

        register_mock_trainable()

    def tearDown(self):
        ray.shutdown()

    def basicSetup(self, rule):
        t1 = Trial(MOCK_TRAINABLE_NAME)  # mean is 450, max 900, t_max=10
        t2 = Trial(MOCK_TRAINABLE_NAME)  # mean is 450, max 450, t_max=5
        runner = mock_tune_controller()
        for i in range(10):
            r1 = result(i, i * 100)
            print("basicSetup:", i)
            self.assertEqual(
                rule.on_trial_result(runner, t1, r1), TrialScheduler.CONTINUE
            )
        for i in range(5):
            r2 = result(i, 450)
            self.assertEqual(
                rule.on_trial_result(runner, t2, r2), TrialScheduler.CONTINUE
            )
        return t1, t2

    def testMedianStoppingConstantPerf(self):
        rule = MedianStoppingRule(
            metric="episode_reward_mean",
            mode="max",
            grace_period=0,
            min_samples_required=1,
        )
        t1, t2 = self.basicSetup(rule)
        runner = mock_tune_controller()
        rule.on_trial_complete(runner, t1, result(10, 1000))
        self.assertEqual(
            rule.on_trial_result(runner, t2, result(5, 450)), TrialScheduler.CONTINUE
        )
        self.assertEqual(
            rule.on_trial_result(runner, t2, result(6, 0)), TrialScheduler.CONTINUE
        )
        self.assertEqual(
            rule.on_trial_result(runner, t2, result(10, 450)), TrialScheduler.STOP
        )

    def testMedianStoppingOnCompleteOnly(self):
        rule = MedianStoppingRule(
            metric="episode_reward_mean",
            mode="max",
            grace_period=0,
            min_samples_required=1,
        )
        t1, t2 = self.basicSetup(rule)
        runner = mock_tune_controller()
        self.assertEqual(
            rule.on_trial_result(runner, t2, result(100, 0)), TrialScheduler.CONTINUE
        )
        rule.on_trial_complete(runner, t1, result(101, 1000))
        self.assertEqual(
            rule.on_trial_result(runner, t2, result(101, 0)), TrialScheduler.STOP
        )

    def testMedianStoppingGracePeriod(self):
        rule = MedianStoppingRule(
            metric="episode_reward_mean",
            mode="max",
            grace_period=2.5,
            min_samples_required=1,
        )
        t1, t2 = self.basicSetup(rule)
        runner = mock_tune_controller()
        rule.on_trial_complete(runner, t1, result(10, 1000))
        rule.on_trial_complete(runner, t2, result(10, 1000))
        t3 = Trial(MOCK_TRAINABLE_NAME)
        self.assertEqual(
            rule.on_trial_result(runner, t3, result(1, 10)), TrialScheduler.CONTINUE
        )
        self.assertEqual(
            rule.on_trial_result(runner, t3, result(2, 10)), TrialScheduler.CONTINUE
        )
        self.assertEqual(
            rule.on_trial_result(runner, t3, result(3, 10)), TrialScheduler.STOP
        )

    def testMedianStoppingMinSamples(self):
        rule = MedianStoppingRule(
            metric="episode_reward_mean",
            mode="max",
            grace_period=0,
            min_samples_required=2,
        )
        t1, t2 = self.basicSetup(rule)
        runner = mock_tune_controller()
        rule.on_trial_complete(runner, t1, result(10, 1000))
        t3 = Trial(MOCK_TRAINABLE_NAME)
        # Insufficient samples to evaluate t3
        self.assertEqual(
            rule.on_trial_result(runner, t3, result(5, 10)), TrialScheduler.CONTINUE
        )
        rule.on_trial_complete(runner, t2, result(5, 1000))
        # Sufficient samples to evaluate t3
        self.assertEqual(
            rule.on_trial_result(runner, t3, result(5, 10)), TrialScheduler.STOP
        )

    def testMedianStoppingUsesMedian(self):
        rule = MedianStoppingRule(
            metric="episode_reward_mean",
            mode="max",
            grace_period=0,
            min_samples_required=1,
        )
        t1, t2 = self.basicSetup(rule)
        runner = mock_tune_controller()
        rule.on_trial_complete(runner, t1, result(10, 1000))
        rule.on_trial_complete(runner, t2, result(10, 1000))
        t3 = Trial(MOCK_TRAINABLE_NAME)
        self.assertEqual(
            rule.on_trial_result(runner, t3, result(1, 260)), TrialScheduler.CONTINUE
        )
        self.assertEqual(
            rule.on_trial_result(runner, t3, result(2, 260)), TrialScheduler.STOP
        )

    def testMedianStoppingSoftStop(self):
        rule = MedianStoppingRule(
            metric="episode_reward_mean",
            mode="max",
            grace_period=0,
            min_samples_required=1,
            hard_stop=False,
        )
        t1, t2 = self.basicSetup(rule)
        runner = mock_tune_controller()
        rule.on_trial_complete(runner, t1, result(10, 1000))
        rule.on_trial_complete(runner, t2, result(10, 1000))
        t3 = Trial(MOCK_TRAINABLE_NAME)
        self.assertEqual(
            rule.on_trial_result(runner, t3, result(1, 260)), TrialScheduler.CONTINUE
        )
        self.assertEqual(
            rule.on_trial_result(runner, t3, result(2, 260)), TrialScheduler.PAUSE
        )

    def _test_metrics(self, result_func, metric, mode):
        rule = MedianStoppingRule(
            grace_period=0,
            min_samples_required=1,
            time_attr="training_iteration",
            metric=metric,
            mode=mode,
        )
        t1 = Trial(MOCK_TRAINABLE_NAME)  # mean is 450, max 900, t_max=10
        t2 = Trial(MOCK_TRAINABLE_NAME)  # mean is 450, max 450, t_max=5
        runner = mock_tune_controller()
        for i in range(10):
            self.assertEqual(
                rule.on_trial_result(runner, t1, result_func(i, i * 100)),
                TrialScheduler.CONTINUE,
            )
        for i in range(5):
            self.assertEqual(
                rule.on_trial_result(runner, t2, result_func(i, 450)),
                TrialScheduler.CONTINUE,
            )
        rule.on_trial_complete(runner, t1, result_func(10, 1000))
        self.assertEqual(
            rule.on_trial_result(runner, t2, result_func(5, 450)),
            TrialScheduler.CONTINUE,
        )
        self.assertEqual(
            rule.on_trial_result(runner, t2, result_func(6, 0)), TrialScheduler.CONTINUE
        )

    def testAlternateMetrics(self):
        def result2(t, rew):
            return dict(training_iteration=t, neg_mean_loss=rew)

        self._test_metrics(result2, "neg_mean_loss", "max")

    def testAlternateMetricsMin(self):
        def result2(t, rew):
            return dict(training_iteration=t, mean_loss=-rew)

        self._test_metrics(result2, "mean_loss", "min")


class _FakeFutureResult(_FutureTrainingResult):
    def __init__(self, result):
        self.result = result

    def resolve(self, block: bool = True):
        return self.result


class _MockTrialRunner:
    def __init__(self, scheduler):
        self._scheduler_alg = scheduler
        self.search_alg = None
        self.trials = []

    def process_action(self, trial, action):
        if action == TrialScheduler.CONTINUE:
            pass
        elif action == TrialScheduler.PAUSE:
            self.pause_trial(trial)
        elif action == TrialScheduler.STOP:
            self.stop_trial(trial)

    def pause_trial(self, trial, should_checkpoint: bool = True):
        if should_checkpoint:
            self._schedule_trial_save(trial, None)
        trial.status = Trial.PAUSED

    def stop_trial(self, trial, error=False, error_msg=None):
        if trial.status in [Trial.ERROR, Trial.TERMINATED]:
            return
        elif trial.status in [Trial.PENDING, Trial.PAUSED]:
            self._scheduler_alg.on_trial_remove(self, trial)
        else:
            self._scheduler_alg.on_trial_complete(self, trial, result(100, 10))

        trial.status = Trial.ERROR if error else Trial.TERMINATED

    def add_trial(self, trial):
        self.trials.append(trial)
        self._scheduler_alg.on_trial_add(self, trial)

    def get_trials(self):
        return self.trials

    def get_live_trials(self):
        return {t for t in self.trials if t.status != Trial.TERMINATED}

    def _launch_trial(self, trial):
        trial.status = Trial.RUNNING

    def _set_trial_status(self, trial, status):
        trial.status = status

    def start_trial(self, trial, checkpoint_obj=None, train=True):
        trial.logger_running = True
        if checkpoint_obj:
            trial.restored_checkpoint = checkpoint_obj.dir_or_data
        trial.status = Trial.RUNNING
        return True

    def _schedule_trial_restore(self, trial):
        pass

    def _schedule_trial_save(self, trial, result=None):
        return _FakeFutureResult(
            _TrainingResult(
                checkpoint=Checkpoint.from_directory(trial.trainable_name),
                metrics=result,
            )
        )


class HyperbandSuite(unittest.TestCase):
    def setUp(self):
        ray.init(object_store_memory=int(1e8))

        register_mock_trainable()

    def tearDown(self):
        ray.shutdown()

    def schedulerSetup(self, num_trials, max_t=81, **kwargs):
        """Setup a scheduler and Runner with max Iter = 9.

        Bracketing is placed as follows:
        (5, 81);
        (8, 27) -> (3, 54);
        (15, 9) -> (5, 27) -> (2, 45);
        (34, 3) -> (12, 9) -> (4, 27) -> (2, 42);
        (81, 1) -> (27, 3) -> (9, 9) -> (3, 27) -> (1, 41);"""
        sched = HyperBandScheduler(
            metric="episode_reward_mean", mode="max", max_t=max_t, **kwargs
        )
        runner = _MockTrialRunner(sched)
        for i in range(num_trials):
            t = Trial(MOCK_TRAINABLE_NAME, trial_id=f"ft_{i:04d}", stub=True)
            runner.add_trial(t)
        return sched, runner

    def default_statistics(self):
        """Default statistics for HyperBand."""
        sched = HyperBandScheduler()
        res = {
            str(s): {"n": sched._get_n0(s), "r": sched._get_r0(s)}  # noqa
            for s in range(sched._s_max_1)
        }
        res["max_trials"] = sum(v["n"] for v in res.values())
        res["brack_count"] = sched._s_max_1
        res["s_max"] = sched._s_max_1 - 1
        return res

    def downscale(self, n, sched):
        return int(np.ceil(n / sched._eta))

    def basicSetup(self):
        """Setup and verify full band."""

        stats = self.default_statistics()
        sched, _ = self.schedulerSetup(stats["max_trials"])

        self.assertEqual(len(sched._hyperbands), 1)
        self.assertEqual(sched._cur_band_filled(), True)

        filled_band = sched._hyperbands[0]
        for bracket in filled_band:
            self.assertEqual(bracket.filled(), True)
        return sched

    def advancedSetup(self):
        sched = self.basicSetup()
        for i in range(4):
            t = Trial(MOCK_TRAINABLE_NAME)
            sched.on_trial_add(None, t)

        self.assertEqual(sched._cur_band_filled(), False)

        unfilled_band = sched._hyperbands[-1]
        self.assertEqual(len(unfilled_band), 2)
        bracket = unfilled_band[-1]
        self.assertEqual(bracket.filled(), False)
        self.assertEqual(len(bracket.current_trials()), 7)

        return sched

    def testConfigSameEta(self):
        sched = HyperBandScheduler(metric="episode_reward_mean", mode="max")
        i = 0
        while not sched._cur_band_filled():
            t = Trial(MOCK_TRAINABLE_NAME)
            sched.on_trial_add(None, t)
            i += 1
        self.assertEqual(len(sched._hyperbands[0]), 5)
        self.assertEqual(sched._hyperbands[0][0]._n, 5)
        self.assertEqual(sched._hyperbands[0][0]._r, 81)
        self.assertEqual(sched._hyperbands[0][-1]._n, 81)
        self.assertEqual(sched._hyperbands[0][-1]._r, 1)

        reduction_factor = 10
        sched = HyperBandScheduler(
            metric="episode_reward_mean",
            mode="max",
            max_t=1000,
            reduction_factor=reduction_factor,
        )
        i = 0
        while not sched._cur_band_filled():
            t = Trial(MOCK_TRAINABLE_NAME)
            sched.on_trial_add(None, t)
            i += 1
        self.assertEqual(len(sched._hyperbands[0]), 4)
        self.assertEqual(sched._hyperbands[0][0]._n, 4)
        self.assertEqual(sched._hyperbands[0][0]._r, 1000)
        self.assertEqual(sched._hyperbands[0][-1]._n, 1000)
        self.assertEqual(sched._hyperbands[0][-1]._r, 1)

    def testConfigSameEtaSmall(self):
        sched = HyperBandScheduler(metric="episode_reward_mean", mode="max", max_t=1)
        i = 0
        while len(sched._hyperbands) < 2:
            t = Trial(MOCK_TRAINABLE_NAME)
            sched.on_trial_add(None, t)
            i += 1
        self.assertEqual(len(sched._hyperbands[0]), 1)

    def testSuccessiveHalving(self):
        """Setup full band, then iterate through last bracket (n=81)
        to make sure successive halving is correct."""

        stats = self.default_statistics()
        sched, mock_runner = self.schedulerSetup(stats["max_trials"])
        big_bracket = sched._state["bracket"]
        cur_units = stats[str(stats["s_max"])]["r"]
        # The last bracket will downscale 4 times
        for x in range(stats["brack_count"] - 1):
            trials = big_bracket.current_trials()
            current_length = len(trials)
            for trl in trials:
                mock_runner._launch_trial(trl)

            # Provides results from 0 to 8 in order, keeping last one running
            for i, trl in enumerate(trials):
                action = sched.on_trial_result(mock_runner, trl, result(cur_units, i))
                if i < current_length - 1:
                    self.assertEqual(action, TrialScheduler.PAUSE)
                mock_runner.process_action(trl, action)

            self.assertEqual(action, TrialScheduler.CONTINUE)
            new_length = len(big_bracket.current_trials())
            self.assertEqual(new_length, self.downscale(current_length, sched))
            cur_units = int(cur_units * sched._eta)
        self.assertEqual(len(big_bracket.current_trials()), 1)

    def testHalvingStop(self):
        stats = self.default_statistics()
        num_trials = stats[str(0)]["n"] + stats[str(1)]["n"]
        sched, mock_runner = self.schedulerSetup(num_trials)
        big_bracket = sched._state["bracket"]
        for trl in big_bracket.current_trials():
            mock_runner._launch_trial(trl)

        # # Provides result in reverse order, killing the last one
        cur_units = stats[str(1)]["r"]
        for i, trl in reversed(list(enumerate(big_bracket.current_trials()))):
            action = sched.on_trial_result(mock_runner, trl, result(cur_units, i))
            mock_runner.process_action(trl, action)

        self.assertEqual(action, TrialScheduler.STOP)

    def testStopsLastOne(self):
        stats = self.default_statistics()
        num_trials = stats[str(0)]["n"]  # setup one bracket
        sched, mock_runner = self.schedulerSetup(num_trials)
        big_bracket = sched._state["bracket"]
        for trl in big_bracket.current_trials():
            mock_runner._launch_trial(trl)

        # # Provides result in reverse order, killing the last one
        cur_units = stats[str(0)]["r"]
        for i, trl in enumerate(big_bracket.current_trials()):
            action = sched.on_trial_result(mock_runner, trl, result(cur_units, i))
            mock_runner.process_action(trl, action)

        self.assertEqual(action, TrialScheduler.STOP)

    def testTrialErrored(self):
        """If a trial errored, make sure successive halving still happens"""

        stats = self.default_statistics()
        trial_count = stats[str(0)]["n"] + 3
        sched, mock_runner = self.schedulerSetup(trial_count)
        t1, t2, t3 = sched._state["bracket"].current_trials()
        for t in [t1, t2, t3]:
            mock_runner._launch_trial(t)

        sched.on_trial_error(mock_runner, t3)
        self.assertEqual(
            TrialScheduler.PAUSE,
            sched.on_trial_result(mock_runner, t1, result(stats[str(1)]["r"], 10)),
        )
        self.assertEqual(
            TrialScheduler.CONTINUE,
            sched.on_trial_result(mock_runner, t2, result(stats[str(1)]["r"], 10)),
        )

    def testTrialErrored2(self):
        """Check successive halving happened even when last trial failed"""
        stats = self.default_statistics()
        trial_count = stats[str(0)]["n"] + stats[str(1)]["n"]
        sched, mock_runner = self.schedulerSetup(trial_count)
        trials = sched._state["bracket"].current_trials()
        for t in trials[:-1]:
            mock_runner._launch_trial(t)
            sched.on_trial_result(mock_runner, t, result(stats[str(1)]["r"], 10))

        mock_runner._launch_trial(trials[-1])
        sched.on_trial_error(mock_runner, trials[-1])
        self.assertEqual(
            len(sched._state["bracket"].current_trials()),
            self.downscale(stats[str(1)]["n"], sched),
        )

    def testTrialEndedEarly(self):
        """Check successive halving happened even when one trial failed"""
        stats = self.default_statistics()
        trial_count = stats[str(0)]["n"] + 3
        sched, mock_runner = self.schedulerSetup(trial_count)

        t1, t2, t3 = sched._state["bracket"].current_trials()
        for t in [t1, t2, t3]:
            mock_runner._launch_trial(t)

        sched.on_trial_complete(mock_runner, t3, result(1, 12))
        self.assertEqual(
            TrialScheduler.PAUSE,
            sched.on_trial_result(mock_runner, t1, result(stats[str(1)]["r"], 10)),
        )
        self.assertEqual(
            TrialScheduler.CONTINUE,
            sched.on_trial_result(mock_runner, t2, result(stats[str(1)]["r"], 10)),
        )

    def testTrialEndedEarly2(self):
        """Check successive halving happened even when last trial failed"""
        stats = self.default_statistics()
        trial_count = stats[str(0)]["n"] + stats[str(1)]["n"]
        sched, mock_runner = self.schedulerSetup(trial_count)
        trials = sched._state["bracket"].current_trials()
        for t in trials[:-1]:
            mock_runner._launch_trial(t)
            sched.on_trial_result(mock_runner, t, result(stats[str(1)]["r"], 10))

        mock_runner._launch_trial(trials[-1])
        sched.on_trial_complete(mock_runner, trials[-1], result(100, 12))
        self.assertEqual(
            len(sched._state["bracket"].current_trials()),
            self.downscale(stats[str(1)]["n"], sched),
        )

    def testAddAfterHalving(self):
        stats = self.default_statistics()
        trial_count = stats[str(0)]["n"] + 1
        sched, mock_runner = self.schedulerSetup(trial_count)
        bracket_trials = sched._state["bracket"].current_trials()
        init_units = stats[str(1)]["r"]

        for t in bracket_trials:
            mock_runner._launch_trial(t)

        for i, t in enumerate(bracket_trials):
            action = sched.on_trial_result(mock_runner, t, result(init_units, i))
        self.assertEqual(action, TrialScheduler.CONTINUE)
        t = Trial(MOCK_TRAINABLE_NAME)
        sched.on_trial_add(None, t)
        mock_runner._launch_trial(t)
        self.assertEqual(len(sched._state["bracket"].current_trials()), 2)

        # Make sure that newly added trial gets fair computation (not just 1)
        self.assertEqual(
            TrialScheduler.CONTINUE,
            sched.on_trial_result(mock_runner, t, result(init_units, 12)),
        )
        new_units = init_units + int(init_units * sched._eta)
        self.assertEqual(
            TrialScheduler.PAUSE,
            sched.on_trial_result(mock_runner, t, result(new_units, 12)),
        )

    def _test_metrics(self, result_func, metric, mode):
        sched = HyperBandScheduler(time_attr="time_total_s", metric=metric, mode=mode)
        stats = self.default_statistics()

        for i in range(stats["max_trials"]):
            t = Trial(MOCK_TRAINABLE_NAME)
            sched.on_trial_add(None, t)
        runner = _MockTrialRunner(sched)

        big_bracket = sched._hyperbands[0][-1]

        for trl in big_bracket.current_trials():
            runner._launch_trial(trl)
        current_length = len(big_bracket.current_trials())

        # Provides results from 0 to 8 in order, keeping the last one running
        for i, trl in enumerate(big_bracket.current_trials()):
            action = sched.on_trial_result(runner, trl, result_func(1, i))
            runner.process_action(trl, action)

        new_length = len(big_bracket.current_trials())
        self.assertEqual(action, TrialScheduler.CONTINUE)
        self.assertEqual(new_length, self.downscale(current_length, sched))

    def testAlternateMetrics(self):
        """Checking that alternate metrics will pass."""

        def result2(t, rew):
            return dict(time_total_s=t, neg_mean_loss=rew)

        self._test_metrics(result2, "neg_mean_loss", "max")

    def testAlternateMetricsMin(self):
        """Checking that alternate metrics will pass."""

        def result2(t, rew):
            return dict(time_total_s=t, mean_loss=-rew)

        self._test_metrics(result2, "mean_loss", "min")

    def testJumpingTime(self):
        sched, mock_runner = self.schedulerSetup(81)
        big_bracket = sched._hyperbands[0][-1]

        for trl in big_bracket.current_trials():
            mock_runner._launch_trial(trl)

        # Provides results from 0 to 8 in order, keeping the last one running
        main_trials = big_bracket.current_trials()[:-1]
        jump = big_bracket.current_trials()[-1]
        for i, trl in enumerate(main_trials):
            action = sched.on_trial_result(mock_runner, trl, result(1, i))
            mock_runner.process_action(trl, action)

        action = sched.on_trial_result(mock_runner, jump, result(4, i))
        self.assertEqual(action, TrialScheduler.PAUSE)

        current_length = len(big_bracket.current_trials())
        self.assertLess(current_length, 27)

    def testRemove(self):
        """Test with 4: start 1, remove 1 pending, add 2, remove 1 pending."""
        sched, runner = self.schedulerSetup(4)
        trials = sorted(sched._trial_info, key=lambda t: t.trial_id)
        runner._launch_trial(trials[0])
        sched.on_trial_result(runner, trials[0], result(1, 5))
        self.assertEqual(trials[0].status, Trial.RUNNING)
        self.assertEqual(trials[1].status, Trial.PENDING)

        bracket, _ = sched._trial_info[trials[1]]
        self.assertTrue(trials[1] in bracket._live_trials)
        sched.on_trial_remove(runner, trials[1])
        self.assertFalse(trials[1] in bracket._live_trials)

        for i in range(2):
            trial = Trial(MOCK_TRAINABLE_NAME)
            sched.on_trial_add(None, trial)

        bracket, _ = sched._trial_info[trial]
        self.assertTrue(trial in bracket._live_trials)
        sched.on_trial_remove(runner, trial)  # where trial is not running
        self.assertFalse(trial in bracket._live_trials)

    def testFilterNoneBracket(self):
        sched, runner = self.schedulerSetup(100, 20)
        # "sched" should contains None brackets
        non_brackets = [
            b for hyperband in sched._hyperbands for b in hyperband if b is None
        ]
        self.assertTrue(non_brackets)
        # Make sure "choose_trial_to_run" still works
        trial = sched.choose_trial_to_run(runner)
        self.assertIsNotNone(trial)

    def testSmallMaxTStop(self, stop_last_trials=True):
        """Assert that trials are stopped after max_t is reached or
        continued if `stop_last_trials=False`."""
        sched, runner = self.schedulerSetup(
            num_trials=8, max_t=8, reduction_factor=2, stop_last_trials=stop_last_trials
        )
        trials = runner.get_trials()

        for trial in trials:
            runner.start_trial(trial)

        def _result(trial, timestep, reward):
            action = sched.on_trial_result(
                runner,
                trial,
                {"training_iteration": timestep, "episode_reward_mean": reward},
            )
            runner.process_action(trial, action)

        def _execute_delayed_actions():
            for hb in sched._hyperbands:
                for b in hb:
                    for t in b.trials_to_unpause:
                        runner.start_trial(t)

        # Trials of the first bracket (s=0).
        # These don't halve.
        _result(trials[0], timestep=4, reward=10)
        _result(trials[1], timestep=4, reward=20)
        _result(trials[2], timestep=4, reward=30)
        _result(trials[3], timestep=4, reward=40)

        assert trials[0].status == Trial.RUNNING
        assert trials[1].status == Trial.RUNNING
        assert trials[2].status == Trial.RUNNING
        assert trials[3].status == Trial.RUNNING

        # Trials of the second bracket (s=1).
        # These halve after 4 timesteps.
        _result(trials[4], timestep=4, reward=10)
        _result(trials[5], timestep=4, reward=20)
        _result(trials[6], timestep=4, reward=30)
        _result(trials[7], timestep=4, reward=40)

        _execute_delayed_actions()

        assert trials[4].status == Trial.TERMINATED
        assert trials[5].status == Trial.TERMINATED
        assert trials[6].status == Trial.RUNNING
        assert trials[7].status == Trial.RUNNING

        # First bracket. The trials will be terminated if stop_last_trials=True
        # and continue otherwise.
        _result(trials[0], timestep=8, reward=10)
        _result(trials[1], timestep=8, reward=20)
        _result(trials[2], timestep=8, reward=30)
        _result(trials[3], timestep=8, reward=40)

        _execute_delayed_actions()

        if stop_last_trials:
            assert trials[0].status == Trial.TERMINATED
            assert trials[1].status == Trial.TERMINATED
            assert trials[2].status == Trial.TERMINATED
            assert trials[3].status == Trial.TERMINATED
        else:
            assert trials[0].status == Trial.RUNNING
            assert trials[1].status == Trial.RUNNING
            assert trials[2].status == Trial.RUNNING
            assert trials[3].status == Trial.RUNNING

        # Second bracket
        _result(trials[6], timestep=8, reward=30)
        _result(trials[7], timestep=8, reward=40)

        _execute_delayed_actions()

        if stop_last_trials:
            assert trials[6].status == Trial.TERMINATED
            assert trials[7].status == Trial.TERMINATED
        else:
            assert trials[6].status == Trial.RUNNING
            assert trials[7].status == Trial.RUNNING

    def testSmallMaxTContinue(self):
        self.testSmallMaxTStop(stop_last_trials=False)

    def testSmallMaxTOverstepStop(self, stop_last_trials=True):
        """Test that when trials report timesteps > max_t early, they are
        stopped correctly.
        """
        sched, runner = self.schedulerSetup(
            num_trials=8, max_t=8, reduction_factor=2, stop_last_trials=stop_last_trials
        )
        trials = runner.get_trials()

        for trial in trials:
            runner.start_trial(trial)

        def _result(trial, timestep, reward):
            action = sched.on_trial_result(
                runner,
                trial,
                {"training_iteration": timestep, "episode_reward_mean": reward},
            )
            runner.process_action(trial, action)

        def _execute_delayed_actions():
            for hb in sched._hyperbands:
                for b in hb:
                    for t in b.trials_to_unpause:
                        runner.start_trial(t)

        # Trials of the first bracket (s=0).
        # These don't halve.
        _result(trials[0], timestep=4, reward=10)
        _result(trials[1], timestep=4, reward=20)
        _result(trials[2], timestep=4, reward=30)
        _result(trials[3], timestep=4, reward=40)

        assert trials[0].status == Trial.RUNNING
        assert trials[1].status == Trial.RUNNING
        assert trials[2].status == Trial.RUNNING
        assert trials[3].status == Trial.RUNNING

        # Trials of the second bracket (s=1).
        # These halve after 4 timesteps.
        # ATTN: Here we report timestep=8. This means after the first halving, the
        # bracket is actually finished, as the trials already progressed very far.
        # This can e.g. happen if a non-iteration timestep is manually reported
        _result(trials[4], timestep=8, reward=10)
        _result(trials[5], timestep=8, reward=20)
        _result(trials[6], timestep=8, reward=30)
        _result(trials[7], timestep=8, reward=40)

        _execute_delayed_actions()

        assert trials[4].status == Trial.TERMINATED
        assert trials[5].status == Trial.TERMINATED

        if stop_last_trials:
            assert trials[6].status == Trial.TERMINATED
            assert trials[7].status == Trial.TERMINATED
        else:
            assert trials[6].status == Trial.RUNNING
            assert trials[7].status == Trial.RUNNING

    def testSmallMaxTOverstepContinue(self, stop_last_trials=True):
        self.testSmallMaxTOverstepStop(stop_last_trials=False)


class BOHBSuite(unittest.TestCase):
    def setUp(self):
        ray.init(object_store_memory=int(1e8))

        register_mock_trainable()

    def tearDown(self):
        ray.shutdown()

    def testLargestBracketFirst(self):
        sched = HyperBandForBOHB(
            metric="episode_reward_mean", mode="max", max_t=3, reduction_factor=3
        )
        runner = _MockTrialRunner(sched)
        for i in range(3):
            t = Trial(MOCK_TRAINABLE_NAME)
            sched.on_trial_add(runner, t)
            runner._launch_trial(t)

        self.assertEqual(sched.state()["num_brackets"], 1)
        sched.on_trial_add(runner, Trial(MOCK_TRAINABLE_NAME))
        self.assertEqual(sched.state()["num_brackets"], 2)

    def testCheckTrialInfoUpdate(self):
        def result(score, ts):
            return {"episode_reward_mean": score, TRAINING_ITERATION: ts}

        sched = HyperBandForBOHB(
            metric="episode_reward_mean", mode="max", max_t=3, reduction_factor=3
        )
        runner = _MockTrialRunner(sched)
        runner.search_alg = MagicMock()
        runner.search_alg.searcher = MagicMock()
        trials = [Trial(MOCK_TRAINABLE_NAME) for i in range(3)]
        for t in trials:
            runner.add_trial(t)
            runner._launch_trial(t)

        for trial, trial_result in zip(trials, [result(1, 1), result(2, 1)]):
            decision = sched.on_trial_result(runner, trial, trial_result)
            self.assertEqual(decision, TrialScheduler.PAUSE)
            runner.pause_trial(trial)
        spy_result = result(0, 1)
        decision = sched.on_trial_result(runner, trials[-1], spy_result)
        self.assertEqual(decision, TrialScheduler.STOP)
        sched.choose_trial_to_run(runner)
        self.assertEqual(runner.search_alg.searcher.on_pause.call_count, 2)
        self.assertEqual(runner.search_alg.searcher.on_unpause.call_count, 1)
        self.assertTrue("hyperband_info" in spy_result)
        self.assertEqual(spy_result["hyperband_info"]["budget"], 1)

    def testCheckTrialInfoUpdateMin(self):
        def result(score, ts):
            return {"episode_reward_mean": score, TRAINING_ITERATION: ts}

        sched = HyperBandForBOHB(
            metric="episode_reward_mean", mode="min", max_t=3, reduction_factor=3
        )
        runner = _MockTrialRunner(sched)
        runner.search_alg = MagicMock()
        runner.search_alg.searcher = MagicMock()
        trials = [Trial(MOCK_TRAINABLE_NAME) for i in range(3)]
        for t in trials:
            runner.add_trial(t)
            runner._launch_trial(t)

        for trial, trial_result in zip(trials, [result(1, 1), result(2, 1)]):
            decision = sched.on_trial_result(runner, trial, trial_result)
            self.assertEqual(decision, TrialScheduler.PAUSE)
            runner.pause_trial(trial)
        spy_result = result(0, 1)
        decision = sched.on_trial_result(runner, trials[-1], spy_result)
        self.assertEqual(decision, TrialScheduler.CONTINUE)
        sched.choose_trial_to_run(runner)
        self.assertEqual(runner.search_alg.searcher.on_pause.call_count, 2)
        self.assertTrue("hyperband_info" in spy_result)
        self.assertEqual(spy_result["hyperband_info"]["budget"], 1)

    def testPauseResumeChooseTrial(self):
        def result(score, ts):
            return {"episode_reward_mean": score, TRAINING_ITERATION: ts}

        sched = HyperBandForBOHB(
            metric="episode_reward_mean", mode="min", max_t=10, reduction_factor=3
        )
        runner = _MockTrialRunner(sched)
        runner.search_alg = MagicMock()
        runner.search_alg.searcher = MagicMock()
        trials = [Trial(MOCK_TRAINABLE_NAME) for i in range(3)]
        for t in trials:
            runner.add_trial(t)
            runner._launch_trial(t)

        all_results = [result(1, 5), result(2, 1), result(3, 5)]
        for trial, trial_result in zip(trials, all_results):
            decision = sched.on_trial_result(runner, trial, trial_result)
            self.assertEqual(decision, TrialScheduler.PAUSE)
            runner.pause_trial(trial)

        run_trial = sched.choose_trial_to_run(runner)
        self.assertEqual(run_trial, trials[1])
        self.assertSequenceEqual(
            [t.status for t in trials], [Trial.PAUSED, Trial.PAUSED, Trial.PAUSED]
        )

    @pytest.mark.skipif(
        sys.version_info >= (3, 12), reason="BOHB doesn't support py312"
    )
    def testNonstopBOHB(self):
        from ray.tune.search.bohb import TuneBOHB

        def train_fn(cfg):
            start = 0
            if tune.get_checkpoint():
                with tune.get_checkpoint().as_directory() as checkpoint_dir:
                    with open(os.path.join(checkpoint_dir, "checkpoint")) as f:
                        start = int(f.read())

            for i in range(start, 200):

                time.sleep(0.1)

                with tempfile.TemporaryDirectory() as checkpoint_dir:
                    with open(os.path.join(checkpoint_dir, "checkpoint"), "w") as f:
                        f.write(str(i))
                    tune.report(
                        dict(episode_reward_mean=i),
                        checkpoint=Checkpoint.from_directory(checkpoint_dir),
                    )

        config = {"test_variable": tune.uniform(0, 20)}
        sched = HyperBandForBOHB(max_t=10, reduction_factor=3, stop_last_trials=False)
        alg = ConcurrencyLimiter(TuneBOHB(), 4)
        analysis = tune.run(
            train_fn,
            scheduler=sched,
            search_alg=alg,
            stop={"training_iteration": 32},
            num_samples=20,
            config=config,
            metric="episode_reward_mean",
            mode="min",
            verbose=1,
            fail_fast="raise",
        )
        counter = Counter(
            t.run_metadata.last_result.get("training_iteration")
            for t in analysis.trials
        )
        assert 32 in counter
        assert counter[32] > 1

    def testBOHBProcessing(self):
        trials = [Trial("foo", stub=True) for i in range(5)]
        bohb = HyperBandForBOHB(max_t=10, metric="metric", mode="max")

        for trial in trials:
            bohb.on_trial_add(None, trial)
            trial.status = Trial.RUNNING

        mock = MagicMock()

        bohb.on_trial_result(mock, trials[0], {"training_iteration": 10, "metric": 40})
        trials[0].status = Trial.PAUSED
        bohb.on_trial_result(mock, trials[1], {"training_iteration": 10, "metric": 30})
        trials[1].status = Trial.PAUSED
        bohb.on_trial_result(mock, trials[2], {"training_iteration": 10, "metric": 20})
        trials[2].status = Trial.PAUSED
        bohb.on_trial_result(mock, trials[3], {"training_iteration": 10, "metric": 10})
        trials[3].status = Trial.PAUSED
        bohb.on_trial_result(mock, trials[4], {"training_iteration": 10, "metric": 0})
        trials[4].status = Trial.PAUSED

        def set_status(trial, status):
            trial.status = status
            return None

        def stop_trial(trial):
            # See TrialRunner.stop_trial()
            if trial.status in [Trial.PENDING, Trial.PAUSED]:
                bohb.on_trial_remove(mock, trial)
                trial.status = Trial.TERMINATED
            return None

        mock._set_trial_status.side_effect = set_status
        mock.stop_trial.side_effect = stop_trial

        assert not bohb._hyperbands[0][0].is_being_processed
        bohb.choose_trial_to_run(mock, allow_recurse=False)
        assert bohb._hyperbands[0][0].is_being_processed


class _MockTrial(Trial):
    def __init__(self, i, config, storage):
        self.trainable_name = "trial_{}".format(i)
        self.trial_id = str(i)
        self.config = config
        self.experiment_tag = "{}tag".format(i)
        self.trial_name_creator = None
        self.logger_running = False
        self._restored_checkpoint = None
        self._restore_checkpoint_result = None
        self.placement_group_factory = PlacementGroupFactory([{"CPU": 1}])
        self.custom_trial_name = None
        self.custom_dirname = None
        self._legacy_local_experiment_path = None
        self.relative_logdir = None
        self._default_result_or_future = None
        self.run_metadata = _TrainingRunMetadata()
        self.run_metadata.checkpoint_manager = _CheckpointManager(
            checkpoint_config=CheckpointConfig(
                num_to_keep=2,
                checkpoint_score_attribute="episode_reward_mean",
            ),
        )
        self.temporary_state = _TemporaryTrialState()
        self.storage = storage

    @property
    def restored_checkpoint(self):
        if hasattr(self.run_metadata.checkpoint_manager, "_latest_checkpoint_result"):
            result = self.run_metadata.checkpoint_manager._latest_checkpoint_result
            return result.checkpoint.path
        return self._restored_checkpoint


class PopulationBasedTestingSuite(unittest.TestCase):
    def setUp(self):
        ray.init(num_cpus=2)

        register_mock_trainable()

    def tearDown(self):
        ray.shutdown()

    # Helper function to call pbt.on_trial_result and assert decision,
    # or trial status upon existing.
    # Need to have the `trial` in `RUNNING` status first.
    def on_trial_result(self, pbt, runner, trial, result, expected_decision=None):
        trial.status = Trial.RUNNING
        decision = pbt.on_trial_result(runner, trial, result)
        if expected_decision is None:
            pass
        elif expected_decision == TrialScheduler.PAUSE:
            self.assertTrue(
                trial.status == Trial.PAUSED or decision == expected_decision
            )
        elif expected_decision == TrialScheduler.CONTINUE:
            self.assertEqual(decision, expected_decision)
        return decision

    def basicSetup(
        self,
        num_trials=5,
        resample_prob=0.0,
        explore=None,
        perturbation_interval=10,
        log_config=False,
        require_attrs=True,
        hyperparams=None,
        hyperparam_mutations=None,
        step_once=True,
        synch=False,
    ):
        hyperparam_mutations = hyperparam_mutations or {
            "float_factor": lambda: 100.0,
            "int_factor": lambda: 10,
            "id_factor": [100],
        }
        pbt = PopulationBasedTraining(
            time_attr="training_iteration",
            metric="episode_reward_mean",
            mode="max",
            perturbation_interval=perturbation_interval,
            resample_probability=resample_prob,
            quantile_fraction=0.25,
            hyperparam_mutations=hyperparam_mutations,
            custom_explore_fn=explore,
            log_config=log_config,
            synch=synch,
            require_attrs=require_attrs,
        )
        tmpdir = tempfile.mkdtemp()
        self.storage = StorageContext(
            storage_path=tmpdir, experiment_dir_name="test_trial_scheduler"
        )
        runner = _MockTrialRunner(pbt)
        for i in range(num_trials):
            trial_hyperparams = hyperparams or {
                "float_factor": 2.0,
                "const_factor": 3,
                "int_factor": 10,
                "id_factor": i,
            }
            trial = _MockTrial(i, trial_hyperparams, self.storage)
            trial.init_local_path()
            runner.add_trial(trial)
            trial.status = Trial.RUNNING
        for i in range(num_trials):
            trial = runner.trials[i]
            if step_once:
                if synch:
                    self.on_trial_result(
                        pbt,
                        runner,
                        trial,
                        result(10, 50 * i),
                        expected_decision=TrialScheduler.PAUSE,
                    )
                else:
                    self.on_trial_result(
                        pbt,
                        runner,
                        trial,
                        result(10, 50 * i),
                        expected_decision=TrialScheduler.CONTINUE,
                    )
        pbt.reset_stats()
        return pbt, runner

    def testSearchError(self):
        pbt, runner = self.basicSetup(num_trials=0)

        def mock_train(config):
            return 1

        with self.assertRaises(ValueError):
            tune.run(
                mock_train, config={"x": 1}, scheduler=pbt, search_alg=_MockSearcher()
            )

    def testMetricError(self):
        pbt, runner = self.basicSetup()
        trials = runner.get_trials()

        # Should error if training_iteration not in result dict.
        with self.assertRaises(RuntimeError):
            self.on_trial_result(
                pbt, runner, trials[0], result={"episode_reward_mean": 4}
            )

        # Should error if episode_reward_mean not in result dict.
        with self.assertRaises(RuntimeError):
            self.on_trial_result(
                pbt,
                runner,
                trials[0],
                result={"random_metric": 10, "training_iteration": 20},
            )

    def testMetricLog(self):
        pbt, runner = self.basicSetup(require_attrs=False)
        trials = runner.get_trials()

        # Should not error if training_iteration not in result dict
        with self.assertLogs("ray.tune.schedulers.pbt", level="WARN"):
            self.on_trial_result(
                pbt, runner, trials[0], result={"episode_reward_mean": 4}
            )

        # Should not error if episode_reward_mean not in result dict.
        with self.assertLogs("ray.tune.schedulers.pbt", level="WARN"):
            self.on_trial_result(
                pbt,
                runner,
                trials[0],
                result={"random_metric": 10, "training_iteration": 20},
            )

    def testCheckpointsMostPromisingTrials(self):
        pbt, runner = self.basicSetup()
        trials = runner.get_trials()

        # no checkpoint: haven't hit next perturbation interval yet
        self.assertEqual(pbt.last_scores(trials), [0, 50, 100, 150, 200])
        self.on_trial_result(
            pbt, runner, trials[0], result(15, 200), TrialScheduler.CONTINUE
        )
        self.assertEqual(pbt.last_scores(trials), [0, 50, 100, 150, 200])
        self.assertEqual(pbt._num_checkpoints, 0)

        # checkpoint: both past interval and upper quantile
        self.on_trial_result(
            pbt, runner, trials[0], result(20, 200), TrialScheduler.CONTINUE
        )
        self.assertEqual(pbt.last_scores(trials), [200, 50, 100, 150, 200])
        self.assertEqual(pbt._num_checkpoints, 1)
        self.on_trial_result(
            pbt, runner, trials[1], result(30, 201), TrialScheduler.CONTINUE
        )
        self.assertEqual(pbt.last_scores(trials), [200, 201, 100, 150, 200])
        self.assertEqual(pbt._num_checkpoints, 2)

        # not upper quantile any more
        self.on_trial_result(
            pbt, runner, trials[4], result(30, 199), TrialScheduler.CONTINUE
        )
        self.assertEqual(pbt._num_checkpoints, 2)
        self.assertEqual(pbt._num_perturbations, 0)

    def testCheckpointMostPromisingTrialsSynch(self):
        pbt, runner = self.basicSetup(synch=True)
        trials = runner.get_trials()

        # no checkpoint: haven't hit next perturbation interval yet
        self.assertEqual(pbt.last_scores(trials), [0, 50, 100, 150, 200])
        self.on_trial_result(
            pbt, runner, trials[0], result(15, 200), TrialScheduler.CONTINUE
        )
        self.assertEqual(pbt.last_scores(trials), [0, 50, 100, 150, 200])
        self.assertEqual(pbt._num_checkpoints, 0)

        # trials should be paused until all trials are synced.
        for i in range(len(trials) - 1):
            self.on_trial_result(
                pbt, runner, trials[i], result(20, 200 + i), TrialScheduler.PAUSE
            )

        self.assertEqual(pbt.last_scores(trials), [200, 201, 202, 203, 200])
        self.assertEqual(pbt._num_checkpoints, 0)

        self.on_trial_result(
            pbt, runner, trials[-1], result(20, 204), TrialScheduler.PAUSE
        )
        self.assertEqual(pbt._num_checkpoints, 2)

    def testPerturbsLowPerformingTrials(self):
        pbt, runner = self.basicSetup()
        trials = runner.get_trials()

        # no perturbation: haven't hit next perturbation interval
        self.on_trial_result(
            pbt, runner, trials[0], result(15, -100), TrialScheduler.CONTINUE
        )
        self.assertEqual(pbt.last_scores(trials), [0, 50, 100, 150, 200])
        self.assertTrue("@perturbed" not in trials[0].experiment_tag)
        self.assertEqual(pbt._num_perturbations, 0)

        # perturb since it's lower quantile
        self.on_trial_result(
            pbt, runner, trials[0], result(20, -100), TrialScheduler.PAUSE
        )
        self.assertEqual(pbt.last_scores(trials), [-100, 50, 100, 150, 200])
        self.assertTrue("@perturbed" in trials[0].experiment_tag)
        self.assertIn(trials[0].restored_checkpoint, ["trial_3", "trial_4"])
        self.assertEqual(pbt._num_perturbations, 1)

        # also perturbed
        self.on_trial_result(
            pbt, runner, trials[2], result(20, 40), TrialScheduler.PAUSE
        )
        self.assertEqual(pbt.last_scores(trials), [-100, 50, 40, 150, 200])
        self.assertEqual(pbt._num_perturbations, 2)
        self.assertIn(trials[0].restored_checkpoint, ["trial_3", "trial_4"])
        self.assertTrue("@perturbed" in trials[2].experiment_tag)

    def testPerturbsLowPerformingTrialsSynch(self):
        pbt, runner = self.basicSetup(synch=True)
        trials = runner.get_trials()

        # no perturbation: haven't hit next perturbation interval
        self.on_trial_result(
            pbt, runner, trials[-1], result(15, -100), TrialScheduler.CONTINUE
        )
        self.assertEqual(pbt.last_scores(trials), [0, 50, 100, 150, 200])
        self.assertTrue("@perturbed" not in trials[-1].experiment_tag)
        self.assertEqual(pbt._num_perturbations, 0)

        # Don't perturb until all trials are synched.
        self.on_trial_result(
            pbt, runner, trials[-1], result(20, -100), TrialScheduler.PAUSE
        )
        self.assertEqual(pbt.last_scores(trials), [0, 50, 100, 150, -100])
        self.assertTrue("@perturbed" not in trials[-1].experiment_tag)

        # Synch all trials.
        for i in range(len(trials) - 1):
            self.on_trial_result(
                pbt, runner, trials[i], result(20, -10 * i), TrialScheduler.PAUSE
            )
        self.assertEqual(pbt.last_scores(trials), [0, -10, -20, -30, -100])
        self.assertIn(trials[-1].restored_checkpoint, ["trial_0", "trial_1"])
        self.assertIn(trials[-2].restored_checkpoint, ["trial_0", "trial_1"])
        self.assertEqual(pbt._num_perturbations, 2)

    def testPerturbWithoutResample(self):
        pbt, runner = self.basicSetup(resample_prob=0.0)
        trials = runner.get_trials()
        self.on_trial_result(
            pbt, runner, trials[0], result(20, -100), TrialScheduler.PAUSE
        )
        self.assertIn(trials[0].restored_checkpoint, ["trial_3", "trial_4"])
        self.assertIn(trials[0].config["id_factor"], [100])
        self.assertIn(trials[0].config["float_factor"], [2.4, 1.6])
        self.assertEqual(type(trials[0].config["float_factor"]), float)
        self.assertIn(trials[0].config["int_factor"], [8, 12])
        self.assertEqual(type(trials[0].config["int_factor"]), int)
        self.assertEqual(trials[0].config["const_factor"], 3)

    def testPerturbWithResample(self):
        pbt, runner = self.basicSetup(resample_prob=1.0)
        trials = runner.get_trials()
        self.on_trial_result(
            pbt, runner, trials[0], result(20, -100), TrialScheduler.PAUSE
        )
        self.assertEqual(trials[0].status, Trial.PAUSED)
        self.assertIn(trials[0].restored_checkpoint, ["trial_3", "trial_4"])
        self.assertEqual(trials[0].config["id_factor"], 100)
        self.assertEqual(trials[0].config["float_factor"], 100.0)
        self.assertEqual(type(trials[0].config["float_factor"]), float)
        self.assertEqual(trials[0].config["int_factor"], 10)
        self.assertEqual(type(trials[0].config["int_factor"]), int)
        self.assertEqual(trials[0].config["const_factor"], 3)

    def testTuneSamplePrimitives(self):
        pbt, runner = self.basicSetup(
            resample_prob=1.0,
            hyperparam_mutations={
                "float_factor": lambda: 100.0,
                "int_factor": lambda: 10,
                "id_factor": tune.choice([100]),
            },
        )
        trials = runner.get_trials()
        self.on_trial_result(
            pbt, runner, trials[0], result(20, -100), TrialScheduler.PAUSE
        )
        self.assertIn(trials[0].restored_checkpoint, ["trial_3", "trial_4"])
        self.assertEqual(trials[0].config["id_factor"], 100)
        self.assertEqual(trials[0].config["float_factor"], 100.0)
        self.assertEqual(type(trials[0].config["float_factor"]), float)
        self.assertEqual(trials[0].config["int_factor"], 10)
        self.assertEqual(type(trials[0].config["int_factor"]), int)
        self.assertEqual(trials[0].config["const_factor"], 3)

    def testTuneSampleFromError(self):
        with self.assertRaises(ValueError):
            pbt, runner = self.basicSetup(
                hyperparam_mutations={"float_factor": tune.sample_from(lambda: 100.0)}
            )

    def testPerturbationValues(self):
        def assertProduces(fn, values):
            random.seed(0)
            seen = set()
            for _ in range(100):
                seen.add(fn()["v"])
            self.assertEqual(seen, values)

        def explore_fn(config, mutations, resample_probability, custom_explore_fn=None):
            if custom_explore_fn is None:
                custom_explore_fn = lambda x: x  # noqa: E731

            new_config, _ = _explore(
                config,
                mutations,
                resample_probability,
                perturbation_factors=(1.2, 0.8),
                custom_explore_fn=custom_explore_fn,
            )
            return new_config

        # Categorical case
        assertProduces(lambda: explore_fn({"v": 4}, {"v": [3, 4, 8, 10]}, 0.0), {3, 8})
        assertProduces(lambda: explore_fn({"v": 3}, {"v": [3, 4, 8, 10]}, 0.0), {3, 4})
        assertProduces(
            lambda: explore_fn({"v": 10}, {"v": [3, 4, 8, 10]}, 0.0), {8, 10}
        )
        assertProduces(
            lambda: explore_fn({"v": 7}, {"v": [3, 4, 8, 10]}, 0.0),
            {3, 4, 8, 10},
        )
        assertProduces(
            lambda: explore_fn({"v": 4}, {"v": [3, 4, 8, 10]}, 1.0),
            {3, 4, 8, 10},
        )

        # Check that tuple also works
        assertProduces(lambda: explore_fn({"v": 4}, {"v": (3, 4, 8, 10)}, 0.0), {3, 8})
        assertProduces(lambda: explore_fn({"v": 3}, {"v": (3, 4, 8, 10)}, 0.0), {3, 4})

        # Passing in an invalid types should raise an error
        with self.assertRaises(ValueError):
            explore_fn({"v": 4}, {"v": {3, 4, 8, 10}}, 0.0)
        with self.assertRaises(ValueError):
            explore_fn({"v": 4}, {"v": "invalid"}, 0.0)

        # Continuous case
        assertProduces(
            lambda: explore_fn(
                {"v": 100}, {"v": lambda: random.choice([10, 100])}, 0.0
            ),
            {80, 120},
        )
        assertProduces(
            lambda: explore_fn(
                {"v": 100.0}, {"v": lambda: random.choice([10, 100])}, 0.0
            ),
            {80.0, 120.0},
        )
        assertProduces(
            lambda: explore_fn(
                {"v": 100.0}, {"v": lambda: random.choice([10, 100])}, 1.0
            ),
            {10.0, 100.0},
        )

        def deep_add(seen, new_values):
            for k, new_value in new_values.items():
                if isinstance(new_value, dict):
                    if k not in seen:
                        seen[k] = {}
                    seen[k].update(deep_add(seen[k], new_value))
                else:
                    if k not in seen:
                        seen[k] = set()
                    seen[k].add(new_value)

            return seen

        def assertNestedProduces(fn, values):
            random.seed(0)
            seen = {}
            for _ in range(100):
                new_config = fn()
                seen = deep_add(seen, new_config)
            self.assertEqual(seen, values)

        # Nested mutation and spec
        assertNestedProduces(
            lambda: explore_fn(
                {
                    "a": {"b": 4},
                    "1": {"2": {"3": 100}},
                },
                {
                    "a": {"b": [3, 4, 8, 10]},
                    "1": {"2": {"3": lambda: random.choice([10, 100])}},
                },
                0.0,
            ),
            {
                "a": {"b": {3, 8}},
                "1": {"2": {"3": {80, 120}}},
            },
        )

        custom_explore_fn = MagicMock(side_effect=lambda x: x)

        # Nested mutation and spec
        assertNestedProduces(
            lambda: explore_fn(
                {
                    "a": {"b": 4},
                    "1": {"2": {"3": 100}},
                },
                {
                    "a": {"b": [3, 4, 8, 10]},
                    "1": {"2": {"3": lambda: random.choice([10, 100])}},
                },
                0.0,
                custom_explore_fn=custom_explore_fn,
            ),
            {
                "a": {"b": {3, 8}},
                "1": {"2": {"3": {80, 120}}},
            },
        )

        # Expect call count to be 100 because we call explore 100 times
        self.assertEqual(custom_explore_fn.call_count, 100)

    def testDictPerturbation(self):
        pbt, runner = self.basicSetup(
            resample_prob=1.0,
            hyperparams={
                "float_factor": 2.0,
                "nest": {"nest_float": 3.0},
                "int_factor": 10,
                "const_factor": 3,
            },
            hyperparam_mutations={
                "float_factor": lambda: 100.0,
                "nest": {"nest_float": lambda: 101.0},
                "int_factor": lambda: 10,
            },
        )
        trials = runner.get_trials()
        self.on_trial_result(
            pbt, runner, trials[0], result(20, -100), TrialScheduler.PAUSE
        )
        self.assertIn(trials[0].restored_checkpoint, ["trial_3", "trial_4"])
        self.assertEqual(trials[0].config["float_factor"], 100.0)
        self.assertIsInstance(trials[0].config["float_factor"], float)
        self.assertEqual(trials[0].config["int_factor"], 10)
        self.assertIsInstance(trials[0].config["int_factor"], int)
        self.assertEqual(trials[0].config["const_factor"], 3)
        self.assertEqual(trials[0].config["nest"]["nest_float"], 101.0)
        self.assertIsInstance(trials[0].config["nest"]["nest_float"], float)

    def testYieldsTimeToOtherTrials(self):
        pbt, runner = self.basicSetup()
        trials = runner.get_trials()
        trials[0].status = Trial.PENDING  # simulate not enough resources
        self.on_trial_result(
            pbt, runner, trials[1], result(20, 1000), TrialScheduler.PAUSE
        )
        self.assertEqual(pbt.last_scores(trials), [0, 1000, 100, 150, 200])
        self.assertEqual(pbt.choose_trial_to_run(runner), trials[0])

    def testSchedulesMostBehindTrialToRun(self):
        pbt, runner = self.basicSetup()
        trials = runner.get_trials()
        self.on_trial_result(pbt, runner, trials[0], result(800, 1000))
        self.on_trial_result(pbt, runner, trials[1], result(700, 1001))
        self.on_trial_result(pbt, runner, trials[2], result(600, 1002))
        self.on_trial_result(pbt, runner, trials[3], result(500, 1003))
        self.on_trial_result(pbt, runner, trials[4], result(700, 1004))
        self.assertEqual(pbt.choose_trial_to_run(runner), None)
        for i in range(5):
            trials[i].status = Trial.PENDING
        self.assertEqual(pbt.choose_trial_to_run(runner), trials[3])

    def testSchedulesMostBehindTrialToRunSynch(self):
        pbt, runner = self.basicSetup(synch=True)
        trials = runner.get_trials()
        runner.process_action(
            trials[0], self.on_trial_result(pbt, runner, trials[0], result(800, 1000))
        )
        runner.process_action(
            trials[1], self.on_trial_result(pbt, runner, trials[1], result(700, 1001))
        )
        runner.process_action(
            trials[2], self.on_trial_result(pbt, runner, trials[2], result(600, 1002))
        )
        runner.process_action(
            trials[3], self.on_trial_result(pbt, runner, trials[3], result(500, 1003))
        )
        runner.process_action(
            trials[4], self.on_trial_result(pbt, runner, trials[4], result(700, 1004))
        )
        self.assertIn(
            pbt.choose_trial_to_run(runner), [trials[0], trials[1], trials[3]]
        )

    def testPerturbationResetsLastPerturbTime(self):
        pbt, runner = self.basicSetup()
        trials = runner.get_trials()
        self.on_trial_result(pbt, runner, trials[0], result(10000, 1005))
        self.on_trial_result(pbt, runner, trials[1], result(10000, 1004))
        self.on_trial_result(pbt, runner, trials[2], result(600, 1003))
        self.assertEqual(pbt._num_perturbations, 0)
        self.on_trial_result(pbt, runner, trials[3], result(500, 1002))
        self.assertEqual(pbt._num_perturbations, 1)
        self.on_trial_result(pbt, runner, trials[3], result(600, 100))
        self.assertEqual(pbt._num_perturbations, 1)
        self.on_trial_result(pbt, runner, trials[3], result(11000, 100))
        self.assertEqual(pbt._num_perturbations, 2)

    def testLogConfig(self):
        def check_policy(policy):
            self.assertIsInstance(policy[2], int)
            self.assertIsInstance(policy[3], int)
            self.assertIn(policy[0], ["0tag", "2tag", "3tag", "4tag"])
            self.assertIn(policy[1], ["0tag", "2tag", "3tag", "4tag"])
            self.assertIn(policy[2], [0, 2, 3, 4])
            self.assertIn(policy[3], [0, 2, 3, 4])
            for i in [4, 5]:
                self.assertIsInstance(policy[i], dict)
                for key in ["const_factor", "int_factor", "float_factor", "id_factor"]:
                    self.assertIn(key, policy[i])
                self.assertIsInstance(policy[i]["float_factor"], float)
                self.assertIsInstance(policy[i]["int_factor"], int)
                self.assertIn(policy[i]["const_factor"], [3])
                self.assertIn(policy[i]["int_factor"], [8, 10, 12])
                self.assertIn(policy[i]["float_factor"], [2.4, 2, 1.6])
                self.assertIn(policy[i]["id_factor"], [3, 4, 100])

        pbt, runner = self.basicSetup(log_config=True)
        trials = runner.get_trials()
        for i, trial in enumerate(trials):
            trial.run_metadata.last_result = {TRAINING_ITERATION: i}
        self.on_trial_result(pbt, runner, trials[0], result(15, -100))
        self.on_trial_result(pbt, runner, trials[0], result(20, -100))
        self.on_trial_result(pbt, runner, trials[2], result(20, 40))
        log_files = ["pbt_global.txt", "pbt_policy_0.txt", "pbt_policy_2.txt"]
        for log_file in log_files:
            self.assertTrue(
                os.path.exists(
                    os.path.join(self.storage.experiment_driver_staging_path, log_file)
                )
            )
            raw_policy = open(
                os.path.join(self.storage.experiment_driver_staging_path, log_file), "r"
            ).readlines()
            for line in raw_policy:
                check_policy(json.loads(line))

    def testLogConfigSynch(self):
        def check_policy(policy):
            self.assertIsInstance(policy[2], int)
            self.assertIsInstance(policy[3], int)
            self.assertIn(policy[0], ["0tag", "1tag"])
            self.assertIn(policy[1], ["3tag", "4tag"])
            self.assertIn(policy[2], [0, 1])
            self.assertIn(policy[3], [3, 4])
            for i in [4, 5]:
                self.assertIsInstance(policy[i], dict)
                for key in ["const_factor", "int_factor", "float_factor", "id_factor"]:
                    self.assertIn(key, policy[i])
                self.assertIsInstance(policy[i]["float_factor"], float)
                self.assertIsInstance(policy[i]["int_factor"], int)
                self.assertIn(policy[i]["const_factor"], [3])
                self.assertIn(policy[i]["int_factor"], [8, 10, 12])
                self.assertIn(policy[i]["float_factor"], [2.4, 2, 1.6])
                self.assertIn(policy[i]["id_factor"], [3, 4, 100])

        pbt, runner = self.basicSetup(log_config=True, synch=True, step_once=False)
        trials = runner.get_trials()
        for i, trial in enumerate(trials):
            trial.run_metadata.last_result = {TRAINING_ITERATION: i}
            self.on_trial_result(pbt, runner, trials[i], result(10, i))
        log_files = ["pbt_global.txt", "pbt_policy_0.txt", "pbt_policy_1.txt"]
        for log_file in log_files:
            self.assertTrue(
                os.path.exists(
                    os.path.join(self.storage.experiment_driver_staging_path, log_file)
                )
            )
            raw_policy = open(
                os.path.join(self.storage.experiment_driver_staging_path, log_file), "r"
            ).readlines()
            for line in raw_policy:
                check_policy(json.loads(line))

    def testReplay(self):
        # Returns unique increasing parameter mutations
        class _Counter:
            def __init__(self, start=0):
                self.count = start - 1

            def __call__(self, *args, **kwargs):
                self.count += 1
                return self.count

        pbt, runner = self.basicSetup(
            num_trials=4,
            perturbation_interval=5,
            log_config=True,
            step_once=False,
            synch=False,
            hyperparam_mutations={
                "float_factor": lambda: 100.0,
                "int_factor": _Counter(1000),
            },
        )
        trials = runner.get_trials()

        # Internal trial state to collect the real PBT history
        class _TrialState:
            def __init__(self, config):
                self.step = 0
                self.config = config
                self.history = []

            def forward(self, t):
                while self.step < t:
                    self.history.append(self.config)
                    self.step += 1

        trial_state = []
        for i, trial in enumerate(trials):
            trial.run_metadata.last_result = {TRAINING_ITERATION: 0}
            trial_state.append(_TrialState(trial.config))

        # Helper function to simulate stepping trial k a number of steps,
        # and reporting a score at the end
        def trial_step(k, steps, score):
            res = result(trial_state[k].step + steps, score)

            trials[k].run_metadata.last_result = res
            trial_state[k].forward(res[TRAINING_ITERATION])

            old_config = trials[k].config
            self.on_trial_result(pbt, runner, trials[k], res)
            new_config = trials[k].config
            trial_state[k].config = new_config.copy()

            if old_config != new_config:
                # Copy history from source trial
                source = -1
                for m, cand in enumerate(trials):
                    if cand.trainable_name == trials[k].restored_checkpoint:
                        source = m
                        break
                assert source >= 0
                trial_state[k].history = trial_state[source].history.copy()
                trial_state[k].step = trial_state[source].step

        # Initial steps
        trial_step(0, 10, 0)
        trial_step(1, 11, 10)
        trial_step(2, 12, 0)
        trial_step(3, 13, 0)

        # Next block
        trial_step(0, 10, -10)  # 0 <-- 1, new_t=11
        trial_step(2, 8, -20)  # 2 <-- 1, new_t=11
        trial_step(3, 9, 0)
        trial_step(1, 7, 0)

        # Next block
        trial_step(1, 12, 0)
        trial_step(2, 13, 0)
        trial_step(3, 14, 10)
        trial_step(0, 11, 0)  # 0 <-- 3, new_t=13+9+14=36

        # Next block
        trial_step(0, 6, 20)
        trial_step(3, 9, -40)  # 3 <-- 0, new_t=42
        trial_step(2, 8, -50)  # 2 <-- 0, new_t=42
        trial_step(1, 7, 30)
        trial_step(2, 8, -60)  # 2 <-- 1, new_t=37

        # Next block
        trial_step(0, 10, 0)
        trial_step(1, 10, 0)
        trial_step(2, 10, 0)
        trial_step(3, 10, 0)

        # Playback trainable to collect configs at each step
        class Playback(Trainable):
            def setup(self, config):
                self.config = config
                self.replayed = []
                self.iter = 0

            def step(self):
                self.iter += 1
                self.replayed.append(self.config)
                return {
                    "reward": 0,
                    "done": False,
                    "replayed": self.replayed,
                    TRAINING_ITERATION: self.iter,
                }

            def save_checkpoint(self, checkpoint_dir):
                path = os.path.join(checkpoint_dir, "checkpoint")
                with open(path, "w") as f:
                    f.write(json.dumps({"iter": self.iter, "replayed": self.replayed}))

            def load_checkpoint(self, checkpoint_dir):
                path = os.path.join(checkpoint_dir, "checkpoint")
                with open(path, "r") as f:
                    checkpoint_json = json.loads(f.read())
                    self.iter = checkpoint_json["iter"]
                    self.replayed = checkpoint_json["replayed"]

        # Loop through all trials and check if PBT history is the
        # same as the playback history
        for i, trial in enumerate(trials):
            if trial.trial_id == "1":  # Did not exploit anything
                continue

            replay = PopulationBasedTrainingReplay(
                os.path.join(
                    self.storage.experiment_driver_staging_path,
                    "pbt_policy_{}.txt".format(trial.trial_id),
                )
            )
            analysis = tune.run(
                Playback,
                scheduler=replay,
                stop={TRAINING_ITERATION: trial_state[i].step},
            )

            replayed = analysis.trials[0].run_metadata.last_result["replayed"]
            self.assertSequenceEqual(trial_state[i].history, replayed)

        # Trial 1 did not exploit anything and should raise an error
        with self.assertRaises(ValueError):
            replay = PopulationBasedTrainingReplay(
                os.path.join(
                    self.storage.experiment_driver_staging_path,
                    "pbt_policy_{}.txt".format(trials[1].trial_id),
                )
            )
            tune.run(
                Playback,
                scheduler=replay,
                stop={TRAINING_ITERATION: trial_state[1].step},
            )

    @unittest.skip("Pausing is now a multi-step action. This test needs refactoring.")
    def testReplaySynch(self):
        # Returns unique increasing parameter mutations
        class _Counter:
            def __init__(self, start=0):
                self.count = start - 1

            def __call__(self, *args, **kwargs):
                self.count += 1
                return self.count

        pbt, runner = self.basicSetup(
            num_trials=4,
            perturbation_interval=5,
            log_config=True,
            step_once=False,
            synch=True,
            hyperparam_mutations={
                "float_factor": lambda: 100.0,
                "int_factor": _Counter(1000),
            },
        )
        trials = runner.get_trials()
        tmpdir = tempfile.mkdtemp()

        # Internal trial state to collect the real PBT history
        class _TrialState:
            def __init__(self, config):
                self.step = 0
                self.config = config
                self.history = []

            def forward(self, t):
                while self.step < t:
                    self.history.append(self.config)
                    self.step += 1

        trial_state = []
        for i, trial in enumerate(trials):
            trial.run_metadata.last_result = {TRAINING_ITERATION: 0}
            trial_state.append(_TrialState(trial.config))

        # Helper function to simulate stepping trial k a number of steps,
        # and reporting a score at the end
        def trial_step(k, steps, score, synced=False):
            res = result(trial_state[k].step + steps, score)

            trials[k].run_metadata.last_result = res
            trial_state[k].forward(res[TRAINING_ITERATION])

            if not synced:
                action = self.on_trial_result(pbt, runner, trials[k], res)
                runner.process_action(trials[k], action)
                return
            else:
                # Reached synchronization point
                old_configs = [trial.config for trial in trials]
                action = self.on_trial_result(pbt, runner, trials[k], res)
                runner.process_action(trials[k], action)
                new_configs = [trial.config for trial in trials]

                for i in range(len(trials)):
                    old_config = old_configs[i]
                    new_config = new_configs[i]
                    if old_config != new_config:
                        # Copy history from source trial
                        source = -1
                        for m, cand in enumerate(trials):
                            if cand.trainable_name == trials[i].restored_checkpoint:
                                source = m
                                break
                        assert source >= 0
                        trial_state[i].history = trial_state[source].history.copy()
                        trial_state[i].step = trial_state[source].step
                        trial_state[i].config = new_config.copy()

        # Initial steps
        trial_step(0, 10, 0)
        trial_step(1, 11, 10)
        trial_step(2, 12, 0)
        trial_step(3, 13, -1, synced=True)

        # 3 <-- 1, new_t 11
        # next_perturb_sync = 13

        # Next block
        trial_step(0, 17, -10)  # 20
        trial_step(2, 15, -20)  # 20
        trial_step(3, 16, 0)  # 20
        trial_step(1, 7, 1, synced=True)  # 18

        # 2 <-- 1, new_t=11+7=18
        # next_perturb_sync = 20

        # Next block
        trial_step(2, 13, 0)  # 31
        trial_step(3, 14, 10)  # 34
        trial_step(0, 11, -1)  # 31
        trial_step(1, 12, 0, synced=True)  # 30

        # 0 <-- 3, new_t=11+9+14=34
        # next_perturb_sync = 34

        # Next block
        trial_step(0, 6, 20)  # 40
        trial_step(3, 9, -40)  # 43
        trial_step(2, 8, -50)  # 39
        trial_step(1, 7, 30, synced=True)  # 37

        # 2 <-- 1, new_t=18+13+8=37
        # next_perturb_sync = 43

        # Playback trainable to collect configs at each step
        class Playback(Trainable):
            def setup(self, config):
                self.config = config
                self.replayed = []
                self.iter = 0

            def step(self):
                self.iter += 1
                self.replayed.append(self.config)
                return {
                    "reward": 0,
                    "done": False,
                    "replayed": self.replayed,
                    TRAINING_ITERATION: self.iter,
                }

            def save_checkpoint(self, checkpoint_dir):
                path = os.path.join(checkpoint_dir, "checkpoint")
                with open(path, "w") as f:
                    f.write(json.dumps({"iter": self.iter, "replayed": self.replayed}))

            def load_checkpoint(self, checkpoint_dir):
                path = os.path.join(checkpoint_dir, "checkpoint")
                with open(path, "r") as f:
                    checkpoint_json = json.loads(f.read())
                    self.iter = checkpoint_json["iter"]
                    self.replayed = checkpoint_json["replayed"]

        # Loop through all trials and check if PBT history is the
        # same as the playback history
        for i, trial in enumerate(trials):
            if trial.trial_id in ["1"]:  # Did not exploit anything
                continue

            replay = PopulationBasedTrainingReplay(
                os.path.join(tmpdir, "pbt_policy_{}.txt".format(trial.trial_id))
            )
            analysis = tune.run(
                Playback,
                scheduler=replay,
                stop={TRAINING_ITERATION: trial_state[i].step},
            )

            replayed = analysis.trials[0].run_metadata.last_result["replayed"]
            self.assertSequenceEqual(trial_state[i].history, replayed)

        # Trial 1 did not exploit anything and should raise an error
        with self.assertRaises(ValueError):
            replay = PopulationBasedTrainingReplay(
                os.path.join(tmpdir, "pbt_policy_{}.txt".format(trials[1].trial_id))
            )
            tune.run(
                Playback,
                scheduler=replay,
                stop={TRAINING_ITERATION: trial_state[1].step},
            )

        shutil.rmtree(tmpdir)

    def testPostprocessingHook(self):
        def explore(new_config):
            new_config["id_factor"] = 42
            new_config["float_factor"] = 43
            return new_config

        pbt, runner = self.basicSetup(resample_prob=0.0, explore=explore)
        trials = runner.get_trials()
        self.on_trial_result(
            pbt, runner, trials[0], result(20, -100), TrialScheduler.PAUSE
        )
        self.assertEqual(trials[0].config["id_factor"], 42)
        self.assertEqual(trials[0].config["float_factor"], 43)

    def testFastPerturb(self):
        pbt, runner = self.basicSetup(
            perturbation_interval=1, step_once=False, log_config=True
        )
        trials = runner.get_trials()

        tmpdir = tempfile.mkdtemp()
        for i, trial in enumerate(trials):
            trial.run_metadata.last_result = {}
        self.on_trial_result(
            pbt, runner, trials[1], result(1, 10), TrialScheduler.CONTINUE
        )
        self.on_trial_result(
            pbt, runner, trials[2], result(1, 200), TrialScheduler.CONTINUE
        )
        self.assertEqual(pbt._num_checkpoints, 1)

        pbt._exploit(runner, trials[1], trials[2])
        shutil.rmtree(tmpdir)

    @pytest.mark.skip(
        reason=(
            "This test is generally flaky: The print after writing `Cleanup` "
            "to the file is printed, but the data is not always written. "
            "For some reason, this only persistently (though flaky) comes up "
            "in the new execution backend - presumably because less time "
            "passes between actor re-use. Skipping test for now."
        ),
    )
    def testContextExit(self):
        vals = [5, 1]

        class MockContext:
            def __init__(self, config):
                self.config = config
                self.active = False

            def __enter__(self):
                print("Set up resource.", self.config)
                with open("status.txt", "wt") as fp:
                    fp.write(f"Activate {self.config['x']}\n")
                print("Cleaned up.", self.config)
                self.active = True
                return self

            def __exit__(self, type, value, traceback):
                print("Clean up resource.", self.config)
                with open("status.txt", "at") as fp:
                    fp.write(f"Cleanup {self.config['x']}\n")
                print("Cleaned up.", self.config)
                self.active = False

        def train_fn(config):
            with MockContext(config):
                for i in range(10):
                    tune.report(metric=i + config["x"])

        class MockScheduler(FIFOScheduler):
            def on_trial_result(self, tune_controller, trial, result):
                return TrialScheduler.STOP

        scheduler = MockScheduler()

        out = tune.run(
            train_fn, config={"x": tune.grid_search(vals)}, scheduler=scheduler
        )

        ever_active = set()
        active = set()
        for trial in out.trials:
            with open(os.path.join(trial.local_path, "status.txt"), "rt") as fp:
                status = fp.read()
            print(f"Status for trial {trial}: {status}")
            if "Activate" in status:
                ever_active.add(trial)
                active.add(trial)
            if "Cleanup" in status:
                active.remove(trial)

        print(f"Ever active: {ever_active}")
        print(f"Still active: {active}")

        self.assertEqual(len(ever_active), len(vals))
        self.assertEqual(len(active), 0)


class E2EPopulationBasedTestingSuite(unittest.TestCase):
    def setUp(self):
        ray.init(num_cpus=4)

        register_mock_trainable()

    def tearDown(self):
        ray.shutdown()

    def basicSetup(
        self,
        resample_prob=0.0,
        explore=None,
        perturbation_interval=10,
        log_config=False,
        hyperparams=None,
        hyperparam_mutations=None,
        step_once=True,
    ):
        hyperparam_mutations = hyperparam_mutations or {
            "float_factor": lambda: 100.0,
            "int_factor": lambda: 10,
            "id_factor": [100],
        }
        pbt = PopulationBasedTraining(
            metric="mean_accuracy",
            mode="max",
            time_attr="training_iteration",
            perturbation_interval=perturbation_interval,
            resample_probability=resample_prob,
            quantile_fraction=0.25,
            hyperparam_mutations=hyperparam_mutations,
            custom_explore_fn=explore,
            log_config=log_config,
        )
        return pbt

    def testCheckpointing(self):
        pbt = self.basicSetup(perturbation_interval=10)

        class train(tune.Trainable):
            def step(self):
                return {"mean_accuracy": self.training_iteration}

            def save_checkpoint(self, path):
                checkpoint = os.path.join(path, "checkpoint")
                with open(checkpoint, "w") as f:
                    f.write("OK")

            def reset_config(self, config):
                return True

            def load_checkpoint(self, checkpoint):
                pass

        trial_hyperparams = {
            "float_factor": 2.0,
            "const_factor": 3,
            "int_factor": 10,
            "id_factor": 0,
        }

        analysis = tune.run(
            train,
            num_samples=3,
            scheduler=pbt,
            checkpoint_config=CheckpointConfig(checkpoint_frequency=3),
            config=trial_hyperparams,
            stop={"training_iteration": 30},
        )

        for trial in analysis.trials:
            self.assertEqual(trial.status, Trial.TERMINATED)
            self.assertTrue(trial.has_checkpoint())

    def testCheckpointDict(self):
        pbt = self.basicSetup(perturbation_interval=10)

        class train_dict(tune.Trainable):
            def setup(self, config):
                self.state = {"hi": 1}

            def step(self):
                return {"mean_accuracy": self.training_iteration}

            def save_checkpoint(self, path):
                return self.state

            def load_checkpoint(self, state):
                self.state = state

            def reset_config(self, config):
                return True

        trial_hyperparams = {
            "float_factor": 2.0,
            "const_factor": 3,
            "int_factor": 10,
            "id_factor": 0,
        }

        analysis = tune.run(
            train_dict,
            num_samples=3,
            scheduler=pbt,
            checkpoint_config=CheckpointConfig(checkpoint_frequency=3),
            config=trial_hyperparams,
            stop={"training_iteration": 30},
        )

        for trial in analysis.trials:
            self.assertEqual(trial.status, Trial.TERMINATED)
            self.assertTrue(trial.has_checkpoint())


class AsyncHyperBandSuite(unittest.TestCase):
    def setUp(self):
        ray.init(num_cpus=2)

        register_mock_trainable()

    def tearDown(self):
        ray.shutdown()

    def basicSetup(self, scheduler):
        t1 = Trial(MOCK_TRAINABLE_NAME)  # mean is 450, max 900, t_max=10
        t2 = Trial(MOCK_TRAINABLE_NAME)  # mean is 450, max 450, t_max=5
        scheduler.on_trial_add(None, t1)
        scheduler.on_trial_add(None, t2)
        for i in range(10):
            self.assertEqual(
                scheduler.on_trial_result(None, t1, result(i, i * 100)),
                TrialScheduler.CONTINUE,
            )
        for i in range(5):
            self.assertEqual(
                scheduler.on_trial_result(None, t2, result(i, 450)),
                TrialScheduler.CONTINUE,
            )
        return t1, t2

    def nanSetup(self, scheduler):
        t1 = Trial(MOCK_TRAINABLE_NAME)  # mean is 450, max 450, t_max=10
        t2 = Trial(MOCK_TRAINABLE_NAME)  # mean is nan, max nan, t_max=10
        scheduler.on_trial_add(None, t1)
        scheduler.on_trial_add(None, t2)
        for i in range(10):
            self.assertEqual(
                scheduler.on_trial_result(None, t1, result(i, 450)),
                TrialScheduler.CONTINUE,
            )
        for i in range(10):
            self.assertEqual(
                scheduler.on_trial_result(None, t2, result(i, np.nan)),
                TrialScheduler.CONTINUE,
            )
        return t1, t2

    def nanInfSetup(self, scheduler, runner=None):
        t1 = Trial(MOCK_TRAINABLE_NAME)
        t2 = Trial(MOCK_TRAINABLE_NAME)
        t3 = Trial(MOCK_TRAINABLE_NAME)
        scheduler.on_trial_add(runner, t1)
        scheduler.on_trial_add(runner, t2)
        scheduler.on_trial_add(runner, t3)
        for i in range(10):
            scheduler.on_trial_result(runner, t1, result(i, np.nan))
        for i in range(10):
            scheduler.on_trial_result(runner, t2, result(i, float("inf")))
        for i in range(10):
            scheduler.on_trial_result(runner, t3, result(i, float("-inf")))
        return t1, t2, t3

    def testAsyncHBOnComplete(self):
        scheduler = AsyncHyperBandScheduler(
            metric="episode_reward_mean", mode="max", max_t=10, brackets=1
        )
        t1, t2 = self.basicSetup(scheduler)
        t3 = Trial(MOCK_TRAINABLE_NAME)
        scheduler.on_trial_add(None, t3)
        scheduler.on_trial_complete(None, t3, result(10, 1000))
        self.assertEqual(
            scheduler.on_trial_result(None, t2, result(101, 0)), TrialScheduler.STOP
        )

    def testAsyncHBGracePeriod(self):
        scheduler = AsyncHyperBandScheduler(
            metric="episode_reward_mean",
            mode="max",
            grace_period=2.5,
            reduction_factor=3,
            brackets=1,
        )
        t1, t2 = self.basicSetup(scheduler)
        scheduler.on_trial_complete(None, t1, result(10, 1000))
        scheduler.on_trial_complete(None, t2, result(10, 1000))
        t3 = Trial(MOCK_TRAINABLE_NAME)
        scheduler.on_trial_add(None, t3)
        self.assertEqual(
            scheduler.on_trial_result(None, t3, result(1, 10)), TrialScheduler.CONTINUE
        )
        self.assertEqual(
            scheduler.on_trial_result(None, t3, result(2, 10)), TrialScheduler.CONTINUE
        )
        self.assertEqual(
            scheduler.on_trial_result(None, t3, result(3, 10)), TrialScheduler.STOP
        )

    def testAsyncHBAllCompletes(self):
        scheduler = AsyncHyperBandScheduler(
            metric="episode_reward_mean", mode="max", max_t=10, brackets=10
        )
        trials = [Trial(MOCK_TRAINABLE_NAME) for i in range(10)]
        for t in trials:
            scheduler.on_trial_add(None, t)

        for t in trials:
            self.assertEqual(
                scheduler.on_trial_result(None, t, result(10, -2)), TrialScheduler.STOP
            )

    def testAsyncHBUsesPercentile(self):
        scheduler = AsyncHyperBandScheduler(
            metric="episode_reward_mean",
            mode="max",
            grace_period=1,
            max_t=10,
            reduction_factor=2,
            brackets=1,
        )
        t1, t2 = self.basicSetup(scheduler)
        scheduler.on_trial_complete(None, t1, result(10, 1000))
        scheduler.on_trial_complete(None, t2, result(10, 1000))
        t3 = Trial(MOCK_TRAINABLE_NAME)
        scheduler.on_trial_add(None, t3)
        self.assertEqual(
            scheduler.on_trial_result(None, t3, result(1, 260)), TrialScheduler.STOP
        )
        self.assertEqual(
            scheduler.on_trial_result(None, t3, result(2, 260)), TrialScheduler.STOP
        )

    def testAsyncHBNanPercentile(self):
        scheduler = AsyncHyperBandScheduler(
            metric="episode_reward_mean",
            mode="max",
            grace_period=1,
            max_t=10,
            reduction_factor=2,
            brackets=1,
        )
        t1, t2 = self.nanSetup(scheduler)
        scheduler.on_trial_complete(None, t1, result(10, 450))
        scheduler.on_trial_complete(None, t2, result(10, np.nan))
        t3 = Trial(MOCK_TRAINABLE_NAME)
        scheduler.on_trial_add(None, t3)
        self.assertEqual(
            scheduler.on_trial_result(None, t3, result(1, 260)), TrialScheduler.STOP
        )
        self.assertEqual(
            scheduler.on_trial_result(None, t3, result(2, 260)), TrialScheduler.STOP
        )

    def testAsyncHBSaveRestore(self):
        _, tmpfile = tempfile.mkstemp()

        scheduler = AsyncHyperBandScheduler(
            metric="episode_reward_mean",
            mode="max",
            grace_period=1,
            max_t=10,
            reduction_factor=2,
            brackets=1,
        )

        # Add some trials
        trials = [Trial(MOCK_TRAINABLE_NAME) for i in range(10)]
        for t in trials:
            scheduler.on_trial_add(None, t)

        # Report some results
        for t in trials[0:5]:
            self.assertNotEqual(
                scheduler.on_trial_result(None, t, result(1, 10)), TrialScheduler.STOP
            )

        # Report worse result: Trial should stop
        self.assertEqual(
            scheduler.on_trial_result(None, trials[5], result(1, 5)),
            TrialScheduler.STOP,
        )

        scheduler.save(tmpfile)

        scheduler2 = AsyncHyperBandScheduler()
        scheduler2.restore(tmpfile)

        # Report a new bad result: Trial should stop
        self.assertEqual(
            scheduler2.on_trial_result(None, trials[6], result(1, 4)),
            TrialScheduler.STOP,
        )

        # Create a new trial and report bad result: Trial should stop
        # Report a new bad result: Trial should stop
        new_trial = Trial(MOCK_TRAINABLE_NAME)
        scheduler2.on_trial_add(None, new_trial)
        self.assertEqual(
            scheduler2.on_trial_result(None, new_trial, result(1, 2)),
            TrialScheduler.STOP,
        )

    def testAsyncHBNonStopTrials(self):
        trials = [Trial(MOCK_TRAINABLE_NAME) for i in range(4)]
        scheduler = AsyncHyperBandScheduler(
            metric="metric",
            mode="max",
            grace_period=1,
            max_t=3,
            reduction_factor=2,
            brackets=1,
            stop_last_trials=False,
        )
        scheduler.on_trial_add(None, trials[0])
        scheduler.on_trial_add(None, trials[1])
        scheduler.on_trial_add(None, trials[2])
        scheduler.on_trial_add(None, trials[3])

        # Report one result
        action = scheduler.on_trial_result(
            None, trials[0], {"training_iteration": 2, "metric": 10}
        )
        assert action == TrialScheduler.CONTINUE
        action = scheduler.on_trial_result(
            None, trials[1], {"training_iteration": 2, "metric": 8}
        )
        assert action == TrialScheduler.STOP
        action = scheduler.on_trial_result(
            None, trials[2], {"training_iteration": 2, "metric": 6}
        )
        assert action == TrialScheduler.STOP
        action = scheduler.on_trial_result(
            None, trials[3], {"training_iteration": 2, "metric": 4}
        )
        assert action == TrialScheduler.STOP

        # Report more. This will fail if `stop_last_trials=True`
        action = scheduler.on_trial_result(
            None, trials[0], {"training_iteration": 4, "metric": 10}
        )
        assert action == TrialScheduler.CONTINUE

        action = scheduler.on_trial_result(
            None, trials[0], {"training_iteration": 8, "metric": 10}
        )
        assert action == TrialScheduler.CONTINUE

        # Also continue if we fall below the cutoff eventually
        action = scheduler.on_trial_result(
            None, trials[0], {"training_iteration": 14, "metric": 1}
        )
        assert action == TrialScheduler.CONTINUE

    def testMedianStoppingNanInf(self):
        scheduler = MedianStoppingRule(metric="episode_reward_mean", mode="max")

        t1, t2, t3 = self.nanInfSetup(scheduler)
        scheduler.on_trial_complete(None, t1, result(10, np.nan))
        scheduler.on_trial_complete(None, t2, result(10, float("inf")))
        scheduler.on_trial_complete(None, t3, result(10, float("-inf")))

    def testHyperbandNanInf(self):
        scheduler = HyperBandScheduler(metric="episode_reward_mean", mode="max")
        t1, t2, t3 = self.nanInfSetup(scheduler)
        scheduler.on_trial_complete(None, t1, result(10, np.nan))
        scheduler.on_trial_complete(None, t2, result(10, float("inf")))
        scheduler.on_trial_complete(None, t3, result(10, float("-inf")))

    def testBOHBNanInf(self):
        scheduler = HyperBandForBOHB(metric="episode_reward_mean", mode="max")

        runner = _MockTrialRunner(scheduler)
        runner.search_alg = MagicMock()
        runner.search_alg.searcher = MagicMock()

        t1, t2, t3 = self.nanInfSetup(scheduler, runner)
        # skip trial complete in this mock setting

    def testPBTNanInf(self):
        scheduler = PopulationBasedTraining(
            metric="episode_reward_mean",
            mode="max",
            hyperparam_mutations={"ignored": [1]},
        )
        t1, t2, t3 = self.nanInfSetup(scheduler, runner=MagicMock())
        scheduler.on_trial_complete(None, t1, result(10, np.nan))
        scheduler.on_trial_complete(None, t2, result(10, float("inf")))
        scheduler.on_trial_complete(None, t3, result(10, float("-inf")))

    def _test_metrics(self, result_func, metric, mode):
        scheduler = AsyncHyperBandScheduler(
            grace_period=1,
            time_attr="training_iteration",
            metric=metric,
            mode=mode,
            brackets=1,
        )
        t1 = Trial(MOCK_TRAINABLE_NAME)  # mean is 450, max 900, t_max=10
        t2 = Trial(MOCK_TRAINABLE_NAME)  # mean is 450, max 450, t_max=5
        scheduler.on_trial_add(None, t1)
        scheduler.on_trial_add(None, t2)
        for i in range(10):
            self.assertEqual(
                scheduler.on_trial_result(None, t1, result_func(i, i * 100)),
                TrialScheduler.CONTINUE,
            )
        for i in range(5):
            self.assertEqual(
                scheduler.on_trial_result(None, t2, result_func(i, 450)),
                TrialScheduler.CONTINUE,
            )
        scheduler.on_trial_complete(None, t1, result_func(10, 1000))
        self.assertEqual(
            scheduler.on_trial_result(None, t2, result_func(5, 450)),
            TrialScheduler.CONTINUE,
        )
        self.assertEqual(
            scheduler.on_trial_result(None, t2, result_func(6, 0)),
            TrialScheduler.CONTINUE,
        )

    def testAlternateMetrics(self):
        def result2(t, rew):
            return dict(training_iteration=t, neg_mean_loss=rew)

        self._test_metrics(result2, "neg_mean_loss", "max")

    def testAlternateMetricsMin(self):
        def result2(t, rew):
            return dict(training_iteration=t, mean_loss=-rew)

        self._test_metrics(result2, "mean_loss", "min")

    def _testAnonymousMetricEndToEnd(self, scheduler_cls, searcher=None):
        def train_fn(config):
            return config["value"]

        out = tune.run(
            train_fn,
            mode="max",
            num_samples=1,
            config={"value": tune.uniform(-2.0, 2.0)},
            scheduler=scheduler_cls(),
            search_alg=searcher,
        )

        self.assertTrue(bool(out.best_trial))

    def testAnonymousMetricEndToEndFIFO(self):
        self._testAnonymousMetricEndToEnd(FIFOScheduler)

    def testAnonymousMetricEndToEndASHA(self):
        self._testAnonymousMetricEndToEnd(AsyncHyperBandScheduler)

    @pytest.mark.skipif(
        sys.version_info >= (3, 12), reason="BOHB doesn't support py312"
    )
    def testAnonymousMetricEndToEndBOHB(self):
        from ray.tune.search.bohb import TuneBOHB

        self._testAnonymousMetricEndToEnd(HyperBandForBOHB, TuneBOHB())

    def testAnonymousMetricEndToEndMedian(self):
        self._testAnonymousMetricEndToEnd(MedianStoppingRule)

    def testAnonymousMetricEndToEndPBT(self):
        self._testAnonymousMetricEndToEnd(
            lambda: PopulationBasedTraining(hyperparam_mutations={"ignored": [1]})
        )


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))

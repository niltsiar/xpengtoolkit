#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the silence watchdog's escalation policy. Standard library only.

    python3 tests/test_watchdog_policy.py
    python3 -m unittest discover -s tests -v

The escalation exists because a reconnect plus a passive query does not always
clear XPENG's stalled cloud fanout — measured 2026-10-06, 11h31m of silence
survived ~66 of them, and one force-upload fixed it in 1.2 seconds. But a
force-upload WAKES THE CAR, so what is tested here is mostly restraint: the
interesting assertions are the ones that say "no".
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys
import unittest

# Loaded by path, not imported as `custom_components.xpeng.watchdog_policy`:
# that would execute the package's __init__, which imports Home Assistant, which
# is not installed here (and must not need to be). The module itself is
# deliberately free of HA imports — that is the point of it existing.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402

_MODULE_PATH = _component.module_path("watchdog_policy.py")
_spec = importlib.util.spec_from_file_location("watchdog_policy", _MODULE_PATH)
assert _spec and _spec.loader
policy = importlib.util.module_from_spec(_spec)
sys.modules["watchdog_policy"] = policy
_spec.loader.exec_module(policy)

HOUR = 3600.0
MINUTE = 60.0


def _arms(*args, **kwargs):
    """A motion arm is allowed when ``motion_refusal`` names no refusal."""
    return policy.motion_refusal(*args, **kwargs) is None


class TestBounds(unittest.TestCase):
    def test_the_documented_bounds_are_the_ones_used(self):
        """These are what the operator was told; a silent change is a bug."""
        self.assertEqual(policy.ESCALATE_AFTER_SECONDS, 30 * MINUTE)
        self.assertEqual(policy.ESCALATE_BACKOFF_SECONDS, 12 * HOUR)

    def test_the_backoff_is_longer_than_the_patience(self):
        """Otherwise the bound would be the silence rule, not the wake rate."""
        self.assertGreater(
            policy.ESCALATE_BACKOFF_SECONDS, policy.ESCALATE_AFTER_SECONDS
        )

    def test_patience_clears_the_reconnect_cadence(self):
        """A reconnect gets several honest attempts before we force an upload.

        RECONNECT_BACKOFF is 600s in __init__.py; the escalation must not fire
        between two reconnects, or it would pre-empt the cheap recovery it is
        meant to be a last resort after.
        """
        self.assertGreaterEqual(policy.ESCALATE_AFTER_SECONDS, 10 * MINUTE * 3)


class TestSilenceBound(unittest.TestCase):
    def test_a_brief_silence_is_left_alone(self):
        """A parked car may legitimately say nothing for a while."""
        self.assertFalse(policy.should_escalate(10 * MINUTE, 0.0, 10_000.0))

    def test_one_second_short_of_the_bound_is_left_alone(self):
        self.assertFalse(
            policy.should_escalate(
                policy.ESCALATE_AFTER_SECONDS - 1, 0.0, 100_000.0
            )
        )

    def test_the_bound_itself_escalates(self):
        """`>=`, so the documented half hour means half an hour."""
        self.assertTrue(
            policy.should_escalate(policy.ESCALATE_AFTER_SECONDS, 0.0, 100_000.0)
        )

    def test_well_past_the_bound_escalates(self):
        self.assertTrue(policy.should_escalate(11.5 * HOUR, 0.0, 100_000.0))


class TestBackoff(unittest.TestCase):
    def test_a_second_upload_inside_the_backoff_is_refused(self):
        now = 100_000.0
        last = now - HOUR  # one hour ago
        self.assertFalse(policy.should_escalate(5 * HOUR, last, now))

    def test_one_second_short_of_the_backoff_is_refused(self):
        now = 100_000.0
        last = now - (12 * HOUR - 1)
        self.assertFalse(policy.should_escalate(20 * HOUR, last, now))

    def test_the_backoff_itself_is_allowed(self):
        now = 100_000.0
        last = now - 12 * HOUR
        self.assertTrue(policy.should_escalate(20 * HOUR, last, now))

    def test_never_escalated_does_not_block(self):
        """0.0 is "none yet", not "infinitely long ago" and not "unknown"."""
        self.assertTrue(policy.should_escalate(3 * HOUR, 0.0, 5.0))


class TestABoundedWakeRate(unittest.TestCase):
    """The behaviour that matters: what a permanently wedged fanout costs.

    A force-upload during a stall yields exactly ONE full state and then the
    silence resumes, so the policy must not treat that message as recovery. This
    simulates the worst case — the stall never ends — and asserts the wake count
    is governed by the backoff and nothing else.
    """

    @staticmethod
    def _run(hours: float):
        now = 1_000_000.0
        silence_since = now  # the last message, i.e. silence grows from here
        last_escalation = 0.0
        escalations = []
        while now < silence_since + hours * HOUR:
            silence = now - silence_since
            if policy.should_escalate(silence, last_escalation, now):
                escalations.append(now - silence_since)
                last_escalation = now
            now += 60.0  # the watchdog ticks once a minute
        return escalations

    def test_a_short_stall_wakes_once(self):
        self.assertEqual(len(self._run(1.0)), 1)

    def test_a_day_of_stall_wakes_three_times(self):
        """Half an hour, then every 12h after: 0.5h, 12.5h and 24.5h."""
        gaps = self._run(30)
        self.assertEqual(len(gaps), 3)
        self.assertAlmostEqual(gaps[0] / HOUR, 0.5, places=1)
        self.assertAlmostEqual(gaps[1] / HOUR, 12.5, places=1)
        self.assertAlmostEqual(gaps[2] / HOUR, 24.5, places=1)

    def test_a_week_of_stall_is_about_a_dozen_wakes_not_three_hundred(self):
        """Without the backoff this would be one every half hour."""
        self.assertEqual(len(self._run(7 * 24)), 14)

    def test_the_gap_between_wakes_is_never_under_the_backoff(self):
        gaps = self._run(72)
        self.assertGreaterEqual(len(gaps), 2)
        for earlier, later in zip(gaps, gaps[1:]):
            self.assertGreaterEqual(later - earlier, 12 * HOUR)


class TestRecovery(unittest.TestCase):
    def test_a_stall_after_a_healthy_stretch_escalates_promptly(self):
        """The bound is on wake RATE, not a permanent penalty.

        Once the previous escalation is older than the backoff — which any
        healthy stretch makes it — a new stall may escalate on its own merits.
        """
        now = 1_000_000.0
        last = now - (5 * 24 * HOUR)  # escalated five days ago
        self.assertTrue(policy.should_escalate(2.5 * HOUR, last, now))


class TestTheEscalationIsReachable(unittest.TestCase):
    """The policy is only as good as the tick that consults it.

    Measured 2026-10-16 on a live install: `stale` climbed past the half-hour
    bound (state_silence 7s, seconds_since_data 1864s) and `escalations` stayed
    0, because the watchdog returned on `if silence < SILENCE_THRESHOLD` before
    reaching the escalation block. A parked car answers every query with a stub,
    so that branch returned on every single tick - the escalation was
    unreachable in exactly the case it exists for.

    Checked statically: the tick lives in __init__.py, which imports Home
    Assistant.
    """

    SOURCE = (_component.component_dir() / "__init__.py").read_text(encoding="utf-8")

    def _silence_branch(self) -> ast.If:
        tree = ast.parse(self.SOURCE)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "_silence_watchdog"
            ):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.If):
                    continue
                test = sub.test
                if (
                    isinstance(test, ast.Compare)
                    and isinstance(test.left, ast.Name)
                    and test.left.id == "silence"
                    and any(isinstance(op, ast.Lt) for op in test.ops)
                ):
                    return sub
        raise AssertionError("the silence branch is gone from _silence_watchdog")

    def test_the_silence_branch_falls_through(self):
        branch = self._silence_branch()
        self.assertFalse(
            any(isinstance(stmt, ast.Return) for stmt in branch.body),
            "stepping out of the tick here skips the escalation a stubbing car needs",
        )

    def test_the_escalation_still_follows_it(self):
        """The wake is still decided in the tick, and after the branch - not before it."""
        tree = ast.parse(self.SOURCE)
        nodes = {
            node.name: ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name in ("_silence_watchdog", "_motion_arm", "_force_upload")
        }
        tick, arm = nodes["_silence_watchdog"], nodes["_motion_arm"]
        self.assertIn("sending ONE", arm, "the force-upload escalation is gone")
        self.assertIn("_force_upload(reason)", arm, "the escalation no longer sends")
        self.assertIn("send_command", nodes["_force_upload"])
        self.assertIn("_motion_arm('watchdog')", tick, "the tick no longer escalates")
        self.assertLess(
            tick.index("if silence < SILENCE_THRESHOLD"),
            tick.index("_motion_arm('watchdog')"),
            "the escalation must not sit before the silence branch",
        )


class TestTheBackoffSurvivesAReload(unittest.TestCase):
    """The 12h gap is a promise about the car, so it cannot live in memory only.

    Measured 2026-10-16: an options change rebuilt the watchdog state at 22:43, and
    the escalation fired again at 23:13 — one hour after the previous wake, not
    twelve. A wake is physical, so the clock that bounds it is stored now. Checked
    statically: the watchdog lives in __init__.py, which imports Home Assistant.
    """

    SOURCE = (_component.component_dir() / "__init__.py").read_text(encoding="utf-8")

    def test_the_clock_is_read_back_at_setup(self):
        self.assertIn("_WATCHDOG_STORE_VERSION", self.SOURCE)
        self.assertIn("watchdog_store.async_load()", self.SOURCE)
        self.assertIn(
            '"last_escalation": float(saved_watchdog.get("last_escalation") or 0.0)',
            self.SOURCE,
        )

    def test_the_clock_is_written_where_it_is_spent(self):
        save = next(
            node
            for node in ast.walk(ast.parse(self.SOURCE))
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_save_watchdog_state"
        )
        shapes = [
            sorted(k.value for k in node.keys)
            for node in ast.walk(save)
            if isinstance(node, ast.Dict)
        ]
        self.assertEqual(shapes, [["counts", "day", "last_escalation"]])
        arm = self.SOURCE[self.SOURCE.index("async def _motion_arm(") :]
        self.assertLess(
            arm.index('watchdog_state["last_escalation"] = now_ts'),
            arm.index("await _save_watchdog_state()"),
            "the clock must be stored where the wake is sent, not later",
        )
        self.assertLess(
            arm.index("await _save_watchdog_state()"),
            arm.index("await _force_upload(reason)"),
            "the clock must be stored before the send, not after it",
        )

    def test_a_broken_store_does_not_block_setup(self):
        node = ast.parse(self.SOURCE)
        for sub in ast.walk(node):
            if isinstance(sub, ast.AsyncFunctionDef) and sub.name == "async_setup_entry":
                text = ast.unparse(sub)
                self.assertIn("except Exception", text)
                self.assertIn("saved_watchdog = {}", text)
                return
        raise AssertionError("async_setup_entry not found")


class TestMotionEscalation(unittest.TestCase):
    """A moving car cannot be woken, so its recovery is not a wake decision.

    Issue #3's capture (2026-10-21): the fanout stalled mid-drive, and from 16:46
    to 17:01 the watchdog refused 16 escalations as "one per 12h" - the budget
    had been spent on a parked stall earlier that day. Telemetry stayed frozen
    for 46 minutes while the car was being driven. These tests pin the two extra
    bounds that replace the sleeping-car budget on that path, and the fact that
    the sleeping-car rule itself is untouched.
    """

    def test_the_motion_bound_is_the_documented_one(self):
        self.assertEqual(policy.MOTION_ESCALATE_BACKOFF_SECONDS, 5 * MINUTE)

    def test_it_is_shorter_than_the_sleeping_car_budget(self):
        """That difference is the whole point: the car is already awake."""
        self.assertLess(
            policy.MOTION_ESCALATE_BACKOFF_SECONDS, policy.ESCALATE_BACKOFF_SECONDS
        )

    def test_the_sleeping_car_rule_still_applies_when_the_car_is_parked(self):
        """No motion evidence, no shortcut - the 12h budget is unchanged."""
        self.assertFalse(
            _arms(
                4 * HOUR, 0.0, 100_000.0, last_motion_at=0.0
            )
        )

    def test_patience_still_applies(self):
        """Motion is a reason to skip the 12h budget, not the caller's patience."""
        now = 100_000.0
        self.assertFalse(
            _arms(
                10 * MINUTE, 0.0, now, last_motion_at=now - MINUTE
            )
        )

    def test_motion_patience_is_two_minutes_of_energy_silence(self):
        now = 100_000.0
        self.assertEqual(policy.MOTION_ESCALATE_AFTER_SECONDS, 2 * MINUTE)
        for energy_age, expected in ((119, False), (120, True), (float("inf"), True)):
            with self.subTest(energy_age=energy_age):
                self.assertEqual(
                    _arms(
                        energy_age,
                        0.0,
                        now,
                        last_motion_at=now - MINUTE,
                        escalate_after=policy.MOTION_ESCALATE_AFTER_SECONDS,
                    ),
                    expected,
                )

    def test_motion_evidence_older_than_ten_minutes_buys_nothing(self):
        now = 100_000.0
        for age, expected in ((10 * MINUTE, True), (10 * MINUTE + 1, False)):
            with self.subTest(evidence_age=age):
                self.assertEqual(
                    _arms(
                        HOUR,
                        0.0,
                        now,
                        last_motion_at=now - age,
                        escalate_after=policy.MOTION_ESCALATE_AFTER_SECONDS,
                    ),
                    expected,
                )

    def test_motion_escapes_a_budget_spent_an_hour_ago(self):
        """The exact shape of the capture: a wake an hour before, car moving."""
        now = 100_000.0
        self.assertTrue(
            _arms(
                policy.ESCALATE_AFTER_SECONDS,
                last_escalation=now - HOUR,
                now=now,
                last_motion_at=now - MINUTE,
            )
        )

    def test_one_episode_of_motion_buys_one_wake(self):
        """The evidence must be newer than the last wake, or a wedged fanout
        would escalate on the same stale evidence every tick."""
        now = 100_000.0
        self.assertFalse(
            _arms(
                2 * HOUR,
                last_escalation=now - 6 * MINUTE,
                now=now,
                last_motion_at=now - 8 * MINUTE,
            )
        )

    def test_the_same_rise_never_arms_twice(self):
        now = 100_000.0
        evidence = now - 4 * MINUTE
        first = _arms(
            HOUR, 0.0, now - 3 * MINUTE, last_motion_at=evidence
        )
        self.assertTrue(first)
        self.assertFalse(
            _arms(
                HOUR, now - 3 * MINUTE, now + 6 * MINUTE, last_motion_at=evidence
            )
        )

    def test_a_cadence_still_applies_between_motion_escalations(self):
        now = 100_000.0
        self.assertFalse(
            _arms(
                2 * HOUR,
                last_escalation=now - 4 * MINUTE,
                now=now,
                last_motion_at=now - MINUTE,
            )
        )

    def test_a_rise_after_a_burst_rearms_after_five_minutes(self):
        now = 100_000.0
        self.assertFalse(
            _arms(
                2 * HOUR,
                last_escalation=now - 5 * MINUTE + 1,
                now=now,
                last_motion_at=now - MINUTE,
            )
        )
        self.assertTrue(
            _arms(
                2 * HOUR,
                last_escalation=now - 5 * MINUTE,
                now=now,
                last_motion_at=now - MINUTE,
            )
        )

    def test_a_third_parked_wake_in_a_day_is_refused(self):
        for counts, expected in (
            ({}, True),
            ({"cold_start": 1}, True),
            ({"cold_start": 1, "stall": 1}, False),
            ({"stall": 2}, False),
        ):
            with self.subTest(counts=counts):
                self.assertEqual(policy.daily_cap_allows("stall", counts), expected)
                self.assertEqual(policy.daily_cap_allows("cold_start", counts), expected)

    def test_motion_arms_do_not_spend_the_parked_cap(self):
        self.assertTrue(policy.daily_cap_allows("stall", {"motion": 23}))
        self.assertTrue(policy.daily_cap_allows("cold_start", {"motion": 24}))
        self.assertTrue(policy.daily_cap_allows("motion", {"stall": 2, "cold_start": 2}))

    def test_motion_arms_stop_at_the_daily_cap(self):
        self.assertTrue(policy.daily_cap_allows("motion", {"motion": 23}))
        self.assertFalse(policy.daily_cap_allows("motion", {"motion": 24}))

    def test_active_and_user_force_uploads_are_only_counted(self):
        for reason in ("active", "user"):
            self.assertTrue(policy.daily_cap_allows(reason, {reason: 1000, "stall": 2, "motion": 24}))

    def test_the_counter_resets_at_local_midnight(self):
        stored = {"stall": 2, "motion": 3}
        self.assertEqual(policy.counts_for_day("2026-11-21", stored, "2026-11-21"), stored)
        self.assertEqual(policy.counts_for_day("2026-11-21", stored, "2026-11-22"), {})

    def test_a_store_from_before_the_counts_starts_the_day_at_zero(self):
        self.assertEqual(policy.counts_for_day(None, None, "2026-11-21"), {})

    def test_only_a_backoff_is_a_refusal_of_an_arm_that_is_due(self):
        now = 100_000.0
        kwargs = dict(escalate_after=policy.MOTION_ESCALATE_AFTER_SECONDS)
        self.assertEqual(policy.motion_refusal(60, 0.0, now, now - 30, **kwargs), "patience")
        self.assertEqual(policy.motion_refusal(300, 0.0, now, 0.0, **kwargs), "no_evidence")
        self.assertEqual(policy.motion_refusal(300, 0.0, now, now - 700, **kwargs), "stale_evidence")
        self.assertEqual(policy.motion_refusal(300, now - 100, now, now - 30, **kwargs), "backoff")
        self.assertEqual(policy.motion_refusal(300, now - 400, now, now - 500, **kwargs), "spent")
        self.assertIsNone(policy.motion_refusal(300, now - 400, now, now - 30, **kwargs))

    def test_the_watchdog_asks_both_questions(self):
        """Pinned at the call site: the sleeping-car rule is never replaced."""
        source = (_component.component_dir() / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("should_escalate(", source)
        self.assertIn("motion_refusal(", source)
        self.assertIn(
            "escalation_allowed = pending_escalation or motion_escalation", source
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

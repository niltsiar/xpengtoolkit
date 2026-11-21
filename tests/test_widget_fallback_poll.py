#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The fast widget fallback poll (issue #3) is built and torn down correctly.

Until 0.6.37 the only widget read rode inside the heavy REST poll, which is
parked-gated at ``parked_scan_interval`` (3600s) - so while the push stream was
wedged, a car that started driving was invisible for up to an hour. The
2026-10-27 log showed the 6-9 minute detections were the hourly poll's phase,
not the 5-minute merge gate (issue #3, comment by the reporter).

The fix is a poll that asks for the widget ALONE, at ``rest_scan_interval``,
and only while ``mqtt_energy_stale()`` says the push stream is the thing that is
missing. These pin the three properties that make that safe:

* it asks only the widget (the heavy REST poll must stay hourly while parked);
* the gate comes first, so a healthy stream costs no requests;
* it reschedules at the REST interval and is cancelled with the other timers,
  and a tick that was already running when the entry unloaded does not
  resurrect the timer.

    python3 tests/test_widget_fallback_poll.py
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402
from test_startup_watchdog import functions  # noqa: E402

COMP = _component.component_dir()
INIT = (COMP / "__init__.py").read_text(encoding="utf-8")


def _poll_body() -> str:
    start = INIT.index("async def _poll_widget_fallback(")
    end = INIT.index("\n        def cancel_widget_fallback_fn(", start)
    return INIT[start:end]


class ThePollUsesTheFallbacksOwnRules(unittest.TestCase):
    def test_it_asks_only_the_widget(self):
        body = _poll_body()
        self.assertIn("_fetch_widget_state_sync", body)
        self.assertIn('coordinator.set_rest_data({"widget_state": widget})', body)
        self.assertNotIn(
            "_fetch_rest_data_sync",
            body,
            "the heavy REST poll must stay hourly while parked",
        )

    def test_it_only_runs_while_the_stream_is_silent(self):
        body = _poll_body()
        self.assertIn("coordinator.mqtt_energy_stale(", body)
        self.assertLess(
            body.index("mqtt_energy_stale"),
            body.index("_fetch_widget_state_sync"),
            "the gate must be checked before the request",
        )

    def test_it_runs_once_energy_is_two_minutes_old(self):
        import test_energy_guard as eg

        body = _poll_body()
        self.assertIn("MOTION_ESCALATE_AFTER_SECONDS", body)
        self.assertNotIn("WIDGET_MERGE_STALE_SECONDS", body)
        policy = (COMP / "watchdog_policy.py").read_text(encoding="utf-8")
        self.assertIn("MOTION_ESCALATE_AFTER_SECONDS: float = 2 * 60", policy)
        coord = eg._coordinator_with(eg._state(60))
        for age, stale in ((60, False), (180, True)):
            with self.subTest(energy_age_s=age):
                coord.last_energy_data_timestamp = datetime.now(
                    timezone.utc
                ) - timedelta(seconds=age)
                self.assertEqual(coord.mqtt_energy_stale(120), stale)

    def test_it_runs_at_the_rest_interval_not_the_parked_one(self):
        body = _poll_body()
        self.assertIn("rest_scan_interval, _poll_widget_fallback", body)
        self.assertNotIn("parked_scan_interval, _poll_widget_fallback", body)

    def test_it_skips_until_the_vehicle_type_code_is_known(self):
        body = _poll_body()
        self.assertIn("vehicle_type_code", body)
        self.assertIn("Widget fallback poll skipped", body)

    def test_a_running_tick_cannot_resurrect_a_stopped_timer(self):
        body = _poll_body()
        self.assertIn('if not widget_timer_state["stopped"]:', body)
        start = INIT.index("def cancel_widget_fallback_fn(")
        end = INIT.index("cancel_widget_fallback = cancel_widget_fallback_fn", start)
        self.assertIn('widget_timer_state["stopped"] = True', INIT[start:end])


class TheStatusReportsTheCadenceItActuallyUses(unittest.TestCase):
    """Issue #34: the polling-status sensor read a field nobody wrote.

    `rest_poll_interval_seconds` was always 0 because `sensor.py` reads
    ``poll_status["rest_interval_seconds"]`` and no ``set_poll_status`` call
    published it. A reporter diagnosing a silent fallback ("REST API polling is
    configured to 300 seconds" against a diagnostic that said 0) had no way to
    tell a misconfiguration from a bug.
    """

    def test_the_interval_is_published(self):
        body = _poll_body()
        self.assertNotIn("rest_interval_seconds", body)
        self.assertIn("rest_interval_seconds=", INIT)

    def test_the_sensor_still_reads_it(self):
        sensor = (COMP / "sensor.py").read_text(encoding="utf-8")
        self.assertIn('"rest_poll_interval_seconds"', sensor)
        self.assertIn('"rest_interval_seconds"', sensor)


class _PollHarness(unittest.IsolatedAsyncioTestCase):
    """The real poll closure, run against a real coordinator on a fake clock."""

    NOW = 1_795_280_300.0

    def setUp(self):
        import test_energy_guard as eg

        self.eg = eg
        self.coord = eg._coordinator_with(eg._state(60))
        self.coord.last_motion_arm_at = 0.0
        self.coord.widget_token_expired = False
        self.coord.vehicle_type_code = "TYPE"
        self.coord.last_energy_data_timestamp = datetime.fromtimestamp(
            self.NOW - 600, timezone.utc
        )
        self.delays, self.merged, self.arms = [], [], []
        self.coord.set_rest_data = self.merged.append
        self.timer = {"cancel": None, "stopped": False, "busy": False}
        self.answers = []
        self.clock = patch("time.time", lambda: self.NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    async def poll(self):
        async def executor(fn, *args):
            return fn(*args)

        async def arm(trigger):
            self.arms.append(trigger)
            return True

        ns = functions("_poll_widget_fallback")
        ns.update(
            coordinator=self.coord, hass=SimpleNamespace(async_add_executor_job=executor),
            entry=object(), vin="TESTVIN", widget_raw_sink=None, rest_scan_interval=300,
            widget_timer_state=self.timer, WIDGET_FAST_POLL_SECONDS=60,
            MOTION_ESCALATE_AFTER_SECONDS=120, MOTION_EVIDENCE_MAX_AGE_SECONDS=600,
            async_call_later=lambda hass, delay, action: self.delays.append(delay),
            _fetch_widget_state_sync=lambda *args: self.answers.pop(0),
            is_token_expired_response=lambda data: isinstance(data, dict)
            and data.get("code") == 12010101,
            _motion_arm=arm,
        )
        await ns["_poll_widget_fallback"](None)

    async def test_motion_suspected_polls_every_60s(self):
        self.answers = [{"odometer": {"total_mileage": 1}}] * 2
        await self.poll()
        self.assertEqual(self.delays, [300], "an idle car keeps the all-day cadence")
        self.coord.last_motion_at = self.NOW - 30
        await self.poll()
        self.assertEqual(self.delays, [300, 60])

    async def test_the_window_stays_open_after_an_arm_and_a_stop(self):
        """The stop's P frame clears the evidence; the arm keeps the window."""
        self.answers = [{"odometer": {"total_mileage": 1}}] * 2
        self.coord.last_motion_at = 0.0
        self.coord.last_motion_arm_at = self.NOW - 590
        await self.poll()
        self.coord.last_motion_arm_at = self.NOW - 610
        await self.poll()
        self.assertEqual(self.delays, [60, 300])

    async def test_an_expired_answer_never_starts_the_rest_poll(self):
        body = _poll_body()
        for forbidden in ("_start_reauth_if_idle", "_poll_rest_data", "_refresh_creds_sync"):
            self.assertNotIn(forbidden, body)
        self.answers = [{"code": 12010101, "data": None}] * 2
        await self.poll()
        self.assertEqual(self.merged, [], "an error body is not a widget")
        self.assertFalse(self.coord.widget_token_expired, "no episode, no fast window")
        self.assertEqual(self.delays, [300])
        self.coord.last_motion_arm_at = self.NOW - 60
        await self.poll()
        self.assertTrue(self.coord.widget_token_expired)
        self.assertEqual(self.delays, [300, 60], "the window waits for the new tokens")

    async def test_a_good_read_closes_the_expired_window(self):
        self.coord.widget_token_expired = True
        self.answers = [{"odometer": {"total_mileage": 1}}]
        await self.poll()
        self.assertFalse(self.coord.widget_token_expired)
        self.assertEqual(self.delays, [300])

    async def test_new_evidence_arms_through_the_shared_closure(self):
        def merge(data):
            self.merged.append(data)
            self.coord.last_motion_at = self.NOW

        self.coord.set_rest_data = merge
        self.answers = [{"odometer": {"total_mileage": 1}}]
        await self.poll()
        self.assertEqual(self.arms, ["widget"])

    async def test_a_token_update_triggers_an_immediate_poll(self):
        cancelled, scheduled = [], []
        ns = functions("_on_tokens_updated")
        ns.update(
            widget_timer_state={"cancel": lambda: cancelled.append(1), "stopped": False, "busy": False},
            async_call_later=lambda hass, delay, action: scheduled.append(delay),
            hass=object(), _poll_widget_fallback=object(),
        )
        await ns["_on_tokens_updated"]()
        self.assertEqual((cancelled, scheduled), ([1], [0]), "one chain: the old timer is replaced")
        ns["widget_timer_state"]["busy"] = True
        await ns["_on_tokens_updated"]()
        self.assertEqual(scheduled, [0], "a poll already running reschedules itself")
        self.assertIn("tokens_updated_signal(entry.entry_id)", INIT)
        self.assertIn("async_dispatcher_connect(", INIT)
        importer = (COMP / "credential_import.py").read_text(encoding="utf-8")
        self.assertIn("async_dispatcher_send(hass, tokens_updated_signal(entry.entry_id))", importer)


class TheSingleReadIsCheap(unittest.TestCase):
    def test_it_reads_the_widget_with_the_vehicle_type_code(self):
        start = INIT.index("def _fetch_widget_state_sync(")
        end = INIT.index("\ndef _fetch_rest_data_sync(", start)
        body = INIT[start:end]
        self.assertIn(
            "get_vehicle_state(None, tenant, tokens, vin, vehicle_type_code", body
        )
        self.assertIn("_load_api_context(entry)", body)

    def test_it_does_not_refresh_tokens_itself(self):
        """A second refresher racing the credential loop is worse than a skip."""
        start = INIT.index("def _fetch_widget_state_sync(")
        end = INIT.index("\ndef _fetch_rest_data_sync(", start)
        body = INIT[start:end]
        for forbidden in ("refresh_login_tokens", "_refresh_creds_sync", "_start_reauth"):
            self.assertNotIn(forbidden, body)


class ItIsTornDownWithTheOtherTimers(unittest.TestCase):
    def test_the_cancel_handle_is_registered(self):
        self.assertIn('"cancel_widget_fallback": cancel_widget_fallback', INIT)

    def test_the_replace_path_and_the_unload_path_both_cancel_it(self):
        # One registration in the data dict plus one entry in each of the two
        # cancel loops (setup replacing a live client, and unload).
        self.assertEqual(INIT.count('"cancel_widget_fallback"'), 3)


if __name__ == "__main__":
    unittest.main()

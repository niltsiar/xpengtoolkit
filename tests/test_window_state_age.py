#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The window section's own clock.

The car does not resend its window block after a door closes, so a position
such as 93 can sit in the state for minutes after the window shut. The windows
cover and its binary sensor report how old that section is, like the lock and
trunk do for ``instant.door`` (issue #32). The clock moves for a frame that
carries its own non-empty window block and for a window command reply. A
restored state, the widget fallback and any other frame leave it alone.

    python3 tests/test_window_state_age.py
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib
import pathlib
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402
import test_command_state_merge as _csm  # noqa: E402
import test_door_state_fallback as _dsf  # noqa: E402
import test_energy_guard as _eg  # noqa: E402
import test_mqtt_state_delivery as _delivery  # noqa: E402

_component.assert_is_default_target()

door_guard = importlib.import_module("custom_components.xpeng.door_guard")


def _ago(seconds: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


class TheWindowClockMovesForRealWindowData(unittest.TestCase):
    def _frames(self):
        client, calls = _delivery._client(
            {
                "code": 200,
                "instant": {
                    "drive": {"shift_state": 4, "speed": 0},
                    "odometer": {"total_mileage": 62234},
                    "charge": {"battery_soc": 64},
                },
            }
        )
        held = {"msg_content": client._last_full_content, "vin": "TESTVIN0000000000"}
        return client, calls, _eg._coordinator_with(held)

    def test_a_partial_with_its_own_window_block_stamps_the_clock(self):
        client, calls, coord = self._frames()
        frame = _delivery._state(
            instant={
                "window": {"rear_left_window_position": 93},
                "charge": {"battery_soc": 63},
            }
        )
        client._on_message(None, None, _delivery._Msg(frame))
        self.assertTrue(calls[-1]["has_window_data"])
        self.assertTrue(coord.set_vehicle_data(calls[-1]))
        self.assertLess(_age(coord), 5)

    def test_a_full_state_with_its_own_window_block_stamps_the_clock(self):
        client, calls, coord = self._frames()
        frame = _delivery._state(
            instant={
                "ext": {"vehicleMode": 0},
                "charge": {"battery_soc": 63},
                "window": {"rear_left_window_position": 100},
            }
        )
        client._on_message(None, None, _delivery._Msg(frame))
        self.assertTrue(calls[-1]["has_window_data"])
        self.assertTrue(coord.set_vehicle_data(calls[-1]))
        self.assertLess(_age(coord), 5)

    def test_a_partial_without_a_window_block_leaves_the_clock_alone(self):
        client, calls, coord = self._frames()
        before = coord.window_data_timestamp = _ago(4 * 3600)
        frame = _delivery._state(instant={"charge": {"battery_soc": 63}})
        client._on_message(None, None, _delivery._Msg(frame))
        self.assertFalse(calls[-1]["has_window_data"])
        self.assertTrue(coord.set_vehicle_data(calls[-1]))
        self.assertEqual(coord.window_data_timestamp, before)

    def test_a_window_command_reply_stamps_the_clock(self):
        coord = _csm._coordinator_with(_csm._cached_state())
        reply = _csm._charge_ack(front_left_window_position=100)
        reply["service_type"] = 12
        self.assertTrue(coord._merge_command_state(reply))
        self.assertLess(_age(coord), 5)

    def test_a_charge_reply_leaves_the_clock_alone(self):
        coord = _csm._coordinator_with(_csm._cached_state())
        before = coord.window_data_timestamp = _ago(4 * 3600)
        self.assertTrue(
            coord._merge_command_state(_csm._charge_ack(max_range_charge=80))
        )
        self.assertEqual(coord.window_data_timestamp, before)

    def test_the_widget_fallback_never_stamps_it(self):
        held = _dsf._held()
        held["msg_content"]["instant"]["window"] = {"rear_left_window_position": 93}
        coord = _dsf._coordinator(held)
        coord.window_data_timestamp = None
        coord._merge_widget_state_if_mqtt_stale(
            _dsf._widget(window={"rear_left_window_position": 100})
        )
        self.assertIsNone(coord.window_data_timestamp)
        window = coord._last_full_data["msg_content"]["instant"]["window"]
        self.assertEqual(window["rear_left_window_position"], 93)

    def test_a_restored_state_has_no_clock(self):
        class _Store:
            async def async_load(self):
                return {
                    "data": _dsf._held(),
                    "ts": _ago(120).isoformat(),
                }

        import asyncio

        coord = _eg._coordinator_with(None)
        coord._state_store = _Store()
        asyncio.run(coord.async_load_cached())
        self.assertTrue(coord.state_is_restored)
        self.assertIsNone(coord.window_data_timestamp)


class TheAgeIsClampedAtZero(unittest.TestCase):
    def test_a_clock_that_ran_backwards_does_not_report_a_negative_age(self):
        now = datetime(2026, 11, 23, 11, 0, tzinfo=timezone.utc)
        attrs = door_guard.age_attributes(
            datetime(2026, 11, 23, 11, 5, tzinfo=timezone.utc),
            now=now,
            section="window",
        )
        self.assertEqual(attrs["window_state_age_seconds"], 0)
        self.assertEqual(
            attrs["window_state_updated_at"], "2026-11-23T11:05:00+00:00"
        )

    def test_the_door_attributes_keep_their_names(self):
        attrs = door_guard.age_attributes(_ago(60))
        self.assertEqual(
            sorted(attrs), ["door_state_age_seconds", "door_state_updated_at"]
        )


def _age(coord) -> float:
    return (datetime.now(timezone.utc) - coord.window_data_timestamp).total_seconds()


if __name__ == "__main__":
    unittest.main(verbosity=2)

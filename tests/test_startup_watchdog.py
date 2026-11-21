"""Exercise identity I/O threading and watchdog diagnostics without HA or a car."""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import logging
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _component

SOURCE = (_component.component_dir() / "__init__.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def functions(*names):
    """Execute the real function bodies without importing HA's package setup."""
    nodes = [node for node in ast.walk(TREE)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name in names]
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[
        ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    namespace = {"__package__": "custom_components.xpeng", "_LOGGER": logging.getLogger("xpeng.test")}
    exec(compile(ast.fix_missing_locations(module), "<xpeng_init>", "exec"), namespace)
    return namespace


class IdentityIOTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop_thread = threading.get_ident()
        self.reads = []
        self.writes = []
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tenant = Path(self.tmp.name) / "tenant.json"
        self.tokens = Path(self.tmp.name) / "tokens.json"
        self.api = ModuleType("custom_components.xpeng.xpeng_api")

        def read(path):
            self.assertNotEqual(threading.get_ident(), self.loop_thread)
            self.reads.append(Path(path).name)
            return json.loads(Path(path).read_text(encoding="utf-8"))

        self.api.load_tenant_cache = read
        self.api.load_login_tokens = read
        self.api.deserialize_tenant = lambda data: dict(data)
        self.api.mint_device_id = lambda: "minted-device"
        self.api.DEVICE_ID_MAX_LENGTH = 32
        self.patcher = patch.dict(sys.modules, {self.api.__name__: self.api})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.ns = functions("_available_tenants", "_tenant_device_id", "_tokens_device_id",
                            "_async_resolve_configured_device_id", "_warn_on_device_id_mismatch")
        self.ns.update(CONF_TENANT_DATA="tenant_data", CONF_TOKEN_FILE="token_file",
                       CONF_CONFIGURED_DEVICE_ID="configured_device_id")

        async def executor(fn, *args):
            return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

        def update(entry, **kwargs):
            self.assertEqual(threading.get_ident(), self.loop_thread)
            self.writes.append(kwargs)
            entry.options = kwargs["options"]

        self.hass = SimpleNamespace(async_add_executor_job=executor,
                                    config_entries=SimpleNamespace(async_update_entry=update))
        self.entry = SimpleNamespace(entry_id="test", data={}, options={"unchanged": True})

    async def resolve(self, configured=""):
        return await self.ns["_async_resolve_configured_device_id"](
            self.hass, self.entry, str(self.tenant), configured)

    async def test_tenant_read_is_off_loop_and_entry_write_is_on_loop(self):
        self.tenant.write_text(json.dumps({"deviceId": "tenant-device"}), encoding="utf-8")
        self.assertEqual(await self.resolve(), "tenant-device")
        self.assertEqual(self.reads, ["tenant.json"])
        self.assertEqual(self.entry.options, {"unchanged": True, "configured_device_id": "tenant-device"})
        self.assertEqual(len(self.writes), 1)

    async def test_existing_option_wins_and_mismatch_is_still_reported(self):
        self.tenant.write_text(json.dumps({"deviceId": "other-device"}), encoding="utf-8")
        with self.assertLogs("xpeng.test", level="WARNING") as logs:
            self.assertEqual(await self.resolve("configured-device"), "configured-device")
        self.assertIn("Device id mismatch", " ".join(logs.output))
        self.assertEqual(self.writes, [])

    async def test_entry_tenant_keeps_priority_over_file_and_tokens(self):
        self.entry.data["tenant_data"] = {"deviceId": "entry-device"}
        self.tenant.write_text(json.dumps({"deviceId": "file-device"}), encoding="utf-8")
        self.entry.options["token_file"] = str(self.tokens)
        with self.assertLogs("xpeng.test", level="WARNING"):
            self.assertEqual(await self.resolve(), "entry-device")
        self.assertEqual(self.reads, ["tenant.json"])

    async def test_token_fallback_also_reads_off_loop(self):
        self.tokens.write_text(json.dumps({"mqttAccount": {"clientId": "xmart:app:123:" + "a" * 32}}), encoding="utf-8")
        self.entry.options["token_file"] = str(self.tokens)
        self.assertEqual(await self.resolve(), "a" * 32)
        self.assertEqual(self.reads, ["tenant.json", "tokens.json"])

    async def test_missing_or_broken_files_allow_first_run_to_mint(self):
        self.entry.options["token_file"] = str(self.tokens)
        for contents in (None, "broken json"):
            if contents is not None:
                self.tenant.write_text(contents, encoding="utf-8")
                self.tokens.write_text(contents, encoding="utf-8")
            self.assertEqual(await self.resolve(), "minted-device")

    def test_setup_awaits_the_identity_resolver(self):
        setup = next(node for node in TREE.body if isinstance(node, ast.AsyncFunctionDef)
                     and node.name == "async_setup_entry")
        self.assertTrue(any(isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
                            and isinstance(node.value.func, ast.Name)
                            and node.value.func.id == "_async_resolve_configured_device_id"
                            for node in ast.walk(setup)))


class WatchdogDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    NOW = 100_000.0

    def build(self, *, parked=True, charging=False, connected=True, stale=350,
              motion_age=None, can_wake=True, park_during_save=False,
              energy_age=None, hint=False, last_reconnect_age=None):
        """The real tick and arm closures, with a car and a clock around them."""
        clock = {"now": self.NOW}
        calls = []
        ns = functions("_motion_arm", "_silence_watchdog")
        spec = importlib.util.spec_from_file_location("watchdog_test_policy", _component.module_path("watchdog_policy.py"))
        policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy)
        async def executor(fn, *args):
            return fn(*args)
        async def save(data):
            calls.append(("save", data))
            await asyncio.sleep(0)
            if park_during_save:
                ns["coordinator"].last_motion_at = 0.0
        evidence_at = self.NOW - motion_age if motion_age is not None else 0.0
        coordinator = SimpleNamespace(vehicle_parked=parked, is_charging=charging,
            last_motion_at=evidence_at, last_motion_arm_at=0.0,
            external_charging_hint=hint,
            energy_data_age_seconds=lambda: stale if energy_age is None else energy_age,
            motion_evidence_fresh=lambda max_age: bool(coordinator.last_motion_at)
            and clock["now"] - coordinator.last_motion_at <= max_age,
            command_policy=SimpleNamespace(can_wake=lambda: can_wake), set_poll_status=lambda **kwargs: None)
        ns.update(
            time=SimpleNamespace(time=lambda: clock["now"]), vin="TESTVIN", entry=object(),
            refresh_state={},
            watchdog_state={"tick_count": 0,
                "last_reconnect": self.NOW - last_reconnect_age if last_reconnect_age is not None else 0.0,
                "last_escalation": 0.0, "escalations": 0},
            SILENCE_THRESHOLD=300, RECONNECT_BACKOFF=600, PARKED_RECONNECT_BACKOFF=1800,
            ESCALATE_AFTER_SECONDS=policy.ESCALATE_AFTER_SECONDS,
            ESCALATE_BACKOFF_SECONDS=policy.ESCALATE_BACKOFF_SECONDS,
            MOTION_ESCALATE_AFTER_SECONDS=policy.MOTION_ESCALATE_AFTER_SECONDS,
            MOTION_EVIDENCE_MAX_AGE_SECONDS=policy.MOTION_EVIDENCE_MAX_AGE_SECONDS,
            should_escalate=policy.should_escalate, should_escalate_moving=policy.should_escalate_moving,
            poll_status=SimpleNamespace(is_escalated=lambda *args, **kwargs: False),
            coordinator=coordinator,
            mqtt_client=SimpleNamespace(connected=connected, last_state_msg_ts=self.NOW - 350,
                last_msg_ts=self.NOW - 350, last_meaningful_state_ts=self.NOW - stale,
                reconnect=lambda *args: calls.append(("reconnect",)) or True,
                send_query=lambda *args: calls.append(("query",)),
                send_command=lambda *args: calls.append(("command", *args))),
            hass=SimpleNamespace(async_add_executor_job=executor), watchdog_store=SimpleNamespace(async_save=save),
            _refresh_creds_sync=lambda *args: None,
        )
        return ns, calls, clock

    async def tick(self, **kwargs):
        ns, calls, _ = self.build(**kwargs)
        with self.assertLogs("xpeng.test", level="INFO") as logs:
            await ns["_silence_watchdog"](None)
        self.assertFalse(any(r.levelno >= logging.ERROR for r in logs.records))
        return logs.records, calls

    async def test_known_parked_reconnect_is_info_and_still_queries(self):
        logs, calls = await self.tick()
        recovery = [r for r in logs if "reconnecting" in r.getMessage()]
        self.assertEqual([r.levelno for r in recovery], [logging.INFO])
        self.assertIn(("reconnect",), calls)
        self.assertIn(("query",), calls)
        self.assertNotIn("cloud-side", " ".join(r.getMessage() for r in logs))

    async def test_active_unknown_and_disconnected_connections_still_warn(self):
        for kwargs in ({"parked": False}, {"parked": None}, {"charging": True}, {"connected": False}):
            logs, _ = await self.tick(**kwargs)
            self.assertTrue(any(r.levelno == logging.WARNING for r in logs), kwargs)

    async def test_recent_motion_diagnostic_acknowledges_uncertainty(self):
        logs, calls = await self.tick(stale=1860, motion_age=300)
        text = " ".join(r.getMessage() for r in logs)
        self.assertIn("motion evidence 5min old", text)
        self.assertIn("current motion is unconfirmed", text)
        self.assertNotIn("cannot wake", text)
        self.assertEqual(sum(c[0] == "command" for c in calls), 1)

    async def test_disabled_waking_logs_only_the_passive_action(self):
        logs, calls = await self.tick(stale=1860, motion_age=300, can_wake=False)
        text = " ".join(r.getMessage() for r in logs)
        self.assertNotIn("sending ONE force-upload", text)
        self.assertIn("waking is disabled", text)
        self.assertFalse(any(c[0] == "command" for c in calls))
        self.assertTrue(any(c[0] == "query" for c in calls))

    async def test_motion_age_describes_the_evidence_that_triggered_the_decision(self):
        logs, _ = await self.tick(stale=1860, motion_age=300, park_during_save=True)
        self.assertIn("motion evidence 5min old", " ".join(r.getMessage() for r in logs))

    async def test_widget_evidence_arms_without_waiting_for_a_tick(self):
        ns, calls, _ = self.build(stale=200, motion_age=30)
        with self.assertLogs("xpeng.test", level="INFO"):
            self.assertTrue(await ns["_motion_arm"]("widget"))
        self.assertEqual(sum(c[0] == "command" for c in calls), 1)
        self.assertEqual(ns["coordinator"].last_motion_arm_at, self.NOW)

    async def test_the_tick_and_the_widget_cannot_both_arm(self):
        ns, calls, _ = self.build(stale=200, motion_age=30)
        with self.assertLogs("xpeng.test", level="INFO"):
            await asyncio.gather(ns["_motion_arm"]("watchdog"), ns["_motion_arm"]("widget"))
        self.assertEqual(sum(c[0] == "command" for c in calls), 1)

    async def test_an_arm_is_not_spent_while_the_client_is_disconnected(self):
        ns, calls, _ = self.build(stale=200, motion_age=30, connected=False)
        self.assertFalse(await ns["_motion_arm"]("widget"))
        self.assertEqual(ns["watchdog_state"]["last_escalation"], 0.0)
        self.assertEqual(calls, [])

    async def test_fresh_evidence_shortens_the_disconnected_backoff(self):
        """Parked backs off 30 minutes; a car with fresh evidence is being driven."""
        _, parked_calls = await self.tick(connected=False, last_reconnect_age=700)
        self.assertNotIn(("reconnect",), parked_calls)
        _, driving_calls = await self.tick(connected=False, last_reconnect_age=700, motion_age=60)
        self.assertIn(("reconnect",), driving_calls)

    async def test_charging_hint_vetoes_motion_arming(self):
        _, calls = await self.tick(stale=200, motion_age=30, hint=True)
        self.assertFalse(any(c[0] == "command" for c in calls))
        _, calls = await self.tick(stale=200, motion_age=30, hint=False)
        self.assertTrue(any(c[0] == "command" for c in calls))

    async def test_stale_is_charging_does_not_veto_a_rise(self):
        _, calls = await self.tick(stale=200, motion_age=30, charging=True, hint=False)
        self.assertTrue(any(c[0] == "command" for c in calls))

    async def test_disabled_wake_logs_the_refused_motion_arm(self):
        ns, calls, clock = self.build(stale=200, motion_age=30, can_wake=False)
        with self.assertLogs("xpeng.test", level="DEBUG") as logs:
            await ns["_motion_arm"]("widget")
            clock["now"] += 300
            ns["coordinator"].last_motion_at = clock["now"] - 30
            await ns["_motion_arm"]("watchdog")
        warnings = [r for r in logs.records if r.levelno == logging.WARNING]
        self.assertEqual(len(warnings), 1, "one WARNING for the whole episode")
        self.assertIn("refused", warnings[0].getMessage())
        self.assertFalse(any(c[0] == "command" for c in calls))
        self.assertEqual(sum(c[0] == "query" for c in calls), 2)
        with self.assertLogs("xpeng.test", level="WARNING"):
            clock["now"] += 11 * 60
            ns["coordinator"].last_motion_at = clock["now"] - 30
            await ns["_motion_arm"]("widget")


class TheTripOf20261121(unittest.IsolatedAsyncioTestCase):
    """A wake burst ended in P, and the drive that followed was missed for km.

    Facts of the trip (UTC): the widget read odometer 29505 at 16:56:11; the
    wake burst ran 16:57:18-16:57:51 and ended with a P frame carrying its own
    drive block at 16:57:46; the widget read 29507 at 17:02 with timer 17:02:24,
    29509 at 17:07 and 29517 at 17:12. The real tick, arm and widget-poll
    closures run against the real coordinator and policy, on a fake clock.
    """

    UTC = timezone.utc

    def at(self, hour, minute, second=0):
        return datetime(2026, 11, 21, hour, minute, second, tzinfo=self.UTC).timestamp()

    async def test_trip_2026_11_21_rearms_at_the_first_rise_after_the_burst(self):
        import test_energy_guard as eg

        clock = {"now": self.at(16, 56, 11)}
        calls, delays = [], []

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(clock["now"], tz)

        coord = eg._coordinator_with(eg._state(60, odo=29505))
        coord.max_odometer = 29505
        coord.last_motion_arm_at = 0.0
        coord.widget_token_expired = False
        coord.vehicle_type_code = "TYPE"
        coord.command_policy = SimpleNamespace(can_wake=lambda: True)
        coord.set_poll_status = lambda **kwargs: None
        coord.set_rest_data = lambda data: coord._merge_widget_state_if_mqtt_stale(
            data["widget_state"]
        )
        mqtt = SimpleNamespace(
            connected=True, last_state_msg_ts=0.0, last_msg_ts=0.0,
            last_meaningful_state_ts=0.0,
            reconnect=lambda *args: True, send_query=lambda *args: None,
            send_command=lambda *args: calls.append(clock["now"]),
        )
        widgets = []
        spec = importlib.util.spec_from_file_location(
            "trip_policy", _component.module_path("watchdog_policy.py"))
        policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy)

        async def executor(fn, *args):
            return fn(*args)

        async def save(data):
            await asyncio.sleep(0)

        ns = functions("_motion_arm", "_silence_watchdog", "_poll_widget_fallback")
        ns.update(
            time=SimpleNamespace(time=lambda: clock["now"]), vin="TESTVIN", entry=object(),
            refresh_state={}, coordinator=coord, mqtt_client=mqtt,
            watchdog_state={"tick_count": 0, "last_reconnect": 0.0, "last_escalation": 0.0, "escalations": 0},
            SILENCE_THRESHOLD=300, RECONNECT_BACKOFF=600, PARKED_RECONNECT_BACKOFF=1800,
            hass=SimpleNamespace(async_add_executor_job=executor),
            watchdog_store=SimpleNamespace(async_save=save),
            _refresh_creds_sync=lambda *args: None,
            poll_status=SimpleNamespace(is_escalated=lambda *a, **k: False),
            widget_timer_state={"cancel": None, "stopped": False, "busy": False},
            rest_scan_interval=300, widget_raw_sink=None,
            async_call_later=lambda hass, delay, action: delays.append(delay),
            _fetch_widget_state_sync=lambda *args: widgets.pop(0),
            is_token_expired_response=lambda data: False,
            WIDGET_FAST_POLL_SECONDS=60,
            **{name: getattr(policy, name) for name in (
                "ESCALATE_AFTER_SECONDS", "ESCALATE_BACKOFF_SECONDS",
                "MOTION_ESCALATE_AFTER_SECONDS", "MOTION_EVIDENCE_MAX_AGE_SECONDS",
                "should_escalate", "should_escalate_moving")},
        )

        def frame(minute, second, *, own_drive=False):
            clock["now"] = self.at(16, minute, second)
            data = eg._state(60, odo=29505)
            data.update(has_fresh_energy_data=True, has_drive_data=own_drive)
            self.assertTrue(coord.set_vehicle_data(data))
            mqtt.last_state_msg_ts = mqtt.last_meaningful_state_ts = clock["now"]

        def widget(odometer, timer_at):
            return {"odometer": {"total_mileage": odometer}, "timer": int(timer_at * 1000)}

        with patch.object(eg.coordinator, "datetime", FakeDatetime), \
                patch("time.time", lambda: clock["now"]), \
                self.assertLogs("xpeng.test", level="DEBUG"):
            # 16:56:11 the widget reads the same odometer: nothing to see.
            widgets.append(widget(29505, self.at(16, 56, 5)))
            await ns["_poll_widget_fallback"](None)
            self.assertEqual(coord.last_motion_at, 0.0)

            # The wake burst, ending with its own P frame.
            frame(57, 18)
            frame(57, 30)
            frame(57, 46, own_drive=True)
            frame(57, 51)
            self.assertEqual(coord.last_motion_at, 0.0)

            # Ticks until the next widget reads: no evidence, no arm.
            for minute in (58, 59):
                clock["now"] = self.at(16, minute)
                await ns["_motion_arm"]("watchdog")
            for minute in (0, 1, 2):
                clock["now"] = self.at(17, minute)
                await ns["_motion_arm"]("watchdog")
            self.assertEqual(calls, [])

            # Widget reads once a minute from 17:02:30 while the rise is open.
            readings = {
                (17, 2, 24): 29507, (17, 7, 24): 29509, (17, 12, 24): 29517,
            }
            odometer, timer_at = 29507, self.at(17, 2, 24)
            for minute in range(2, 13):
                clock["now"] = self.at(17, minute, 30)
                for (h, m, sec), value in readings.items():
                    if self.at(h, m, sec) <= clock["now"]:
                        odometer, timer_at = value, self.at(h, m, sec)
                widgets.append(widget(odometer, timer_at))
                await ns["_poll_widget_fallback"](None)
                clock["now"] = self.at(17, minute, 59)
                await ns["_motion_arm"]("watchdog")

        self.assertEqual(
            [datetime.fromtimestamp(t, self.UTC).strftime("%H:%M:%S") for t in calls],
            ["17:02:30", "17:07:30", "17:12:30"],
            "one arm at each new rise, five minutes apart, none from the same rise",
        )
        self.assertEqual(coord.last_motion_arm_at, self.at(17, 12, 30))
        self.assertEqual(set(delays[1:]), {60}, "the fast window stays open all the way")


if __name__ == "__main__":
    unittest.main(verbosity=2)

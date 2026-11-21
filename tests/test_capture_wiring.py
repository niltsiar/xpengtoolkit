#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for the MQTT client's side of raw capture.

    python3 tests/test_capture_wiring.py
    python3 -m unittest discover -s tests -v

``capture.py`` tests the recorder; this tests the two places the recorder is
plugged into, because those are where a capture silently does nothing:

* the client must hand the sink the **bytes**, before anything decodes them -
  the listener's old path decoded every payload as UTF-8 with
  ``errors="replace"``, which is lossy for exactly the binary frames a capture
  exists for;
* the expensive half of ``_handle_listener_message`` must not run when only
  capture is on. 0.4.30 proved that per-frame decode is load; this checks it is
  actually skipped rather than merely moved;
* the primary topic, which the integration parses and drops fields from, and the
  REST widget body must reach the recorder as they arrived.

Loaded by path with a paho stub: ``mqtt_client`` imports paho, which is not
installed on a development host, but it does not import Home Assistant.
"""

from __future__ import annotations

# Load the stdlib module before the component directory can shadow it.
import calendar  # noqa: F401
import importlib.util
import json
import pathlib
import re
# Cache the stdlib extension before component select.py can shadow it.
import select  # noqa: F401
import sys
import tempfile
import time
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "custom_components" / "xpeng"))
import _component  # noqa: E402


def _stub_paho() -> None:
    try:
        import paho.mqtt.client  # noqa: F401
        return
    except ImportError:
        pass

    class _Any(types.ModuleType):
        def __getattr__(self, name):
            child = _Any(f"{self.__name__}.{name}")
            setattr(self, name, child)
            return child

    paho = _Any("paho")
    mqtt = _Any("paho.mqtt")
    client = _Any("paho.mqtt.client")
    mqtt.client = client
    paho.mqtt = mqtt
    sys.modules.setdefault("paho", paho)
    sys.modules.setdefault("paho.mqtt", mqtt)
    sys.modules.setdefault("paho.mqtt.client", client)


_stub_paho()
_SPEC = importlib.util.spec_from_file_location(
    "mqtt_client_under_test", _component.module_path("mqtt_client.py")
)
mqtt_client = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = mqtt_client
_SPEC.loader.exec_module(mqtt_client)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _component.module_path(filename))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


capture = _load("capture_wiring_under_test", "capture.py")


def sink_into(seen: list):
    """A sink with the real ``CaptureRecorder.submit`` signature: qos and retain are keyword-only."""

    def sink(topic, payload, *, qos, retain):
        seen.append((topic, payload, qos, retain))

    return sink


def make_client() -> object:
    return mqtt_client.XPengMQTTClient(
        client_id="xmart:app:1:2",
        password="p",
        server="broker:8885",
        user_id="1",
        cert_dir="/tmp/does-not-need-to-exist",
    )


BINARY = bytes(range(256))  # not UTF-8, and not JSON: a CAN pack in shape if not in fact


class CaptureSink(unittest.TestCase):
    def test_the_sink_receives_the_exact_bytes(self) -> None:
        client = make_client()
        seen: list[tuple] = []
        client.set_capture_callback(sink_into(seen))
        client._handle_listener_message("Xmart/Car/94434/State/CanPack1s", BINARY, qos=1, retain=True)
        self.assertEqual(len(seen), 1)
        topic, payload, qos, retain = seen[0]
        self.assertEqual(topic, "Xmart/Car/94434/State/CanPack1s")
        # Byte for byte: a decode with errors="replace" would have destroyed this.
        self.assertEqual(payload, BINARY)
        self.assertEqual((qos, retain), (1, True))

    def test_a_raising_sink_does_not_take_the_connection_down(self) -> None:
        client = make_client()

        def explode(*_args, **_kwargs):
            raise RuntimeError("capture bug")

        client.set_capture_callback(explode)
        # Must not propagate: this runs on paho's network thread, where an escaping
        # exception kills the loop and takes the whole integration's MQTT with it.
        # It must not vanish either, so it is logged.
        with self.assertLogs(mqtt_client._LOGGER, level="ERROR") as captured:
            client._handle_listener_message("t", BINARY)
        self.assertIn("capture bug", "\n".join(captured.output))

    def test_no_sink_is_fine(self) -> None:
        make_client()._handle_listener_message("t", b"hello")

    def test_the_sink_is_offered_binary_before_anything_else_runs(self) -> None:
        client = make_client()
        client.set_capture_callback(lambda *_args, **_kwargs: None)
        client._handle_listener_message("t", BINARY)
        # The decode-and-log half never ran: no topic was announced, which is the
        # observable side effect of reaching the logging block.
        self.assertEqual(client._listener_logged_topics, set())


class LazyDecode(unittest.TestCase):
    """Capture-only installs must not pay the per-frame decode at all."""

    def test_listener_still_gets_a_decoded_string(self) -> None:
        client = make_client()
        got: list[tuple] = []
        client.set_listener_callback(lambda topic, payload: got.append((topic, payload)))
        client._handle_listener_message("t", b'{"msg_type": 1}')
        self.assertEqual(got, [("t", '{"msg_type": 1}')])
        # ...and it announced the topic, so the logging block did run.
        self.assertIn("t", client._listener_logged_topics)

    def test_binary_is_skipped_when_only_capture_is_on(self) -> None:
        client = make_client()
        client.set_capture_callback(lambda *_args, **_kwargs: None)
        client._handle_listener_message("t", BINARY)
        self.assertEqual(client._listener_logged_topics, set())

    def test_json_is_still_examined_when_only_capture_is_on(self) -> None:
        # A push notification can arrive on a captured topic, so a JSON payload
        # must keep reaching the notification path even with no listener callback.
        client = make_client()
        notifications: list[dict] = []
        client.set_notification_callback(notifications.append)
        client.set_capture_callback(lambda *_args, **_kwargs: None)
        client._handle_listener_message(
            "t", json.dumps({"msg_type": 3, "service_type": 19, "msg_content": {"a": 1}}).encode()
        )
        self.assertEqual(len(notifications), 1)


def message(topic: str, payload: bytes, qos: int = 1, retain: bool = False):
    return types.SimpleNamespace(topic=topic, payload=payload, qos=qos, retain=retain)


class PrimaryTopic(unittest.TestCase):
    """The primary topic is parsed and its unknown fields dropped, so capture is the only record."""

    def test_the_sink_receives_the_primary_frame_as_it_arrived(self) -> None:
        client = make_client()
        seen: list[tuple] = []
        client.set_capture_callback(sink_into(seen))
        # Odd spacing and a field the parser has never heard of: a decoded and
        # re-serialised copy would lose both.
        raw = b'{"msg_type":  1, "service_type": 19, "future_field": {"x": [1, 2]}}'
        client._on_message(None, None, message(client.recv_topic, raw, qos=1, retain=True))
        self.assertEqual(seen, [(client.recv_topic, raw, 1, True)])

    def test_the_v2_topic_reaches_the_sink_once(self) -> None:
        client = make_client()
        seen: list[tuple] = []
        client.set_capture_callback(sink_into(seen))
        client._on_message(None, None, message(client.recv_topic_v2, BINARY))
        self.assertEqual([frame[:2] for frame in seen], [(client.recv_topic_v2, BINARY)])

    def test_a_primary_topic_that_is_also_a_listener_topic_is_captured_once(self) -> None:
        client = make_client()
        client.configure_listener(
            enabled=True,
            topics=[client.recv_topic, client.recv_topic_v2],
            discovery_enabled=False,
            extra_topics=[client.recv_topic],
        )
        client.set_listener_callback(lambda *_args: None)
        seen: list[str] = []
        client.set_capture_callback(lambda topic, payload, **_kwargs: seen.append(topic))
        client._on_message(None, None, message(client.recv_topic, b'{"msg_type": 1}'))
        client._on_message(None, None, message(client.recv_topic_v2, b'{"msg_type": 1}'))
        self.assertEqual(seen, [client.recv_topic, client.recv_topic_v2])


class RealRecorder(unittest.TestCase):
    """The client wired to the real recorder, not a stand-in for its signature."""

    def test_primary_and_listener_frames_land_in_the_file(self) -> None:
        import can_journal

        client = make_client()
        listener_topic = "Xmart/Car/1/State/CanPack1s"
        primary = b'{"msg_type":  1, "service_type": 19, "future_field": 1}'
        with tempfile.TemporaryDirectory() as directory:
            recorder = capture.CaptureRecorder(directory)
            recorder.start()
            self.addCleanup(recorder.stop, 5.0, "cleanup")
            client.set_capture_callback(recorder.submit)
            client._on_message(None, None, message(client.recv_topic, primary, qos=1))
            client._on_message(None, None, message(listener_topic, BINARY, qos=0, retain=True))
            deadline = time.monotonic() + 5.0
            while recorder.frames < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            recorder.stop(5.0, "test")
            found = list(can_journal.JournalReader(recorder.current_file).messages())
        self.assertEqual(
            [(m.topic, m.payload, m.qos, m.retain) for m in found],
            [(client.recv_topic, primary, 1, False), (listener_topic, BINARY, 0, True)],
        )


class RestWidgetBody(unittest.TestCase):
    """The widget poll keeps a few fields; the recorder must get the whole body."""

    BODY = '{"code": 200,  "data": {"soc": 61, "futureField": {"x": [1, 2]}}}'

    def setUp(self) -> None:
        self.api = _load("xpeng_api_capture_under_test", "xpeng_api.py")
        # const.py imports Home Assistant, so the constant is read from the source.
        const_source = _component.module_path("const.py").read_text(encoding="utf-8")
        self.widget_topic = re.search(
            r'^CAPTURE_TOPIC_WIDGET_STATE: Final = "([^"]+)"', const_source, re.M
        ).group(1)
        self._real_headers = self.api.build_authenticated_headers
        self._real_send = self.api._send_api_request
        self.api.build_authenticated_headers = lambda *a, **k: ({}, {})
        self.api._send_api_request = lambda *a, **k: {"status": 200, "body": self.BODY}
        self.addCleanup(setattr, self.api, "build_authenticated_headers", self._real_headers)
        self.addCleanup(setattr, self.api, "_send_api_request", self._real_send)

    def _widget(self, sink):
        return self.api.get_vehicle_state(
            None, {"_key_pool": []}, {}, "vin", "type", raw_body_sink=sink
        )

    def test_the_recorder_stores_the_body_under_the_rest_pseudo_topic(self) -> None:
        import functools

        import can_journal

        with tempfile.TemporaryDirectory() as directory:
            recorder = capture.CaptureRecorder(directory)
            recorder.start()
            self.addCleanup(recorder.stop, 5.0, "cleanup")
            sink = functools.partial(recorder.submit, self.widget_topic)
            self.assertEqual(self._widget(sink), {"soc": 61, "futureField": {"x": [1, 2]}})
            deadline = time.monotonic() + 5.0
            while recorder.frames < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            recorder.stop(5.0, "test")
            found = list(can_journal.JournalReader(recorder.current_file).messages())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].topic, "rest/biz/v5/widget/vehicle/state")
        self.assertEqual(found[0].payload, self.BODY.encode("utf-8"))
        self.assertEqual((found[0].qos, found[0].retain), (0, False))

    def test_an_encrypted_body_reaches_the_sink_as_plaintext(self) -> None:
        plain = '{"soc": 61, "futureField": 7}'
        self.api._send_api_request = lambda *a, **k: {
            "status": 200, "body": '{"code": 200, "data": {"sData": "opaque"}}'
        }
        self.api.decrypt_xtee_blob_flexible = lambda blob, pool: plain.encode("utf-8")
        seen: list[bytes] = []
        self.assertEqual(self._widget(seen.append)["soc"], 61)
        self.assertEqual(seen, [plain.encode("utf-8")])

    def test_a_failing_sink_does_not_fail_the_poll(self) -> None:
        def explode(_body):
            raise RuntimeError("capture bug")

        self.assertEqual(self._widget(explode)["soc"], 61)


class ListenerTopics(unittest.TestCase):
    def test_capture_topics_are_subscribed_without_the_listener(self) -> None:
        client = make_client()
        client.configure_listener(
            enabled=True,
            topics=[],
            discovery_enabled=False,
            extra_topics=["Xmart/Car/+/State/CanPack1s"],
        )
        self.assertIn("Xmart/Car/+/State/CanPack1s", client._listener_topics)

    def test_extra_topics_do_not_duplicate_discovery_candidates(self) -> None:
        client = make_client()
        client.configure_listener(
            enabled=True,
            topics=[],
            discovery_enabled=True,
            discovery_candidates=("Xmart/Car/+/State/CanPack1s",),
            extra_topics=["Xmart/Car/+/State/CanPack1s"],
        )
        self.assertEqual(
            client._listener_topics.count("Xmart/Car/+/State/CanPack1s"), 1
        )

    def test_disabled_listener_does_not_subscribe(self) -> None:
        client = make_client()
        client.configure_listener(enabled=False, topics=[], discovery_enabled=False)
        self.assertFalse(client._listener_enabled)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A `msg_type=3` push must keep its envelope, like a polled record keeps its raw item.

`_fire_notification()` flattens `msg_content` into the top-level notification
dict (service_type/msg_id/code/message plus the extra content fields), which
drops the envelope the broker actually sent. That envelope is where a field the
integration does not know about lives, so it is snapshotted as `raw` (copied
from the pre-flattening `data`) exactly as `parse_notification_item()` snapshots
a Message Centre item. `record_notification()` keeps an existing `raw` rather
than replacing it.

Home Assistant is not importable here; `mqtt_client.py` imports only paho at
module scope, so paho is stubbed when it is not installed - the same pattern the
rest of the suite uses for `cryptography`.

    python3 tests/test_mqtt_notification_raw.py
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import types
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _component  # noqa: E402


def _stub_paho() -> None:
    """Stand in for paho's client module, which the host may not have."""
    try:
        import paho.mqtt.client  # noqa: F401
        return
    except ImportError:
        pass

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

    paho = types.ModuleType("paho")
    paho_mqtt = types.ModuleType("paho.mqtt")
    paho_mqtt_client = types.ModuleType("paho.mqtt.client")
    paho_mqtt_client.Client = _Client
    paho_mqtt_client.MQTTv311 = 4
    paho_mqtt_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    paho_mqtt_client.MQTTMessage = object
    paho_mqtt.client = paho_mqtt_client
    paho.mqtt = paho_mqtt
    sys.modules["paho"] = paho
    sys.modules["paho.mqtt"] = paho_mqtt
    sys.modules["paho.mqtt.client"] = paho_mqtt_client


_stub_paho()

_spec = importlib.util.spec_from_file_location(
    "mqtt_client_raw_t", _component.module_path("mqtt_client.py"))
assert _spec and _spec.loader
mqtt_client = importlib.util.module_from_spec(_spec)
sys.modules["mqtt_client_raw_t"] = mqtt_client
_spec.loader.exec_module(mqtt_client)


def _client():
    client = mqtt_client.XPengMQTTClient(
        client_id="xmart:app:1:abc", password="pw",
        server="broker.example:8885", user_id="1",
    )
    received: list[dict] = []
    client.set_notification_callback(received.append)
    return client, received


class TestPushRawRecord(unittest.TestCase):
    PUSH = {
        "msg_type": 3,
        "service_type": 12,
        "msg_id": "abc123",
        "msg_content": {"code": 0, "msg": "Window open", "window": 1},
    }

    def test_envelope_is_preserved_as_raw(self):
        client, received = _client()
        client._fire_notification(self.PUSH)
        self.assertEqual(len(received), 1)
        raw = received[0]["raw"]
        # The flattened fields still work...
        self.assertEqual(received[0]["message"], "Window open")
        self.assertEqual(received[0]["window"], 1)
        # ...and the envelope that flattening drops is still there.
        self.assertEqual(raw["msg_type"], 3)
        self.assertEqual(raw["msg_content"], self.PUSH["msg_content"])
        self.assertEqual(raw, self.PUSH)

    def test_raw_is_a_copy_and_the_payload_is_not_mutated(self):
        client, received = _client()
        push = {
            "msg_type": 3,
            "service_type": 12,
            "msg_id": "m",
            "msg_content": {"code": 0, "msg": "x"},
        }
        client._fire_notification(push)
        self.assertIsNot(received[0]["raw"], push)
        # record_notification adds timestamp/local_time later; nothing of that
        # may already be in the push dict, or raw would carry derived fields.
        self.assertNotIn("timestamp", push)
        self.assertNotIn("local_time", push)
        self.assertNotIn("raw", push)

    def test_falsy_raw_is_still_present(self):
        client, received = _client()
        client._fire_notification({"msg_type": 3, "msg_content": {}})
        self.assertEqual(received[0]["raw"], {"msg_type": 3, "msg_content": {}})

    def test_msg_content_key_named_raw_cannot_displace_the_envelope(self):
        """`raw` is reserved: the wire envelope wins over a content field."""
        client, received = _client()
        client._fire_notification({
            "msg_type": 3, "msg_id": "m",
            "msg_content": {"code": 0, "msg": "x", "raw": "from content"},
        })
        raw = received[0]["raw"]
        self.assertIsInstance(raw, dict)
        self.assertEqual(raw["msg_id"], "m")
        self.assertEqual(raw["msg_content"]["raw"], "from content")


TAKEOVER = {
    "msg_type": 3,
    "service_type": 17,
    "msg_id": "m1",
    "msg_content": {"code": 4003007, "msg": "Account logged in on another device"},
}
OTHER = {
    "msg_type": 3,
    "service_type": 12,
    "msg_id": "m2",
    "msg_content": {"code": 0, "msg": "Window open"},
}


class _Msg:
    qos = 0
    retain = False

    def __init__(self, topic: str, body: dict) -> None:
        self.topic = topic
        self.payload = json.dumps(body).encode("utf-8")


class TestTakeoverIsQuietInTheLog(unittest.TestCase):
    """Every renewal sends one takeover notice; none of it may reach INFO."""

    CLIENT_TOPIC = "$client/xmart:app:1:abc"

    def test_a_takeover_frame_on_the_client_topic_logs_nothing_above_debug(self):
        client, _received = _client()
        msg = _Msg(self.CLIENT_TOPIC, TAKEOVER)
        with self.assertNoLogs(level="INFO"):
            client._on_message(None, None, msg)

    def test_a_takeover_frame_on_the_primary_topic_logs_nothing_above_debug(self):
        client, _received = _client()
        msg = _Msg(client.recv_topic, TAKEOVER)
        with self.assertNoLogs(level="INFO"):
            client._on_message(None, None, msg)

    def test_a_takeover_still_reaches_the_notification_callback(self):
        for topic in (self.CLIENT_TOPIC, None):
            with self.subTest(topic=topic):
                client, received = _client()
                client._on_message(None, None, _Msg(topic or client.recv_topic, TAKEOVER))
                self.assertEqual(len(received), 1)
                self.assertEqual(received[0]["code"], 4003007)
                self.assertTrue(mqtt_client.is_account_takeover(received[0]))

    def test_other_push_notifications_still_log_at_info(self):
        for topic in (self.CLIENT_TOPIC, None):
            with self.subTest(topic=topic):
                client, received = _client()
                msg = _Msg(topic or client.recv_topic, OTHER)
                with self.assertLogs(level="INFO") as logs:
                    client._on_message(None, None, msg)
                self.assertTrue(
                    any("Push notification parsed" in line for line in logs.output)
                )
                self.assertEqual(len(received), 1)

    def test_note_account_takeover_logs_at_debug_and_still_opens_the_grace(self):
        client, _received = _client()
        self.assertFalse(client.settling)
        with self.assertNoLogs(level="INFO"):
            client.note_account_takeover("signed in elsewhere")
        self.assertTrue(client.settling)

    def test_the_envelope_form_is_read_through_msg_content(self):
        self.assertTrue(mqtt_client.is_account_takeover_frame(TAKEOVER))
        self.assertFalse(mqtt_client.is_account_takeover_frame(OTHER))
        for junk in (None, [], {"msg_content": "x"}, {"service_type": 17}):
            self.assertFalse(mqtt_client.is_account_takeover_frame(junk), junk)


if __name__ == "__main__":
    unittest.main(verbosity=2)

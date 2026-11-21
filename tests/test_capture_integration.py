#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The integration's capture wiring, checked statically.

    python3 tests/test_capture_integration.py
    python3 -m unittest discover -s tests -v

``__init__.py`` cannot be imported without Home Assistant, so the wiring that
connects the recorder to a config entry is not reachable from an ordinary unit
test - and it is three lines that are easy to get wrong in ways nothing else
notices:

* a method called on the recorder that the recorder does not have. This is not
  hypothetical: ``capture_recorder.note(...)`` shipped against a recorder that
  only had ``_note``, and the failure would have been an AttributeError on the
  first subscription result - inside a callback, on paho's thread, for every
  topic the broker answered. The check below is what found it.
* a recorder that is started but never stopped, which leaks a writer thread per
  config-entry reload.

Both are static properties of the source, so they are checked statically, in the
same spirit as ``test_module_names.py``.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import re
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "custom_components" / "xpeng"))
import _component  # noqa: E402

INIT_PATH = _component.module_path("__init__.py")
SOURCE = INIT_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)

_SPEC = importlib.util.spec_from_file_location(
    "capture_under_test", _component.module_path("capture.py")
)
capture = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = capture
_SPEC.loader.exec_module(capture)


def function_source(name: str) -> str:
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(SOURCE, node) or ""
    raise AssertionError(f"{name}() not found in __init__.py")


class RecorderApiIsReal(unittest.TestCase):
    """Every method the setup code calls the recorder exists on the recorder."""

    @staticmethod
    def _recorder_names() -> set[str]:
        """Names that hold a recorder: from open_recorder(), or from get("capture").

        Matching on the name alone is not enough - ``capture_summary`` is the
        summary dict returned by stop(), and ``capture_dir`` is a string, so their
        attributes are not the recorder's.
        """
        names: set[str] = set()
        for node in ast.walk(TREE):
            if not isinstance(node, ast.Assign):
                continue
            segment = ast.get_source_segment(SOURCE, node.value) or ""
            if "open_recorder" not in segment and 'get("capture")' not in segment:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, ast.Tuple) and target.elts:
                    # capture_recorder, capture_dir = ... open_recorder(...)
                    first = target.elts[0]
                    if isinstance(first, ast.Name):
                        names.add(first.id)
        return names

    def test_every_attribute_used_on_the_recorder_exists(self) -> None:
        names = self._recorder_names()
        self.assertTrue(names, "no capture recorder binding found in __init__.py")

        # An instance, not the class: `topics` and the counters are set in
        # __init__, so they are not class attributes.
        with tempfile.TemporaryDirectory() as directory:
            instance = capture.CaptureRecorder(directory)

            used: set[tuple[str, str]] = set()
            for node in ast.walk(TREE):
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    if node.value.id in names:
                        used.add((node.value.id, node.attr))

            self.assertTrue(used, "the recorder is never used in __init__.py")
            for name, attribute in sorted(used):
                with self.subTest(call=f"{name}.{attribute}"):
                    self.assertTrue(
                        hasattr(instance, attribute),
                        f"__init__.py calls {name}.{attribute}, which "
                        "CaptureRecorder does not define",
                    )

    def test_the_recorder_is_actually_configured(self) -> None:
        body = function_source("async_setup_entry")
        self.assertIn("open_recorder", body)
        self.assertIn("set_capture_callback", body)
        self.assertIn("CONF_CAPTURE_ENABLED", body)


class RecorderIsStopped(unittest.TestCase):
    """A recorder that outlives its entry leaks a writer thread per reload."""

    def test_unload_stops_the_recorder(self) -> None:
        body = function_source("async_unload_entry")
        self.assertIn('data.get("capture")', body)
        self.assertIn(".stop", body)

    def test_a_replaced_entry_stops_the_previous_recorder(self) -> None:
        # The same guard that stops a stale MQTT client must stop a stale writer:
        # two writers in one directory interleave parts under one timestamped name.
        body = function_source("async_setup_entry")
        self.assertIn('previous.get("capture")', body)

    def test_stop_is_called_off_the_event_loop(self) -> None:
        # stop() joins the writer thread, so calling it directly would block the
        # loop for up to its timeout.
        for name in ("async_unload_entry", "async_setup_entry"):
            body = function_source(name)
            for line in body.splitlines():
                if ".stop" in line and "capture" in line.lower():
                    self.assertNotIn(
                        "capture_recorder.stop(", line.strip().lstrip("await "),
                        f"{name} calls stop() without an executor",
                    )


class OptionsAreWired(unittest.TestCase):
    def test_the_new_options_are_declared_where_they_are_read(self) -> None:
        const_source = _component.module_path("const.py").read_text(encoding="utf-8")
        for name in ("CONF_CAPTURE_ENABLED", "CONF_CAPTURE_PATH",
                     "CONF_CAPTURE_TOPICS", "DEFAULT_CAPTURE_PATH"):
            self.assertIn(name, const_source, f"{name} is missing from const.py")

    def test_the_options_form_exposes_them(self) -> None:
        flow = _component.module_path("config_flow.py").read_text(encoding="utf-8")
        for name in ("CONF_CAPTURE_ENABLED", "CONF_CAPTURE_PATH", "CONF_CAPTURE_TOPICS"):
            self.assertGreaterEqual(
                flow.count(name), 2, f"{name} is imported but never used as a field"
            )

    def test_every_option_has_a_label_and_a_description(self) -> None:
        import json

        for name in ("strings.json", "translations/en.json"):
            document = json.loads(
                (_component.component_dir() / name).read_text(encoding="utf-8")
            )
            step = document["options"]["step"]["init"]
            for option in ("capture_enabled", "capture_path", "capture_topics"):
                with self.subTest(translation=name, option=option):
                    self.assertIn(option, step["data"])
                    self.assertIn(option, step.get("data_description", {}))
                    # Home Assistant rejects the whole form on a placeholder it
                    # cannot fill, so a label must not carry {braces}.
                    self.assertNotRegex(step["data"][option], re.compile(r"[{}]"))


class WidgetBodyIsCaptured(unittest.TestCase):
    """The REST widget body is recorded only while capture is on, from both polls."""

    @staticmethod
    def _setup_tree() -> ast.AST:
        for node in ast.walk(TREE):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry":
                return node
        raise AssertionError("async_setup_entry() not found in __init__.py")

    def test_the_sink_is_only_built_when_capture_is_enabled(self) -> None:
        tree = self._setup_tree()
        guarded: dict[int, bool] = {}

        def visit(node: ast.AST, in_capture_branch: bool) -> None:
            if isinstance(node, ast.If) and "capture_enabled" in ast.dump(node.test):
                in_capture_branch = True
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Name) and t.id == "widget_raw_sink" for t in targets):
                    is_none = isinstance(node.value, ast.Constant) and node.value.value is None
                    guarded[node.lineno] = is_none or in_capture_branch
            for child in ast.iter_child_nodes(node):
                visit(child, in_capture_branch)

        visit(tree, False)
        self.assertGreaterEqual(len(guarded), 2, "widget_raw_sink is never built")
        self.assertTrue(all(guarded.values()), f"built outside the capture branch: {guarded}")

    def test_the_sink_files_frames_under_the_rest_topic(self) -> None:
        body = " ".join(function_source("async_setup_entry").split())
        self.assertIn(
            "functools.partial( capture_recorder.submit, CAPTURE_TOPIC_WIDGET_STATE )", body
        )

    def test_both_widget_polls_hand_over_the_sink(self) -> None:
        passed = [
            node.id
            for node in ast.walk(self._setup_tree())
            if isinstance(node, ast.Name) and node.id == "widget_raw_sink"
            and isinstance(node.ctx, ast.Load)
        ]
        self.assertEqual(len(passed), 2, "the heavy REST poll and the fast fallback poll")


if __name__ == "__main__":
    unittest.main(verbosity=2)

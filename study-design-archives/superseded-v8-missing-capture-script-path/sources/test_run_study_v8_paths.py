#!/usr/bin/env python3
"""Offline regression tests for the v8 raw proxy event path resolver."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SAVED_V7_RUN = ROOT / "results/ir-composition-tdd-v7-20260930T101537Z-f1832bd67ec9"
RUNNER_PATH = ROOT / "scripts/run_study_v8.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("run_study_v8_under_test", RUNNER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load run_study_v8.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RawProxyEventPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner()
        cls.run_dir = SAVED_V7_RUN
        event_index = json.loads((cls.run_dir / "proxy/events.json").read_bytes())
        cls.events = event_index["events"]
        cls.post = next(event for event in cls.events if event.get("kind") == "laya_choice")
        cls.health = next(event for event in cls.events if event.get("kind") == "health_check")

    def test_saved_real_post_event_reads_both_raw_files_and_matches_hashes(self):
        request, response = self.runner.raw_event_files(self.run_dir, self.post)
        self.assertEqual(hashlib.sha256(request).hexdigest(), self.post["request_sha256"])
        self.assertEqual(hashlib.sha256(response).hexdigest(), self.post["response_sha256"])
        self.assertGreater(len(request), 0)
        self.assertGreater(len(response), 0)

    def test_saved_health_event_reads_empty_request_and_raw_response(self):
        request, response = self.runner.raw_event_files(self.run_dir, self.health)
        self.assertEqual(request, b"")
        self.assertEqual(hashlib.sha256(request).hexdigest(), self.health["request_sha256"])
        self.assertEqual(hashlib.sha256(response).hexdigest(), self.health["response_sha256"])
        self.assertGreater(len(response), 0)

    def test_rejects_absolute_parent_and_wrong_tree_paths(self):
        for path in ("/tmp/outside.raw", "../outside.raw", "proxy/../outside.raw",
                     "proxy/responses/0001.response.raw", "proxy\\requests\\outside.raw"):
            with self.subTest(path=path):
                event = dict(self.post, request_file=path)
                with self.assertRaises(ValueError):
                    self.runner.raw_event_files(self.run_dir, event)

    def test_rejects_symlink_that_escapes_run_directory(self):
        with tempfile.TemporaryDirectory(prefix="gooo-v8-event-path-") as temp:
            temp_root = Path(temp)
            run_dir = temp_root / "run"
            outside = temp_root / "outside"
            (run_dir / "proxy").mkdir(parents=True)
            outside.mkdir()
            (outside / "0001.request.raw").write_bytes(b"outside request")
            (run_dir / "proxy/requests").symlink_to(outside, target_is_directory=True)
            (run_dir / "proxy/responses").mkdir()
            (run_dir / "proxy/responses/0001.response.raw").write_bytes(b"inside response")
            event = {"request_file": "proxy/requests/0001.request.raw",
                     "response_file": "proxy/responses/0001.response.raw"}
            with self.assertRaises(ValueError):
                self.runner.raw_event_files(run_dir, event)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from live_relay.runtime import RelayStatus
from live_relay.supervisor import (
    HEARTBEAT_FILE,
    RESTART_FILE,
    RelaySupervisor,
    _record_restart,
    heartbeat_age,
    read_heartbeat,
    read_restarts,
    write_heartbeat,
)


class HeartbeatTests(unittest.TestCase):
    def test_heartbeat_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            write_heartbeat(state_dir, mode="streaming", extra={"title": "بث"})
            data = read_heartbeat(state_dir)
            self.assertEqual(data["mode"], "streaming")
            self.assertEqual(data["title"], "بث")
            self.assertIsNotNone(heartbeat_age(state_dir))
            self.assertGreaterEqual(heartbeat_age(state_dir), 0)
            if os.name != "nt":
                self.assertEqual(
                    (state_dir / HEARTBEAT_FILE).stat().st_mode & 0o777, 0o600
                )

    def test_missing_or_broken_heartbeat_is_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            self.assertEqual(read_heartbeat(state_dir), {})
            self.assertIsNone(heartbeat_age(state_dir))
            (state_dir / HEARTBEAT_FILE).write_text("{not json", encoding="utf-8")
            self.assertEqual(read_heartbeat(state_dir), {})

    def test_restart_history_is_capped(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            for index in range(25):
                _record_restart(state_dir, attempt=index, reason="x", delay=1)
            history = read_restarts(state_dir)
            self.assertEqual(len(history), 20)
            self.assertEqual(history[-1]["attempt"], 24)
            if os.name != "nt":
                self.assertEqual(
                    (state_dir / RESTART_FILE).stat().st_mode & 0o777, 0o600
                )


class SupervisorTests(unittest.TestCase):
    def test_restarts_after_a_failure_and_then_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(state_dir=Path(directory))
            stopped = threading.Event()
            calls = []

            def fake_run(settings, stopped, status=None):
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("boom")
                return None

            with patch("live_relay.service.run", side_effect=fake_run):
                RelaySupervisor(
                    settings, stopped, None, base_delay=0.01
                ).run()

            self.assertEqual(len(calls), 2)
            history = read_restarts(Path(directory))
            self.assertEqual(len(history), 1)
            self.assertIn("boom", history[0]["reason"])
            self.assertEqual(read_heartbeat(Path(directory))["mode"], "stopped")

    def test_gives_up_after_too_many_consecutive_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(state_dir=Path(directory))
            stopped = threading.Event()
            status = RelayStatus()

            def always_fail(settings, stopped, status=None):
                raise RuntimeError("still broken")

            with patch("live_relay.service.run", side_effect=always_fail):
                RelaySupervisor(
                    settings,
                    stopped,
                    status,
                    max_consecutive=2,
                    base_delay=0.01,
                ).run()

            snapshot = status.snapshot()
            self.assertEqual(snapshot["mode"], "error")
            self.assertIn("أعطال", snapshot["message"])
            self.assertEqual(len(read_restarts(Path(directory))), 2)

    def test_a_stopped_relay_does_not_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SimpleNamespace(state_dir=Path(directory))
            stopped = threading.Event()
            stopped.set()
            with patch("live_relay.service.run") as run:
                RelaySupervisor(settings, stopped, None, base_delay=0.01).run()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

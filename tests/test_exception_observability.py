"""Tests for audit item 3 (R1): once-silent handlers now record a diagnostic.

These lock the observability contract - a swallowed failure must be logged
(via ``Utils.get_logger()``) rather than disappearing, without changing control
flow or return values.
"""
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))

from dwipe import device_io, markers, verification  # noqa: E402
from dwipe.Utils import Utils  # noqa: E402
from dwipe.WipeJob import WipeJob  # noqa: E402
from dwipe.WriteTask import WriteZeroTask  # noqa: E402


class RecordingLogger:
    """Minimal stand-in that records (level, args) tuples."""

    def __init__(self):
        self.records = []

    def debug(self, *args, **kwargs):
        self.records.append(("debug", args))

    def info(self, *args, **kwargs):
        self.records.append(("info", args))

    def error(self, *args, **kwargs):
        self.records.append(("error", args))

    def event(self, *args, **kwargs):
        self.records.append(("event", args))

    def levels(self):
        return [level for level, _ in self.records]


class _UnreadableIO:
    def open_read(self, path):
        raise OSError("cannot open")

    def open_buffered(self, path, mode="rb"):
        raise OSError("cannot open")


def _use_recorder(monkeypatch):
    rec = RecordingLogger()
    monkeypatch.setattr(Utils, "get_logger", staticmethod(lambda: rec))
    return rec


def test_read_marker_logs_and_returns_none_on_ioerror(monkeypatch):
    rec = _use_recorder(monkeypatch)
    assert markers.read_marker("sda1", _UnreadableIO()) is None
    assert "debug" in rec.levels()


def test_write_task_marker_failure_is_logged(monkeypatch):
    rec = _use_recorder(monkeypatch)
    task = WriteZeroTask("/dev/loop0", 4096,
                         SimpleNamespace(slowdown_stop=0, stall_timeout=0))
    # Force the update to be due without assuming system uptime exceeds the
    # interval (time.monotonic() is small on freshly-booted CI runners).
    task.last_marker_update_mono = time.monotonic() - task.marker_update_interval - 1

    def boom(path, mode="rb"):
        raise OSError("marker write failed")

    monkeypatch.setattr(device_io, "open_buffered", boom)
    task.maybe_update_marker()  # must not raise
    assert "debug" in rec.levels()


def test_verify_device_logs_on_error(monkeypatch):
    rec = _use_recorder(monkeypatch)
    result = verification.verify_device(
        _UnreadableIO(), "/dev/loop0", total_size=1000, verify_pct=5,
        expected_pattern="zeroed")
    assert result[0] == "error"
    assert "error" in rec.levels()


def test_detect_pattern_on_disk_logs_when_unreadable(monkeypatch):
    rec = _use_recorder(monkeypatch)

    def boom(*a, **k):
        raise OSError("unreadable")

    monkeypatch.setattr("builtins.open", boom)
    assert WipeJob.detect_pattern_on_disk("/dev/loop0") is None
    assert "debug" in rec.levels()

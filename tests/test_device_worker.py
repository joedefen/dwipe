"""Tests for DeviceWorker / DeviceWorkerManager (audit item 6).

The worker threads are never started here; the pure state logic, sysfs
sanitisation and the manager bookkeeping are exercised directly.
"""
import io
import time
from types import SimpleNamespace

import pytest

import dwipe.DeviceWorker as dwmod
from dwipe.DeviceWorker import DeviceWorker, DeviceWorkerManager, ProbeState


# ---------------------------------------------------------------------------
# state / requests
# ---------------------------------------------------------------------------
def test_initial_states_are_pending():
    w = DeviceWorker("sda")
    st = w.get_state()
    assert st["hw_caps_state"] is ProbeState.PENDING
    assert st["serial_state"] is ProbeState.PENDING
    assert st["model_state"] is ProbeState.PENDING
    assert w.get_hw_caps() == ('', '', '', ProbeState.PENDING, False, False)
    assert w.get_marker_formatted() == ''


def test_request_hw_caps_enqueues_once_then_force_requeues():
    w = DeviceWorker("sda")
    w.request_hw_caps()
    assert w.get_state()["hw_caps_state"] is ProbeState.PROBING
    assert w._work_queue.qsize() == 1
    w.request_hw_caps()  # already probing -> no duplicate
    assert w._work_queue.qsize() == 1
    w.request_hw_caps(force=True)
    assert w._work_queue.qsize() == 2


def test_set_want_marker_enable_then_disable():
    w = DeviceWorker("sda")
    w.set_want_marker(True)
    st = w.get_state()
    assert st["want_marker"] is True
    assert st["marker_search_start_time"] > 0

    w._state["marker_formatted"] = "stale"
    w.set_want_marker(False)
    st = w.get_state()
    assert st["want_marker"] is False
    assert st["marker_formatted"] == ""


# ---------------------------------------------------------------------------
# marker polling interval logic
# ---------------------------------------------------------------------------
def test_check_marker_polling_noop_when_not_wanted(monkeypatch):
    w = DeviceWorker("sda")
    calls = []
    monkeypatch.setattr(w, "_probe_marker_once", lambda: calls.append(1))
    w._check_marker_polling()
    assert calls == []


def test_check_marker_polling_initial_window_probes(monkeypatch):
    w = DeviceWorker("sda")
    w._state["want_marker"] = True
    w._state["marker_search_start_time"] = time.time()
    w._state["last_marker_check_time"] = 0
    calls = []
    monkeypatch.setattr(w, "_probe_marker_once", lambda: calls.append(1))
    w._check_marker_polling()
    assert calls == [1]


def test_check_marker_polling_recent_check_skips(monkeypatch):
    w = DeviceWorker("sda")
    w._state["want_marker"] = True
    w._state["marker_search_start_time"] = time.time()
    w._state["last_marker_check_time"] = time.time()  # just checked
    calls = []
    monkeypatch.setattr(w, "_probe_marker_once", lambda: calls.append(1))
    w._check_marker_polling()
    assert calls == []


# ---------------------------------------------------------------------------
# _format_marker
# ---------------------------------------------------------------------------
def _marker(**kw):
    base = dict(unixtime=int(time.time()) - 3600, scrubbed_bytes=1000,
                size_bytes=1000, mode="Zero")
    base.update(kw)
    return SimpleNamespace(**base)


def test_format_marker_invalid_size_returns_empty():
    assert DeviceWorker("sda")._format_marker(_marker(size_bytes=0)) == ''


def test_format_marker_future_timestamp_returns_empty():
    future = int(time.time()) + 10_000
    assert DeviceWorker("sda")._format_marker(_marker(unixtime=future)) == ''


def test_format_marker_completed_and_partial():
    w = DeviceWorker("sda")
    assert w._format_marker(_marker()).startswith("W 100% Zero ")
    assert w._format_marker(_marker(scrubbed_bytes=500, mode="Rand")).startswith("s 50% Rand ")


def test_format_marker_verify_and_abort_decorations():
    w = DeviceWorker("sda")
    assert w._format_marker(_marker(verify_status="pass")).startswith("✓ ")
    assert w._format_marker(_marker(verify_status="fail")).startswith("✗ ")
    assert w._format_marker(_marker(abort_reason="stall")).endswith("Err[stall]")


# ---------------------------------------------------------------------------
# sysfs attribute reading
# ---------------------------------------------------------------------------
def test_read_sysfs_attr_sanitizes(monkeypatch):
    w = DeviceWorker("sda")
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO("  MOD\x00EL \n"))
    assert w._read_sysfs_attr("device/model") == "MODEL"


def test_read_sysfs_attr_missing_returns_empty(monkeypatch):
    w = DeviceWorker("sda")

    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr("builtins.open", boom)
    assert w._read_sysfs_attr("device/model") == ""


# ---------------------------------------------------------------------------
# DeviceWorkerManager
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,expected", [
    ("sda", True), ("sda1", False),
    ("hdb", True), ("hdb1", False),
    ("nvme0n1", True), ("nvme0n1p1", False),
    ("dm-0", False),
])
def test_is_whole_disk(name, expected):
    assert DeviceWorkerManager._is_whole_disk(name) is expected


class _FakeWorker:
    def __init__(self, name, checker):
        self.name = name
        self.started = False
        self.stopped = False
        self.joined = False
        self._marker = ''
        self._hwstate = ProbeState.PENDING

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        self.joined = True

    def get_state(self):
        return {"marker_formatted": self._marker, "hw_caps_state": self._hwstate}


def test_update_devices_creates_and_stops(monkeypatch):
    created = {}
    monkeypatch.setattr(dwmod, "DeviceWorker", _FakeWorker)

    def factory(name, checker):
        w = _FakeWorker(name, checker)
        created[name] = w
        return w

    monkeypatch.setattr(dwmod, "DeviceWorker", factory)

    mgr = DeviceWorkerManager()
    mgr.update_devices({"sda", "sdb"})
    assert set(created) == {"sda", "sdb"}
    assert created["sda"].started and created["sdb"].started

    mgr.update_devices({"sda"})
    assert created["sdb"].stopped is True
    assert "sdb" not in mgr._workers


def test_manager_unknown_device_defaults():
    mgr = DeviceWorkerManager()
    assert mgr.get_state("nope") is None
    assert mgr.get_hw_caps("nope") == ('', '', '', ProbeState.PENDING, False, False)
    assert mgr.get_marker_formatted("nope") == ''
    mgr.request_hw_caps("nope")     # no-op, must not raise
    mgr.set_want_marker("nope", True)
    mgr.request_marker_check("nope")


def test_has_updates_detects_marker_and_state_changes(monkeypatch):
    created = {}
    monkeypatch.setattr(dwmod, "DeviceWorker",
                        lambda name, checker: created.setdefault(name, _FakeWorker(name, checker)))

    mgr = DeviceWorkerManager()
    mgr.update_devices({"sda"})
    worker = created["sda"]
    worker._marker = "A"
    worker._hwstate = ProbeState.READY

    assert mgr.has_updates({"sda": {"marker": "A", "hw_caps_state": ProbeState.READY}}) is False
    assert mgr.has_updates({"sda": {"marker": "B", "hw_caps_state": ProbeState.READY}}) is True
    assert mgr.has_updates({"sda": {"marker": "A", "hw_caps_state": ProbeState.PENDING}}) is True


def test_stop_all_stops_and_clears(monkeypatch):
    created = {}
    monkeypatch.setattr(dwmod, "DeviceWorker",
                        lambda name, checker: created.setdefault(name, _FakeWorker(name, checker)))
    mgr = DeviceWorkerManager()
    mgr.update_devices({"sda", "sdb"})
    mgr.stop_all()
    assert all(w.stopped and w.joined for w in created.values())
    assert mgr._workers == {}

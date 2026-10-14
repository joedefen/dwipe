"""Tests for DeviceChangeMonitor change detection (audit item 6).

These never touch real block devices; the sysfs/``/proc`` reads are
monkeypatched so the fingerprint logic is exercised hermetically.
"""
import io
import os

from dwipe.DeviceChangeMonitor import DeviceChangeMonitor


def _patch_sources(monkeypatch, part_text, devs=("sda", "sdb")):
    monkeypatch.setattr(os, "listdir", lambda path: list(devs))
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(part_text))


def test_check_for_changes_first_then_unchanged(monkeypatch):
    mon = DeviceChangeMonitor()
    _patch_sources(monkeypatch, "partitions-v1")
    assert mon._check_for_changes() is True   # differs from initial None
    assert mon._check_for_changes() is False  # unchanged fingerprint


def test_check_for_changes_detects_new_fingerprint(monkeypatch):
    mon = DeviceChangeMonitor()
    _patch_sources(monkeypatch, "partitions-v1")
    assert mon._check_for_changes() is True
    _patch_sources(monkeypatch, "partitions-v2")
    assert mon._check_for_changes() is True


def test_check_for_changes_error_defaults_true(monkeypatch):
    mon = DeviceChangeMonitor()

    def boom(path):
        raise OSError("cannot read /sys")

    monkeypatch.setattr(os, "listdir", boom)
    assert mon._check_for_changes() is True


def test_get_and_clear_then_peek():
    mon = DeviceChangeMonitor()
    assert mon.get_and_clear() is False
    mon._changes_detected = True
    assert mon.peek() is True
    assert mon.get_and_clear() is True
    assert mon.peek() is False


def test_polling_loop_sets_flag_and_exits_on_stop(monkeypatch):
    mon = DeviceChangeMonitor(check_interval=0.01)

    def fake_check():
        mon._stop_event.set()  # ask the loop to finish after this check
        return True

    monkeypatch.setattr(mon, "_check_for_changes", fake_check)
    mon._polling_loop()  # returns promptly because stop_event is set
    assert mon.get_and_clear() is True


def test_monitor_loop_dispatches_to_polling(monkeypatch):
    mon = DeviceChangeMonitor()
    mon._use_inotify = False
    called = {"poll": False, "inotify": False}
    monkeypatch.setattr(mon, "_polling_loop", lambda: called.__setitem__("poll", True))
    monkeypatch.setattr(mon, "_inotify_loop", lambda: called.__setitem__("inotify", True))
    mon._monitor_loop()
    assert called == {"poll": True, "inotify": False}


def test_start_and_stop_joins_thread(monkeypatch):
    mon = DeviceChangeMonitor(check_interval=0.01)
    monkeypatch.setattr(mon, "_try_init_inotify", lambda: None)
    mon._use_inotify = False
    monkeypatch.setattr(mon, "_check_for_changes", lambda: False)

    mon.start()
    assert mon._thread is not None and mon._thread.is_alive()
    mon.start()  # idempotent - should not spawn a second thread
    mon.stop()
    assert not mon._thread.is_alive()


def test_stop_without_start_is_safe():
    DeviceChangeMonitor().stop()  # must not raise

"""Contract tests for ``WriteTask.safe_write`` (audit item 4).

``safe_write`` previously existed twice with *divergent* contracts: the copy in
``WipeJob`` documented "returns len(chunk) on a non-fatal skip" while the copy in
``WriteTask`` raises on abort. The dead ``WipeJob`` copy has been deleted, so
this file locks the surviving ``WriteTask.safe_write`` behaviour:

* success returns ``(bytes_written, fd)`` and resets the reopen counter,
* a run of transient errors is retried (with an fd reopen) until it succeeds,
* a streak of consecutive errors aborts by raising.
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from device_double import FakeDeviceIO  # noqa: E402

from dwipe import device_io  # noqa: E402
from dwipe.WriteTask import WriteZeroTask  # noqa: E402

CHUNK = 4096
DEVICE_PATH = "/dev/loop0"


@pytest.fixture
def fake_device():
    fake = FakeDeviceIO(1 << 20)
    device_io.use_device(fake)
    try:
        yield fake
    finally:
        device_io.reset()


def make_task():
    return WriteZeroTask(DEVICE_PATH, 1 << 20,
                         SimpleNamespace(slowdown_stop=0, stall_timeout=0))


def test_safe_write_success_returns_bytes_and_same_fd(fake_device):
    task = make_task()
    fd = device_io.open_direct(DEVICE_PATH, write=True)

    written, new_fd = task.safe_write(fd, bytes(CHUNK))

    assert written == CHUNK
    assert new_fd == fd  # no reopen needed
    assert task.reopen_count == 0
    assert task.total_errors == 0


def test_safe_write_retries_transient_errors(fake_device, monkeypatch):
    task = make_task()
    fd = device_io.open_direct(DEVICE_PATH, write=True)

    real_write = fake_device.write
    calls = {"n": 0}

    def flaky(handle, data):
        calls["n"] += 1
        if calls["n"] <= 2:  # fail twice, then succeed
            raise OSError("transient")
        return real_write(handle, data)

    monkeypatch.setattr(fake_device, "write", flaky)

    written, _ = task.safe_write(fd, bytes(CHUNK))

    assert written == CHUNK  # eventually succeeded
    assert task.total_errors == 2  # two transient failures were counted
    assert calls["n"] == 3  # failed, failed, succeeded


def test_safe_write_aborts_after_consecutive_errors(fake_device, monkeypatch):
    task = make_task()
    fd = device_io.open_direct(DEVICE_PATH, write=True)

    def always_fail(handle, data):
        raise OSError("persistent")

    monkeypatch.setattr(fake_device, "write", always_fail)

    with pytest.raises(Exception) as excinfo:
        task.safe_write(fd, bytes(CHUNK))

    assert "consecutive write errors" in str(excinfo.value)
    assert task.total_errors == task.max_consecutive_errors == 3

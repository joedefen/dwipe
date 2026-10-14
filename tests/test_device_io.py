"""Direct tests for the real ``device_io.PosixDeviceIO`` implementation.

Audit item 8: previously only the in-memory ``FakeDeviceIO`` was exercised, so
the actual seam implementation (including the ``fileno()`` branch in ``sync`` and
the O_DIRECT flag construction) was unverified.
"""
import os

import pytest

from dwipe import device_io
from dwipe.device_io import PosixDeviceIO


# ---------------------------------------------------------------------------
# open_buffered
# ---------------------------------------------------------------------------
def test_open_buffered_read(tmp_path):
    path = tmp_path / "dev"
    path.write_bytes(b"hello world")
    with PosixDeviceIO().open_buffered(str(path), "rb") as f:
        assert f.read() == b"hello world"


def test_open_buffered_write(tmp_path):
    path = tmp_path / "dev"
    with PosixDeviceIO().open_buffered(str(path), "wb") as f:
        f.write(b"abc")
    assert path.read_bytes() == b"abc"


# ---------------------------------------------------------------------------
# open_direct / open_read flag construction
# ---------------------------------------------------------------------------
def test_open_direct_uses_o_direct_and_write_flag(monkeypatch):
    seen = {}

    def fake_os_open(path, flags):
        seen["path"], seen["flags"] = path, flags
        return 4242

    monkeypatch.setattr(os, "open", fake_os_open)
    impl = PosixDeviceIO()

    assert impl.open_direct("/dev/x", write=True) == 4242
    assert seen["path"] == "/dev/x"
    assert seen["flags"] & os.O_WRONLY
    assert seen["flags"] & os.O_DIRECT

    impl.open_direct("/dev/x", write=False)
    assert not (seen["flags"] & os.O_WRONLY)  # O_RDONLY == 0, so assert absence
    assert seen["flags"] & os.O_DIRECT


def test_open_read_uses_rdonly(monkeypatch):
    seen = {}

    def fake_os_open(path, flags):
        seen["flags"] = flags
        return 7

    monkeypatch.setattr(os, "open", fake_os_open)
    assert PosixDeviceIO().open_read("/dev/x") == 7
    assert seen["flags"] == os.O_RDONLY


# ---------------------------------------------------------------------------
# lseek / read / write / sync / close against a real fd
# ---------------------------------------------------------------------------
def test_lseek_read_close_real_fd(tmp_path):
    path = tmp_path / "dev"
    path.write_bytes(b"0123456789")
    impl = PosixDeviceIO()
    fd = impl.open_read(str(path))
    try:
        impl.lseek(fd, 3, os.SEEK_SET)
        assert impl.read(fd, 4) == b"3456"
    finally:
        impl.close(fd)


@pytest.mark.parametrize("whence,offset,expected", [
    (os.SEEK_SET, 2, 2),
    (os.SEEK_CUR, 3, 5),
    (os.SEEK_END, 0, 10),
])
def test_lseek_whence_variants(tmp_path, whence, offset, expected):
    path = tmp_path / "dev"
    path.write_bytes(b"0123456789")
    impl = PosixDeviceIO()
    fd = impl.open_read(str(path))
    try:
        impl.lseek(fd, 2, os.SEEK_SET)  # position at 2
        assert impl.lseek(fd, offset, whence) == expected
    finally:
        impl.close(fd)


def test_write_and_sync_raw_fd(tmp_path):
    path = tmp_path / "dev"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
    impl = PosixDeviceIO()
    try:
        assert impl.write(fd, b"xyz") == 3
        impl.sync(fd)  # raw fd path
    finally:
        impl.close(fd)
    assert path.read_bytes() == b"xyz"


def test_sync_accepts_file_object(tmp_path):
    path = tmp_path / "dev"
    with open(str(path), "w+b") as f:
        f.write(b"data")
        PosixDeviceIO().sync(f)  # exercises the fileno() branch


# ---------------------------------------------------------------------------
# Seam swap / reset
# ---------------------------------------------------------------------------
class _RecordingImpl:
    def open_buffered(self, path, mode="rb"):
        return ("buffered", path, mode)

    def open_direct(self, path, write=False):
        return ("direct", path, write)

    def open_read(self, path):
        return ("read", path)

    def lseek(self, handle, offset, whence=os.SEEK_SET):
        return ("lseek", handle, offset, whence)

    def read(self, handle, size):
        return ("read", handle, size)

    def write(self, handle, data):
        return ("write", handle, data)

    def sync(self, handle):
        return ("sync", handle)

    def close(self, handle):
        return ("close", handle)


def test_module_functions_dispatch_to_swapped_impl():
    device_io.use_device(_RecordingImpl())
    try:
        assert device_io.open_buffered("/d", "rb") == ("buffered", "/d", "rb")
        assert device_io.open_direct("/d", True) == ("direct", "/d", True)
        assert device_io.open_read("/d") == ("read", "/d")
        assert device_io.lseek(1, 2, os.SEEK_SET) == ("lseek", 1, 2, os.SEEK_SET)
        assert device_io.read(1, 5) == ("read", 1, 5)
        assert device_io.write(1, b"x") == ("write", 1, b"x")
        assert device_io.sync(1) == ("sync", 1)
        assert device_io.close(1) == ("close", 1)
    finally:
        device_io.reset()


def test_reset_restores_posix_impl():
    device_io.use_device(_RecordingImpl())
    device_io.reset()
    assert isinstance(device_io._impl, PosixDeviceIO)  # noqa: SLF001

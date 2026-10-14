"""In-memory device double for tests.

Implements the ``dwipe.device_io`` interface over a shared ``bytearray`` so the
write and verify loops can run against a fake "disk" without touching real
hardware. Install it with ``dwipe.device_io.use_device(fake)`` and restore the
real implementation with ``dwipe.device_io.reset()``.
"""
import os


class _FakeHandle:
    """A small stand-in for an integer file descriptor."""

    __slots__ = ("cursor", "writable")

    def __init__(self, writable):
        self.cursor = 0
        self.writable = writable


class _FakeFile:
    """Mimics the buffered file object returned by ``open()``."""

    def __init__(self, device, mode):
        self._dev = device
        self._mode = mode
        self._pos = 0
        self.closed = False

    def seek(self, offset, whence=os.SEEK_SET):
        if whence == os.SEEK_SET:
            self._pos = offset
        elif whence == os.SEEK_CUR:
            self._pos += offset
        elif whence == os.SEEK_END:
            self._pos = len(self._dev.data) + offset
        return self._pos

    def read(self, size=-1):
        if size is None or size < 0:
            end = len(self._dev.data)
        else:
            end = min(self._pos + size, len(self._dev.data))
        chunk = bytes(self._dev.data[self._pos:end])
        self._pos = end
        return chunk

    def write(self, data):
        data = bytes(data)
        end = self._pos + len(data)
        if end > len(self._dev.data):
            self._dev.data.extend(b"\x00" * (end - len(self._dev.data)))
        self._dev.data[self._pos:end] = data
        self._pos = end
        return len(data)

    def flush(self):
        return None

    def fileno(self):
        return -1

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeDeviceIO:
    """In-memory implementation of the ``device_io`` interface."""

    def __init__(self, size, fill=0):
        self.size = size
        if fill:
            self.data = bytearray([fill]) * size
        else:
            self.data = bytearray(size)
        self.handles = {}
        self._next_handle = 1

    # -- buffered ---------------------------------------------------------
    def open_buffered(self, path, mode="rb"):
        return _FakeFile(self, mode)

    # -- unbuffered handles ----------------------------------------------
    def open_direct(self, path, write=False):
        return self._new_handle(write)

    def open_read(self, path):
        return self._new_handle(False)

    def _new_handle(self, writable):
        handle = _FakeHandle(writable)
        hid = self._next_handle
        self._next_handle += 1
        self.handles[hid] = handle
        return hid

    def lseek(self, handle, offset, whence=os.SEEK_SET):
        h = self.handles[handle]
        if whence == os.SEEK_SET:
            h.cursor = offset
        elif whence == os.SEEK_CUR:
            h.cursor += offset
        elif whence == os.SEEK_END:
            h.cursor = len(self.data) + offset
        return h.cursor

    def read(self, handle, size):
        h = self.handles[handle]
        end = min(h.cursor + size, len(self.data))
        chunk = bytes(self.data[h.cursor:end])
        h.cursor = end
        return chunk

    def write(self, handle, data):
        h = self.handles[handle]
        data = bytes(data)
        end = h.cursor + len(data)
        if end > len(self.data):
            self.data.extend(b"\x00" * (end - len(self.data)))
        self.data[h.cursor:end] = data
        h.cursor = end
        return len(data)

    def sync(self, handle):
        return None

    def close(self, handle):
        self.handles.pop(handle, None)

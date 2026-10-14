"""Device I/O seam.

All block-device byte access (buffered marker reads/writes and unbuffered
O_DIRECT writes / plain reads) funnels through this module. The default
implementation delegates straight to the OS; tests can swap in an in-memory
device via ``use_device()`` (see ``tests/device_double.py``).

Callers must reference the module (``device_io.open_direct(...)``) rather than
importing the individual functions, so that swapping the implementation takes
effect everywhere.
"""
# pylint: disable=broad-exception-caught
import os


class PosixDeviceIO:
    """Real implementation - every operation delegates to ``os``/builtins."""

    def open_buffered(self, path, mode="rb"):
        """Open a device with regular (buffered) I/O. Returns a file object."""
        return open(path, mode)

    def open_direct(self, path, write=False):
        """Open a device with O_DIRECT for unbuffered, aligned I/O.

        Returns an integer file descriptor (as ``os.open`` does).
        """
        flags = (os.O_WRONLY if write else os.O_RDONLY) | os.O_DIRECT
        return os.open(path, flags)

    def open_read(self, path):
        """Open a device read-only (plain, unbuffered fd)."""
        return os.open(path, os.O_RDONLY)

    def lseek(self, handle, offset, whence=os.SEEK_SET):
        return os.lseek(handle, offset, whence)

    def read(self, handle, size):
        return os.read(handle, size)

    def write(self, handle, data):
        return os.write(handle, data)

    def sync(self, handle):
        """Flush to stable storage. Accepts a file object or a raw fd."""
        fd = handle.fileno() if hasattr(handle, "fileno") else handle
        os.fsync(fd)

    def close(self, handle):
        os.close(handle)


_impl = PosixDeviceIO()


def use_device(impl):
    """Swap in an alternative device implementation (used by tests)."""
    global _impl  # pylint: disable=global-statement
    _impl = impl


def reset():
    """Restore the default POSIX implementation (used by tests)."""
    global _impl  # pylint: disable=global-statement
    _impl = PosixDeviceIO()


def open_buffered(path, mode="rb"):
    """Open a device with regular (buffered) I/O (see PosixDeviceIO)."""
    return _impl.open_buffered(path, mode)


def open_direct(path, write=False):
    """Open a device with O_DIRECT (see PosixDeviceIO)."""
    return _impl.open_direct(path, write)


def open_read(path):
    """Open a device read-only (see PosixDeviceIO)."""
    return _impl.open_read(path)


def lseek(handle, offset, whence=os.SEEK_SET):
    """Seek within an open device handle."""
    return _impl.lseek(handle, offset, whence)


def read(handle, size):
    """Read from an open device handle."""
    return _impl.read(handle, size)


def write(handle, data):
    """Write to an open device handle."""
    return _impl.write(handle, data)


def sync(handle):
    """Flush an open device handle (or file object) to storage."""
    return _impl.sync(handle)


def close(handle):
    """Close an open device handle."""
    return _impl.close(handle)

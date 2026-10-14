"""Marker (16KB device header) helpers.

The marker lives at the very start of a device:

    [ 15KB zero fill ][ 1KB JSON ][ zero fill to 16KB ]

It records wipe progress (``scrubbed_bytes``), device ``size_bytes``, ``passes``
and the final ``mode`` ("Rand"/"Zero"), plus optional ``verify_status`` and
``abort_reason``. It enables crash recovery / resume.

These helpers are the single source of truth for building, parsing, reading and
writing markers; ``WipeJob``, ``WriteTask`` and ``FirmwareWipeTask`` all
delegate here.
"""
# pylint: disable=broad-exception-caught
import json
import time
from types import SimpleNamespace

from .WipeTask import WipeTask

# Fields accepted by parse_marker_buffer, with the type each must have.
_INT_FIELDS = ("unixtime", "scrubbed_bytes", "size_bytes", "passes")
_STR_FIELDS = ("mode", "verify_status", "abort_reason")


def build_marker_buffer(scrubbed_bytes, size_bytes, passes, is_random,
                        verify_status=None, abort_reason=None,
                        mode_override=None):
    """Build a 16KB marker buffer.

    Args:
        scrubbed_bytes: Total bytes written (including all passes).
        size_bytes: Total device size in bytes.
        passes: Number of passes intended/completed.
        is_random: bool; selects mode "Rand" vs "Zero" when ``mode_override``
            is not given.
        verify_status: "pass"/"fail" or None to omit.
        abort_reason: short error description or None to omit.
        mode_override: explicit mode string (e.g. a firmware wipe name).
    """
    if mode_override is not None:
        mode = mode_override
    else:
        mode = "Rand" if is_random else "Zero"

    data = {
        "unixtime": int(time.time()),
        "scrubbed_bytes": scrubbed_bytes,
        "size_bytes": size_bytes,
        "passes": passes,
        "mode": mode,
    }
    if verify_status is not None:
        data["verify_status"] = verify_status
    if abort_reason is not None:
        data["abort_reason"] = abort_reason
    return _encode_buffer(data)


def build_marker_from_existing(marker, verify_status=None, abort_reason=None,
                               mode_override=None):
    """Build a marker buffer that preserves fields from a parsed marker."""
    if mode_override is not None:
        mode = mode_override
    else:
        mode = marker.mode
    return build_marker_buffer(
        scrubbed_bytes=marker.scrubbed_bytes,
        size_bytes=marker.size_bytes,
        passes=getattr(marker, "passes", 1),
        is_random=(marker.mode == "Rand"),
        verify_status=verify_status,
        abort_reason=abort_reason,
        mode_override=mode,
    )


def _encode_buffer(data):
    """Serialise ``data`` into the fixed 16KB marker layout."""
    json_data = json.dumps(data).encode("utf-8")
    buffer = bytearray(WipeTask.MARKER_SIZE)
    buffer[:WipeTask.STATE_OFFSET] = b"\x00" * WipeTask.STATE_OFFSET
    buffer[WipeTask.STATE_OFFSET:WipeTask.STATE_OFFSET + len(json_data)] = json_data
    remaining_size = WipeTask.MARKER_SIZE - (WipeTask.STATE_OFFSET + len(json_data))
    buffer[WipeTask.STATE_OFFSET + len(json_data):] = b"\x00" * remaining_size
    return buffer


def parse_marker_buffer(buffer):
    """Parse a raw 16KB buffer into a SimpleNamespace, or None if invalid."""
    if buffer[:WipeTask.STATE_OFFSET] != b"\x00" * WipeTask.STATE_OFFSET:
        return None  # First 15 KB are not zeros

    json_data_bytes = buffer[WipeTask.STATE_OFFSET:WipeTask.MARKER_SIZE].rstrip(b"\x00")
    if not json_data_bytes:
        return None  # No JSON data found

    try:
        data = json.loads(json_data_bytes.decode("utf-8"))
    except (json.JSONDecodeError, Exception):
        return None  # Invalid JSON data!

    rv = {}
    for key, value in data.items():
        if key in _INT_FIELDS and isinstance(value, int):
            rv[key] = value
        elif key in _STR_FIELDS and isinstance(value, str):
            rv[key] = value
        else:
            return None  # bogus data

    # Old markers: 4 fields (no passes/verify_status/abort_reason)
    # New markers: 5 minimum (with passes), up to 7 with verify_status/abort_reason
    if len(rv) < 4 or len(rv) > 7:
        return None
    return SimpleNamespace(**rv)


def read_marker(device_name, io=None):
    """Read and parse the marker from ``/dev/<device_name>``.

    Args:
        device_name: Name relative to ``/dev`` (e.g. "sda1"), or any string to
            be appended to ``/dev/``.
        io: optional device I/O implementation (defaults to ``device_io``).

    Returns:
        SimpleNamespace or None.
    """
    if io is None:
        from . import device_io as io  # local import to avoid a cycle at import time
    try:
        with io.open_buffered(f"/dev/{device_name}", "rb") as device:
            device.seek(0)
            buffer = device.read(WipeTask.MARKER_SIZE)
    except Exception:
        return None  # cannot find info
    return parse_marker_buffer(buffer)


def write_marker(device_path, buffer, io, mode="wb"):
    """Write ``buffer`` to the start of ``device_path`` and flush it.

    ``mode`` defaults to "wb" (firmware markers) but callers updating an
    existing marker may pass "r+b".
    """
    with io.open_buffered(device_path, mode) as f:
        f.seek(0)
        f.write(buffer)
        f.flush()
        io.sync(f)

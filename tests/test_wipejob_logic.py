"""Unit tests for the pure / side-effect-light logic in dwipe.WipeJob.

Covers multi-pass pattern selection, marker (de)serialization, the random
buffer re-balancer, abort-reason extraction and on-disk pattern detection.
No real block device is touched.
"""
import builtins
import io
import json
from types import SimpleNamespace

import pytest

from dwipe.WipeJob import WipeJob
from dwipe.WipeTask import WipeTask

STATE_OFFSET = WipeTask.STATE_OFFSET
MARKER_SIZE = WipeTask.MARKER_SIZE


def make_job(passes=1, total_size=1_000_000, **opts):
    options = SimpleNamespace(passes=passes, slowdown_stop=64, stall_timeout=60, **opts)
    return WipeJob("/dev/loop0", total_size, opts=options)


def make_marker_buffer(data):
    js = json.dumps(data).encode("utf-8")
    buf = bytearray(MARKER_SIZE)
    buf[:STATE_OFFSET] = b"\x00" * STATE_OFFSET
    buf[STATE_OFFSET:STATE_OFFSET + len(js)] = js
    return bytes(buf)


# ---------------------------------------------------------------------------
# Multi-pass pattern selection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "pass_num,total,desired,expected",
    [
        (0, 1, "Rand", True),
        (0, 1, "Zero", False),
        # 2 passes ending on Rand -> Zero, Rand
        (0, 2, "Rand", False),
        (1, 2, "Rand", True),
        # 2 passes ending on Zero -> Rand, Zero
        (0, 2, "Zero", True),
        (1, 2, "Zero", False),
        # 4 passes ending on Rand -> Zero, Rand, Zero, Rand
        (0, 4, "Rand", False),
        (1, 4, "Rand", True),
        (2, 4, "Rand", False),
        (3, 4, "Rand", True),
        # 4 passes ending on Zero -> Rand, Zero, Rand, Zero
        (0, 4, "Zero", True),
        (1, 4, "Zero", False),
        (2, 4, "Zero", True),
        (3, 4, "Zero", False),
    ],
)
def test_get_pass_pattern_static(pass_num, total, desired, expected):
    assert WipeJob._get_pass_pattern_static(pass_num, total, desired) is expected


def test_get_pass_pattern_instance_matches_static():
    job = make_job(passes=4)
    for pn in range(4):
        for desired in ("Rand", "Zero"):
            assert (job.get_pass_pattern(pn, desired)
                    == WipeJob._get_pass_pattern_static(pn, 4, desired))


# ---------------------------------------------------------------------------
# Marker serialization
# ---------------------------------------------------------------------------
def test_prep_marker_buffer_layout():
    job = make_job(passes=2, total_size=5000)
    job.total_written = 1234
    buf = job.prep_marker_buffer(True, verify_status="pass", abort_reason="stall")

    assert len(buf) == MARKER_SIZE
    assert buf[:STATE_OFFSET] == b"\x00" * STATE_OFFSET

    payload = json.loads(bytes(buf[STATE_OFFSET:]).rstrip(b"\x00").decode())
    assert payload["scrubbed_bytes"] == 1234
    assert payload["size_bytes"] == 5000
    assert payload["passes"] == 2
    assert payload["mode"] == "Rand"
    assert payload["verify_status"] == "pass"
    assert payload["abort_reason"] == "stall"


def test_prep_marker_buffer_omits_optional_fields():
    job = make_job()
    job.total_written = 10
    buf = job.prep_marker_buffer(False)
    payload = json.loads(bytes(buf[STATE_OFFSET:]).rstrip(b"\x00").decode())
    assert payload["mode"] == "Zero"
    assert "verify_status" not in payload
    assert "abort_reason" not in payload


def test_read_marker_buffer_roundtrip(monkeypatch):
    buf = make_marker_buffer({
        "unixtime": 1, "scrubbed_bytes": 42, "size_bytes": 100,
        "passes": 1, "mode": "Rand",
    })
    monkeypatch.setattr(builtins, "open", lambda *a, **k: io.BytesIO(buf))

    marker = WipeJob.read_marker_buffer("loop0")
    assert marker.scrubbed_bytes == 42
    assert marker.size_bytes == 100
    assert marker.mode == "Rand"


def test_read_marker_buffer_rejects_nonzero_prefix(monkeypatch):
    buf = bytearray(make_marker_buffer({
        "unixtime": 1, "scrubbed_bytes": 1, "size_bytes": 2,
        "passes": 1, "mode": "Zero",
    }))
    buf[0] = 0xAB  # corrupt the leading zero region
    monkeypatch.setattr(builtins, "open", lambda *a, **k: io.BytesIO(bytes(buf)))
    assert WipeJob.read_marker_buffer("loop0") is None


def test_read_marker_buffer_rejects_unknown_keys(monkeypatch):
    buf = make_marker_buffer({
        "unixtime": 1, "scrubbed_bytes": 1, "size_bytes": 2,
        "passes": 1, "mode": "Zero", "bogus": "nope",
    })
    monkeypatch.setattr(builtins, "open", lambda *a, **k: io.BytesIO(buf))
    assert WipeJob.read_marker_buffer("loop0") is None


def test_read_marker_buffer_empty(monkeypatch):
    monkeypatch.setattr(builtins, "open",
                        lambda *a, **k: io.BytesIO(b"\x00" * MARKER_SIZE))
    assert WipeJob.read_marker_buffer("loop0") is None


# ---------------------------------------------------------------------------
# _rebalance_buffer
# ---------------------------------------------------------------------------
def test_rebalance_buffer_preserves_length_and_improves_zero_heavy():
    data = b"\x00" * 10000
    out = WipeJob._rebalance_buffer(data)
    assert isinstance(out, bytes)
    assert len(out) == len(data)
    # Far fewer zeros after re-balancing
    assert out.count(0) < 10000


def test_rebalance_buffer_already_balanced_is_unchanged():
    data = b"".join(bytes([i]) * 40 for i in range(256))
    out = WipeJob._rebalance_buffer(data)
    assert out == data


# ---------------------------------------------------------------------------
# Abort-reason extraction
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "exception,expected",
    [
        (None, None),
        ("Slowdown abort: (1.0MB/s) is 1/10 baseline", "slowdown"),
        ("STALL DETECTED: No progress for 30.0 seconds", "stall"),
        ("Marker write failed: traceback...", None),
        ("Some other internal error", None),
    ],
)
def test_extract_abort_reason(exception, expected):
    job = make_job()
    job.exception = exception
    assert job._extract_abort_reason() == expected


# ---------------------------------------------------------------------------
# detect_pattern_on_disk
# ---------------------------------------------------------------------------
def _sample_file(tail):
    # detect_pattern_on_disk() seeks past the marker area before reading
    return io.BytesIO(b"\x00" * WipeTask.WRITE_SIZE + tail)


def test_detect_pattern_on_disk_zeros(monkeypatch):
    monkeypatch.setattr(builtins, "open",
                        lambda *a, **k: _sample_file(b"\x00" * 4096))
    assert WipeJob.detect_pattern_on_disk("/dev/loop0") is False


def test_detect_pattern_on_disk_random(monkeypatch):
    monkeypatch.setattr(builtins, "open",
                        lambda *a, **k: _sample_file(bytes(range(256)) * 64))
    assert WipeJob.detect_pattern_on_disk("/dev/loop0") is True


def test_detect_pattern_on_disk_empty_is_zeros(monkeypatch):
    monkeypatch.setattr(builtins, "open", lambda *a, **k: _sample_file(b""))
    assert WipeJob.detect_pattern_on_disk("/dev/loop0") is False

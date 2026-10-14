"""Characterization tests for the shared verification helpers.

Item 2A: the verification algorithm used to exist twice (in ``WipeJob`` and
``VerifyTask``). It now lives once in ``dwipe.verification``; these tests lock
its behaviour so both call sites stay correct.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from device_double import FakeDeviceIO  # noqa: E402

from dwipe import verification  # noqa: E402
from dwipe.WipeTask import WipeTask  # noqa: E402

MARKER_SIZE = WipeTask.MARKER_SIZE
SECTION_SIZE = 24000  # enough samples per section to satisfy the >200-unique rule


def _uniform_device(total_size):
    fake = FakeDeviceIO(total_size)
    body = bytes(range(256)) * ((total_size // 256) + 1)
    fake.data[MARKER_SIZE:total_size] = body[:total_size - MARKER_SIZE]
    return fake


def _device_total():
    return MARKER_SIZE + 100 * SECTION_SIZE


# ---------------------------------------------------------------------------
# analyze_section_randomness
# ---------------------------------------------------------------------------
def test_analyze_section_randomness_uniform_is_random():
    counts = [40] * 256  # perfectly even distribution
    result, stats = verification.analyze_section_randomness(counts, 256 * 40)
    assert result == "random"
    assert stats["unique_bytes"] == 256


def test_analyze_section_randomness_all_zero_is_zeroed():
    counts = [0] * 256
    counts[0] = 5000
    result, _stats = verification.analyze_section_randomness(counts, 5000)
    assert result == "zeroed"


def test_analyze_section_randomness_insufficient_data():
    result, stats = verification.analyze_section_randomness([0] * 256, 50)
    assert result == "insufficient-data"
    assert stats["samples"] == 50


# ---------------------------------------------------------------------------
# verify_device
# ---------------------------------------------------------------------------
def test_verify_device_random_passes():
    total = _device_total()
    fake = _uniform_device(total)
    result, pattern, _sections = verification.verify_device(
        fake, "/dev/loop0", total, 100, "random", fast_fail=False)
    assert result == "random"
    assert pattern == "random"


def test_verify_device_zeros_fastfail():
    total = _device_total()
    fake = FakeDeviceIO(total)  # all zeros
    result, pattern, _sections = verification.verify_device(
        fake, "/dev/loop0", total, 100, "zeroed", fast_fail=True)
    assert result == "zeroed"
    assert pattern == "zeroed"


def test_verify_device_zeros_expected_random_fails():
    total = _device_total()
    fake = FakeDeviceIO(total)  # all zeros, but random was expected
    result, _pattern, _sections = verification.verify_device(
        fake, "/dev/loop0", total, 100, "random", fast_fail=False)
    assert result.startswith("not-wiped")


def test_verify_device_skip_when_zero_pct():
    fake = FakeDeviceIO(MARKER_SIZE + 4096)
    result, pattern, sections = verification.verify_device(
        fake, "/dev/loop0", fake.size, 0, None)
    assert result == "skipped"
    assert sections == []


def test_verify_device_unmarked_zero_disk_detects_zeroed():
    total = _device_total()
    fake = FakeDeviceIO(total)
    result, pattern, _sections = verification.verify_device(
        fake, "/dev/loop0", total, 100, None, fast_fail=False)
    assert result == "zeroed"
    assert pattern == "zeroed"

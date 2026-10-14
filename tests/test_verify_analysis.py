"""Unit tests for the statistical verification logic in dwipe.VerifyTask."""
from types import SimpleNamespace

from dwipe.VerifyTask import VerifyTask, VerifyZeroTask, VerifyRandTask


def make_task(cls=VerifyZeroTask, verify_pct=2):
    return cls("/dev/loop0", 1_000_000, SimpleNamespace(), verify_pct=verify_pct)


# ---------------------------------------------------------------------------
# _analyze_section_randomness
# ---------------------------------------------------------------------------
def test_analysis_insufficient_data():
    task = make_task()
    result, stats = task._analyze_section_randomness([0] * 256, 50)
    assert result == "insufficient-data"
    assert stats["samples"] == 50


def test_analysis_uniform_is_random():
    task = make_task()
    counts = [100] * 256  # 25600 samples, perfectly uniform
    result, stats = task._analyze_section_randomness(counts, sum(counts))
    assert result == "random"
    assert stats["unique_bytes"] == 256
    assert stats["cv"] == 0.0


def test_analysis_minimum_boundary_random():
    task = make_task()
    counts = [1] * 256  # exactly 256 samples, uniform
    result, _ = task._analyze_section_randomness(counts, 256)
    assert result == "random"


def test_analysis_zero_dominated_is_zeroed():
    task = make_task()
    counts = [0] * 256
    counts[0] = 1000
    result, stats = task._analyze_section_randomness(counts, 1000)
    assert result == "zeroed"
    assert stats["zero_freq"] == 1.0


def test_analysis_structured_data_is_not_wiped():
    task = make_task()
    counts = [0] * 256
    counts[10] = 50
    counts[20] = 50
    result, _ = task._analyze_section_randomness(counts, 100)
    assert result == "not-wiped"


def test_analysis_lumpy_distribution_is_not_random():
    task = make_task()
    # One byte dominates >2% -> not random, but not mostly zeros
    counts = [1] * 256
    counts[5] = 500
    result, _ = task._analyze_section_randomness(counts, sum(counts))
    assert result == "not-wiped"


# ---------------------------------------------------------------------------
# Task metadata / trivial behaviour
# ---------------------------------------------------------------------------
def test_subclass_flags_and_names():
    zero = make_task(VerifyZeroTask)
    rand = make_task(VerifyRandTask)
    assert zero.expected_pattern == "zeroed" and zero.fast_fail is True
    assert rand.expected_pattern == "random" and rand.fast_fail is False
    assert zero.get_display_name() == "Verify"
    assert rand.get_display_name() == "Verify"


def test_run_task_zero_pct_is_skipped():
    task = make_task(verify_pct=0)
    task.run_task()
    assert task.verify_result == "skipped"
    assert task.done is True

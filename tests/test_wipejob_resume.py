"""Direct tests for WipeJob resume planning and execution.

Item 2C: ``WipeJob.start_job`` resume logic was previously only exercised
indirectly. It is now split into a pure ``WipeJob.plan_job`` (planning) plus a
thin ``start_job`` (threading). ``plan_job`` is tested here across the resume
branch matrix; the execution half is tested against an in-memory device via the
``device_io`` seam.
"""
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from device_double import FakeDeviceIO  # noqa: E402

from dwipe import device_io, markers  # noqa: E402
from dwipe.WipeJob import WipeJob  # noqa: E402
from dwipe.WipeTask import WipeTask  # noqa: E402
from dwipe.WriteTask import WriteZeroTask, WriteRandTask  # noqa: E402
from dwipe.VerifyTask import VerifyZeroTask, VerifyRandTask  # noqa: E402

MARKER_SIZE = WipeTask.MARKER_SIZE


def make_opts(**overrides):
    opts = dict(passes=1, wipe_mode="Zero", verify_pct=2,
                slowdown_stop=64, stall_timeout=60)
    opts.update(overrides)
    return SimpleNamespace(**opts)


def make_marker(scrubbed, size, passes=1, mode="Zero"):
    return SimpleNamespace(unixtime=1, scrubbed_bytes=scrubbed,
                           size_bytes=size, passes=passes, mode=mode)


def no_detect_pattern(monkeypatch, is_random=False):
    monkeypatch.setattr(WipeJob, "detect_pattern_on_disk",
                        staticmethod(lambda *a, **k: is_random))


# ---------------------------------------------------------------------------
# plan_job: resume branch matrix
# ---------------------------------------------------------------------------
def test_plan_job_no_marker_starts_fresh(monkeypatch):
    no_detect_pattern(monkeypatch)
    opts = make_opts(passes=1, wipe_mode="Zero")
    tasks, resume_from, resume_mode = WipeJob.plan_job(
        "/dev/loop0", 1_000_000, opts, existing_marker=None, io=FakeDeviceIO(0))
    assert resume_from == 0
    assert resume_mode is None
    assert len(tasks) == 1
    assert isinstance(tasks[0], WriteZeroTask)
    assert tasks[0].resume_from == 0


def test_plan_job_completed_marker_starts_fresh(monkeypatch):
    no_detect_pattern(monkeypatch)
    opts = make_opts(passes=1, wipe_mode="Zero")
    marker = make_marker(1_000_000, 1_000_000, passes=1, mode="Zero")
    tasks, resume_from, resume_mode = WipeJob.plan_job(
        "/dev/loop0", 1_000_000, opts, marker)
    assert resume_from == 0
    assert resume_mode is None


def test_plan_job_partial_final_pass_pattern_match(monkeypatch):
    no_detect_pattern(monkeypatch, is_random=False)  # zeros on disk
    opts = make_opts(passes=1, wipe_mode="Zero")
    marker = make_marker(500_000, 1_000_000, passes=1, mode="Zero")
    tasks, resume_from, resume_mode = WipeJob.plan_job(
        "/dev/loop0", 1_000_000, opts, marker)
    assert resume_from == 500_000
    assert resume_mode == "Zero"
    assert tasks[0].resume_from == 500_000


def test_plan_job_final_pass_pattern_mismatch_restarts_pass(monkeypatch):
    no_detect_pattern(monkeypatch, is_random=True)  # random on disk, expected zero
    opts = make_opts(passes=2, wipe_mode="Zero")
    marker = make_marker(1_500_000, 1_000_000, passes=2, mode="Zero")
    tasks, resume_from, resume_mode = WipeJob.plan_job(
        "/dev/loop0", 1_000_000, opts, marker)
    # Mismatch -> restart the final pass from its beginning.
    assert resume_from == 1_000_000
    assert resume_mode == "Zero"


def test_plan_job_multi_pass_marks_completed_passes(monkeypatch):
    no_detect_pattern(monkeypatch)
    opts = make_opts(passes=3, wipe_mode="Zero")
    marker = make_marker(150_000, 100_000, passes=3, mode="Zero")
    tasks, resume_from, resume_mode = WipeJob.plan_job(
        "/dev/loop0", 100_000, opts, marker)
    assert resume_from == 150_000
    write_tasks = [t for t in tasks if isinstance(t, (WriteZeroTask, WriteRandTask))]
    assert len(write_tasks) == 3
    # Pass 0 is fully written already
    assert write_tasks[0].done is True
    assert write_tasks[0].total_written == 100_000
    # Pass 1 resumes midway
    assert write_tasks[1].resume_from == 50_000
    assert write_tasks[1].done is False
    # Pass 2 not started
    assert write_tasks[2].resume_from == 0


def test_plan_job_mode_change_discards_resume(monkeypatch):
    no_detect_pattern(monkeypatch)
    opts = make_opts(passes=2, wipe_mode="Rand")
    marker = make_marker(500_000, 1_000_000, passes=2, mode="Zero")
    tasks, resume_from, resume_mode = WipeJob.plan_job(
        "/dev/loop0", 1_000_000, opts, marker)
    assert resume_from == 0
    assert resume_mode is None
    assert all(t.resume_from == 0 for t in tasks
               if isinstance(t, (WriteZeroTask, WriteRandTask)))


@pytest.mark.parametrize("mode,verify_cls", [
    ("Zero+V", VerifyZeroTask),
    ("Rand+V", VerifyRandTask),
])
def test_plan_job_autoverify_appends_verify_task(monkeypatch, mode, verify_cls):
    no_detect_pattern(monkeypatch)
    opts = make_opts(passes=1, wipe_mode=mode, verify_pct=7)
    tasks, resume_from, resume_mode = WipeJob.plan_job(
        "/dev/loop0", 1_000_000, opts, None)
    assert isinstance(tasks[-1], verify_cls)
    assert tasks[-1].verify_pct == 7


def test_plan_job_reads_marker_through_io(monkeypatch):
    no_detect_pattern(monkeypatch)
    fake = FakeDeviceIO(1_000_000)
    buf = markers.build_marker_buffer(scrubbed_bytes=500_000, size_bytes=1_000_000,
                                      passes=1, is_random=False)
    fake.data[:len(buf)] = buf
    opts = make_opts(passes=1, wipe_mode="Zero")
    tasks, resume_from, resume_mode = WipeJob.plan_job(
        "/dev/loop0", 1_000_000, opts, existing_marker=None, io=fake)
    assert resume_from == 500_000


# ---------------------------------------------------------------------------
# Execution against an in-memory device
# ---------------------------------------------------------------------------
def test_run_tasks_full_zero_wipe_writes_final_marker():
    total = 49152  # MARKER_SIZE + 8 * 4096
    fake = FakeDeviceIO(total)
    device_io.use_device(fake)
    try:
        opts = make_opts(passes=1, wipe_mode="Zero")
        task = WriteZeroTask("/dev/loop0", total, opts)
        job = WipeJob("/dev/loop0", total, opts=opts, tasks=[task])
        job.run_tasks()
    finally:
        device_io.reset()

    marker = markers.parse_marker_buffer(bytes(fake.data[:MARKER_SIZE]))
    assert marker is not None
    assert marker.scrubbed_bytes == total
    assert marker.mode == "Zero"
    assert fake.data[MARKER_SIZE:total] == b"\x00" * (total - MARKER_SIZE)


def test_run_tasks_resumes_from_existing_marker():
    total = 49152
    resume_at = 24576
    fake = FakeDeviceIO(total, fill=0xAA)
    # Seed a partial marker and a "already written" zero region.
    buf = markers.build_marker_buffer(scrubbed_bytes=resume_at, size_bytes=total,
                                      passes=1, is_random=False)
    fake.data[:len(buf)] = buf
    fake.data[MARKER_SIZE:resume_at] = b"\x00" * (resume_at - MARKER_SIZE)

    device_io.use_device(fake)
    try:
        opts = make_opts(passes=1, wipe_mode="Zero")
        tasks, resume_from, resume_mode = WipeJob.plan_job(
            "/dev/loop0", total, opts, existing_marker=None, io=fake)
        assert resume_from == resume_at
        job = WipeJob("/dev/loop0", total, opts=opts, tasks=tasks)
        job.resume_from = resume_from
        job.resume_mode = resume_mode
        job.run_tasks()
    finally:
        device_io.reset()

    # Everything after the resume point was zeroed over the 0xAA filler.
    assert fake.data[resume_at:total] == b"\x00" * (total - resume_at)
    # Marker area (partially preserved zeros region) was left as zeros.
    assert fake.data[MARKER_SIZE:resume_at] == b"\x00" * (resume_at - MARKER_SIZE)

    marker = markers.parse_marker_buffer(bytes(fake.data[:MARKER_SIZE]))
    assert marker is not None
    assert marker.scrubbed_bytes == total
    assert marker.mode == "Zero"

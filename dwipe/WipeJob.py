"""WipeJob class for handling disk/partition wiping operations

 * Orchestrates task sequences (write and verify operations).
"""
import mmap
import os
import random
import threading
import time
import traceback
from types import SimpleNamespace

from . import device_io, markers
from .FirmwareWipeTask import FirmwareWipeTask
from .Utils import Utils
from .VerifyTask import VerifyRandTask, VerifyTask, VerifyZeroTask
from .WipeTask import WipeTask
from .WriteTask import WriteRandTask, WriteTask, WriteZeroTask


class WipeJob:
    """Handles disk/partition wiping operations with progress tracking."""

    @staticmethod
    def _get_dirty_kb():
        """Read Dirty pages from /proc/meminfo (in KB)"""
        try:
            with open('/proc/meminfo', encoding='utf-8') as f:
                for line in f:
                    if line.startswith('Dirty:'):
                        return int(line.split()[1])
        except (OSError, ValueError, IndexError):
            pass
        return 0

    @staticmethod
    def _get_total_memory_mb():
        """Read total system memory from /proc/meminfo (in MB)"""
        try:
            with open('/proc/meminfo', encoding='utf-8') as f:
                for line in f:
                    if line.startswith('MemTotal:'):
                        # Value is in KB, convert to MB
                        return int(line.split()[1]) // 1024
        except (OSError, ValueError, IndexError):
            pass
        return 8192  # Default to 8GB if we can't read

    @staticmethod
    def _rebalance_buffer(buffer):
        """Rebalance byte distribution to be more uniform

        Takes random data and redistributes bytes to make the distribution more uniform,
        which helps with chi-squared verification while maintaining cryptographic randomness.
        """
        byte_counts = [0] * 256
        for byte in buffer:
            byte_counts[byte] += 1

        expected = len(buffer) / 256 # expected frequency  if uniform 

        overused = []  # (byte_value, excess_count)
        underused = []  # (byte_value, deficit_count)

        for byte_val in range(256):
            count = byte_counts[byte_val]
            diff = count - expected
            if diff > expected * 0.05:  # More than 5% over expected
                overused.append((byte_val, int(diff * 0.7)))  # Replace 70% of excess
            elif diff < -expected * 0.05:  # More than 5% under expected
                underused.append((byte_val, int(-diff * 0.7)))  # Add to 70% of deficit

        if not overused or not underused:
            return buffer  # Already well-balanced

        result = bytearray(buffer)

        replacements_made = 0
        target_replacements = min(sum(count for _, count in overused),
                                  sum(count for _, count in underused))

        # Randomly replace overused bytes with underused bytes
        underused_idx = 0
        for overused_byte, excess in overused:
            if replacements_made >= target_replacements:
                break

            positions = [i for i, b in enumerate(result) if b == overused_byte]

            replace_count = min(excess, target_replacements - replacements_made)
            positions_to_replace = random.sample(positions, min(replace_count, len(positions)))

            for pos in positions_to_replace:
                if underused_idx >= len(underused):
                    break
                underused_byte, deficit = underused[underused_idx]
                result[pos] = underused_byte
                replacements_made += 1

                if replacements_made % max(1, deficit) == 0:
                    underused_idx += 1

        return bytes(result)

    def __init__(self, device_path, total_size, opts=None, tasks=None):
        """Initialize WipeJob as a task orchestrator

        Args:
         *  device_path: Path to device (e.g., '/dev/sda1')
         *  total_size: Total size in bytes
         *  opts: Options namespace
         *  tasks: List of WipeTask instances to execute sequentially
        """
        self.opts = opts
        self.device_path = device_path
        self.total_size = total_size
        self.do_abort = False
        self.thread = None

        self.tasks = tasks if tasks else []  # List of WipeTask instances
        self.current_task = None  # Currently executing task
        self.current_task_index = 0  # Index of current task

        self.start_mono = time.monotonic()  # Track the start time
        self.total_written = 0  # Will be set by tasks
        self.resume_from = 0  # Track resume offset
        self.resume_mode = None  # Original mode if resuming (overrides opts.wipe_mode)
        self.wr_hists = []  # list of (mono, written)
        self.done = False
        self.exception = None  # in case of issues

        self.passes = getattr(opts, 'passes', 1)  # Total number of passes to perform
        self.current_pass = 0  # Current pass number (0-indexed)

        self.verify_phase = False  # True when verifying
        self.verify_start_mono = None  # Start time of verify phase
        self.verify_progress = 0  # Bytes verified so far
        self.verify_pct = 0  # Percentage of disk being verified (e.g., 2 for 2%)
        self.verify_result = None  # "zeroed", "random", "not-wiped", "mixed"

        self.expected_pattern = None  # "zero" or "random" for what we wrote
        self.is_verify_only = False  # True if this is a standalone verify job

        self.last_marker_update_mono = time.monotonic() - 25  # Last time we wrote progress marker
        self.marker_update_interval = 30  # Update marker every 30 seconds

        self.slowdown_stop = getattr(opts, 'slowdown_stop', 64)
        self.stall_timeout = getattr(opts, 'stall_timeout', 60)
        self.max_slowdown_ratio = 0
        self.max_stall_secs = 0
        self.baseline_speed = None  # Bytes per second
        self.baseline_end_mono = None  # When baseline measurement ended
        self.last_progress_mono = time.monotonic()  # Last time we made progress
        self.last_progress_written = 0  # Bytes written at last progress check
        self.last_slowdown_check = 0
        self.wr_hists.append(SimpleNamespace(mono=self.start_mono, written=0))

        self.max_consecutive_errors = 3 # a control
        self.max_total_errors = 100 # a control
        self.reopen_on_error = True # a control
        self.reopen_count = 0 # cumulative (info only)
        self.total_errors = 0 # cumulative


    def run_tasks(self):
        """Execute task sequence and update WipeJob state from tasks

        This is the main orchestration method that runs each task in sequence.
        It proxies task state to WipeJob attributes for backwards compatibility.
        """
        stop_sync = [False]

        def sync_abort_flag():
            """Continuously sync WipeJob.do_abort to current task"""
            while not stop_sync[0]:
                if self.current_task and not self.current_task.do_abort:
                    self.current_task.do_abort = self.do_abort
                time.sleep(0.1)  # Check every 100ms

        sync_thread = threading.Thread(target=sync_abort_flag, daemon=True)
        sync_thread.start()

        all_writes_complete = False
        last_write_task_index = -1

        for i, task in enumerate(self.tasks):
            if isinstance(task, WriteTask):
                last_write_task_index = i

        try:
            for i, task in enumerate(self.tasks):
                if task.done:
                    continue
                if self.do_abort:
                    break

                self.current_task = task
                self.current_task_index = i
                task.job = self

                task.run_task()

                # Proxy task state back to WipeJob for
                # compatibility FIRST (before checking exceptions,
                # so we capture metrics even on failure) Write tasks
                if isinstance(task, WriteTask):
                    self.total_written = task.total_written
                    self.max_slowdown_ratio = max(self.max_slowdown_ratio, task.max_slowdown_ratio)
                    self.max_stall_secs = max(self.max_stall_secs, task.max_stall_secs)
                    self.total_errors = task.total_errors
                    self.reopen_count = task.reopen_count
                    # Set expected pattern from write task
                    if isinstance(task, WriteZeroTask):
                        self.expected_pattern = "zeroed"
                    elif isinstance(task, WriteRandTask):
                        self.expected_pattern = "random"

                    if i == last_write_task_index and not task.exception and not self.do_abort:
                        all_writes_complete = True
                        self._write_final_marker()

                elif isinstance(task, VerifyTask):
                    self.verify_phase = True
                    if self.verify_start_mono is None:
                        self.verify_start_mono = task.start_mono
                    self.verify_progress = task.total_written  # VerifyTask uses total_written for progress
                    self.verify_pct = task.verify_pct
                    # Extract verify result from task summary
                    summary = task.get_summary_dict()
                    self.verify_result = summary.get('result', None)

                    if not task.exception and not self.do_abort and self.verify_result:
                        is_random = (self.expected_pattern == "random")
                        self._write_marker_with_verify_status(is_random)

                if task.exception:
                    if isinstance(task, (WriteTask, FirmwareWipeTask)):
                        self.exception = task.exception
                        # Sync abort state before breaking
                        if task.do_abort:
                            self.do_abort = True
                        break

                if task.do_abort and not self.do_abort:
                    self.do_abort = True
                    break

        finally:
            stop_sync[0] = True # Stop abort sync thread

            # Write marker on stop to capture current progress (not just last 30s marker)
            if self.do_abort and self.total_written > 0 and not all_writes_complete:
                # Stopped mid-wipe - write progress marker
                is_random = False  # Default to zeros
                for task in reversed(self.tasks):
                    if isinstance(task, WriteTask) and task.total_written > 0:
                        is_random = isinstance(task, WriteRandTask)
                        break

                try:
                    abort_reason = self._extract_abort_reason()
                    with device_io.open_buffered(self.device_path, 'r+b') as marker_file:
                        marker_file.seek(0)
                        marker_file.write(self.prep_marker_buffer(is_random, verify_status=None,
                                                                   abort_reason=abort_reason))
                        marker_file.flush()
                        device_io.sync(marker_file)
                except Exception:
                    # Don't fail the stop on marker write error, but record it.
                    Utils.get_logger().debug(
                        f"stop marker write failed for {self.device_path}: "
                        f"{traceback.format_exc()}")

            self.done = True
            self.current_task = None

    @staticmethod
    def _get_pass_pattern_static(pass_number, total_passes, desired_mode):
        """Static version of get_pass_pattern for use in start_job()

        Args:
         *  pass_number: 0-indexed pass number
         *  total_passes: Total number of passes
         *  desired_mode: 'Rand' or 'Zero' - the final desired pattern

        Returns: bool: True for random, False for zeros
        """
        if total_passes == 1:
            return desired_mode == 'Rand'

        # Multi-pass: alternate patterns, ending on desired
        if pass_number == total_passes - 1:
            return desired_mode == 'Rand'

        if desired_mode == 'Rand':
            return pass_number % 2 == 1
        else:
            return pass_number % 2 == 0

    @staticmethod
    def plan_job(device_path, total_size, opts, existing_marker=None, io=None):
        """Compute resume offset/mode and build the task sequence (pure).

        This is the planning half of ``start_job``.

        Smart resume logic for multi-pass:
        - If resuming in the final pass, checks pattern on disk
        - If pattern matches expected for final pass, resumes from current position
        - If the pattern does not match (or cannot be read),
          restarts the final pass from its beginning
        This prevents mixed patterns (e.g., random then zeros) in the final result.

        Args:
         *  device_path: Path to device (e.g., '/dev/sda1').
         *  total_size: Total size in bytes.
         *  opts: Options namespace.
         *  existing_marker: parsed marker (or None). If None, the marker is
                read from the device using ``io``.
         *  io: optional device I/O implementation used to read the marker
                (defaults to the ``device_io`` module).

        Returns: tuple: (tasks, resume_from, resume_mode)
        """
        if io is None:
            io = device_io

        resume_from = 0
        resume_mode = None

        # Check for existing marker to resume from
        if existing_marker is None:
            device_name = device_path.replace('/dev/', '')
            existing_marker = markers.read_marker(device_name, io)
        if existing_marker and existing_marker.scrubbed_bytes > 0:
            scrubbed = existing_marker.scrubbed_bytes
            resume_mode = existing_marker.mode  # Use original mode ('Rand' or 'Zero')
            passes = getattr(opts, 'passes', 1)

            # Check if the marker indicates a completed wipe
            marker_passes = getattr(existing_marker, 'passes', 1)
            marker_target = total_size * marker_passes

            if scrubbed >= marker_target: # completed wipe
                resume_from = 0
                resume_mode = None
            else: # Partial/stopped wipe - resume from where it left off
                resume_from = scrubbed
                # Ensure we don't resume in the marker area
                if resume_from < WipeTask.MARKER_SIZE:
                    resume_from = WipeTask.MARKER_SIZE
                if resume_from > total_size * getattr(opts, 'passes', 1):
                    resume_from = 0  # Start over if marker corrupted

                current_pass = scrubbed // total_size
                last_pass_num = passes - 1

                if current_pass >= last_pass_num:  # final pass
                    expected_is_random = WipeJob._get_pass_pattern_static(
                        last_pass_num, passes, resume_mode)

                    actual_is_random = WipeJob.detect_pattern_on_disk(device_path)

                    if actual_is_random is None: # uncertain
                        resume_from = last_pass_num * total_size
                    elif expected_is_random != actual_is_random:
                        resume_from = last_pass_num * total_size
                    else: # Pattern matches - resume from current position
                        resume_from = scrubbed

        tasks = []
        mode = getattr(opts, 'wipe_mode', 'Rand')
        base_mode = mode.replace('+V', '')  # Remove verification suffix
        auto_verify = '+V' in mode
        passes = getattr(opts, 'passes', 1)

        if resume_mode and resume_mode != base_mode:
            # User changed wipe mode (e.g., Zero -> Rand) - start fresh
            resume_from = 0
            resume_mode = None

        desired_mode = resume_mode if resume_mode else base_mode

        for pass_num in range(passes):
            is_random = WipeJob._get_pass_pattern_static(pass_num, passes, desired_mode)

            pass_start = pass_num * total_size
            pass_resume = 0
            if resume_from > pass_start:
                pass_resume = resume_from - pass_start

            if is_random:
                task = WriteRandTask(device_path, total_size, opts,
                                    resume_from=pass_resume, pass_number=pass_num)
            else:
                task = WriteZeroTask(device_path, total_size, opts,
                                    resume_from=pass_resume, pass_number=pass_num)
            tasks.append(task)

            if resume_from >= (pass_num + 1) * total_size: # already complete
                task.done = True
                task.total_written = total_size

        if auto_verify:
            verify_pct = getattr(opts, 'verify_pct', 2)
            if desired_mode == 'Rand':
                task = VerifyRandTask(device_path, total_size, opts, verify_pct=verify_pct)
            else:
                task = VerifyZeroTask(device_path, total_size, opts, verify_pct=verify_pct)
            tasks.append(task)

        return tasks, resume_from, resume_mode

    @staticmethod
    def start_job(device_path, total_size, opts):
        """Start a wipe job in a background thread.

        If an existing marker with stopped state is found, resumes from that offset.
        Uses current passes setting to determine target bytes (may differ from original).

        Reads any existing marker, plans the resume/task sequence via ``plan_job``,
        then runs the tasks on a worker thread.
        """
        device_name = device_path.replace('/dev/', '')
        existing_marker = WipeJob.read_marker_buffer(device_name)
        tasks, resume_from, resume_mode = WipeJob.plan_job(
            device_path, total_size, opts, existing_marker)

        job = WipeJob(device_path=device_path, total_size=total_size, opts=opts, tasks=tasks)
        job.resume_from = resume_from
        job.resume_mode = resume_mode
        job.thread = threading.Thread(target=job.run_tasks)
        job.thread.start()
        return job

    @staticmethod
    def start_verify_job(device_path, total_size, opts, expected_pattern=None):
        """Start a verification-only job in a background thread

        Args:
         *  device_path: Path to device (e.g., '/dev/sda1')
         *  total_size: Total size in bytes
         *  opts: Options namespace with verify_pct
         *  expected_pattern: "zeroed", "random", or None (auto-detect)
        """
        device_name = os.path.basename(device_path)
        existing_marker = WipeJob.read_marker_buffer(device_name)
        if existing_marker and expected_pattern is None:
            expected_pattern = "random" if existing_marker.mode == 'Rand' else "zeroed"

        verify_pct = getattr(opts, 'verify_pct', 0)
        if verify_pct == 0:
            verify_pct = 2  # Default to 2% if not set

        tasks = []
        if expected_pattern == "random":
            task = VerifyRandTask(device_path, total_size, opts, verify_pct=verify_pct)
        else:
            task = VerifyZeroTask(device_path, total_size, opts, verify_pct=verify_pct)
        tasks.append(task)

        job = WipeJob(device_path=device_path, total_size=total_size, opts=opts, tasks=tasks)
        job.is_verify_only = True
        job.expected_pattern = expected_pattern
        job.verify_phase = True

        def verify_runner():
            try:
                job.run_tasks()

                if existing_marker:
                    is_random = existing_marker.mode == 'Rand'
                    job._write_marker_with_verify_status(is_random)
            except Exception:
                job.exception = traceback.format_exc()
            finally:
                if not job.done:
                    job.done = True

        job.thread = threading.Thread(target=verify_runner)
        job.thread.start()
        return job

    def get_status(self):
        """Get status tuple: (elapsed, percent, rate, eta, more_state)

        Returns stats for current phase only:
         * Write phase (0-100%): elapsed/rate/eta for writing
         * Flushing phase: 100% FLUSH while kernel syncs to device
         * Verify phase (v0-v100%): elapsed/rate/eta for verification only
         * more_state: optional extra status from derived task class
        """
        if self.current_task is not None:
            if isinstance(self.current_task, WriteTask):
                self.max_slowdown_ratio = max(self.max_slowdown_ratio, self.current_task.max_slowdown_ratio)
                self.max_stall_secs = max(self.max_stall_secs, self.current_task.max_stall_secs)
            return self.current_task.get_status()

        # No task is current yet or the job has finished and cleared it.  Keep UI happy.
        elapsed = Utils.ago_str(int(round(time.monotonic() - self.start_mono)))
        if self.do_abort:
            return elapsed, 'STOP', '0/s', '0', ''
        if self.done:
            return elapsed, 'v100%' if self.verify_phase else '100%', '0/s', '0', ''
        return elapsed, 'v0%' if self.verify_phase else '0%', '0/s', '0', ''

    def get_plan_dict(self, mode=None):
        """Generate plan dictionary for structured logging

        Args:
         *  mode: Optional mode override (e.g., 'Rand', 'Zero', 'Rand+V')
                  If None, uses self.opts.wipe_mode

        Returns: dict: Plan section with operation, steps, mode, verify settings, passes
        """
        if mode is None:
            mode = getattr(self.opts, 'wipe_mode', 'Unknown')

        steps = []

        base_mode = mode.replace('+V', '')
        verify_in_mode = '+V' in mode

        for pass_num in range(self.passes):
            if self.passes > 1:
                steps.append(f"wipe {base_mode} {self.device_path} (pass {pass_num + 1}/{self.passes})")
            else:
                steps.append(f"wipe {base_mode} {self.device_path}")

        verify_pct = getattr(self.opts, 'verify_pct', 0)
        if verify_in_mode or verify_pct > 0:
            if verify_pct > 0 and verify_pct < 100:
                steps.append(f"verify {base_mode} ({verify_pct}% sample)")
            else:
                steps.append(f"verify {base_mode}")

        return {
            "operation": "verify" if self.is_verify_only else "wipe",
            "steps": steps,
            "mode": base_mode,
            "verify_enabled": verify_in_mode or verify_pct > 0,
            "verify_pct": verify_pct,
            "passes": self.passes,
            "slowdown_stop_threshold": self.slowdown_stop,
            "stall_timeout_threshold": self.stall_timeout,
        }

    def get_summary_dict(self):
        """Generate complete summary dictionary for structured logging

        Returns: dict: Summary with top-level aggregates and per-step details
        """
        mono = time.monotonic()
        total_elapsed = mono - self.start_mono

        # Aggregate all task summaries
        steps = []
        total_errors = 0
        for task in self.tasks:
            task_summary = task.get_summary_dict()
            steps.append(task_summary)
            total_errors += task_summary.get('errors', 0)

        total_work = self.total_size * self.passes
        pct_complete = min(100, (self.total_written / total_work) * 100 if total_work > 0 else 0)

        abort_reason = None
        error_message = None
        if self.do_abort and self.exception:
            abort_reason = self._extract_abort_reason()
            error_message = self.exception

        summary = {
            "result": "stopped" if self.do_abort else ("failed" if self.exception else "completed"),
            "total_elapsed": Utils.ago_str(int(total_elapsed)),
            "total_errors": total_errors,
            "pct_complete": round(pct_complete, 1),
            "resumed_from_bytes": self.resume_from,
            "steps": steps,
        }

        if abort_reason:
            summary["abort_reason"] = abort_reason
        if error_message:
            summary["error_message"] = error_message

        return summary

    def _extract_abort_reason(self):
        """Extract short error reason from exception for marker

        Returns short lowercase error type like 'slowdown', 'stall',
        or None if no user-facing abort reason exists (only used for abnormal stops).
        """
        if not self.exception:
            return None

        exc_lower = self.exception.lower()

        # Check for known abort conditions
        if 'slowdown abort' in exc_lower:
            return 'slowdown'
        elif 'stall detected' in exc_lower:
            return 'stall'

        # Don't record internal errors in marker (marker write failures, tracebacks, etc.)
        return None

    def prep_marker_buffer(self, is_random, verify_status=None, abort_reason=None):
        """Get the 1st 16KB to write: - 15K zeros - JSON status + zero fill to 1KB

        Marker format (JSON):
            - unixtime: Unix timestamp when marker was written
            - scrubbed_bytes: Total bytes written (including all passes)
            - size_bytes: Total device size in bytes
            - passes: Number of passes intended/completed
            - mode: 'Rand' or 'Zero' (final desired pattern)
            - verify_status: 'pass', 'fail', or omitted (not verified)
            - abort_reason: short error description if job failed abnormally

        Args:
            is_random: bool, whether random data was written
            verify_status: str, "pass", "fail", or None (not verified)
            abort_reason: str, short error description or None (no error)
        """
        return markers.build_marker_buffer(
            scrubbed_bytes=self.total_written,
            size_bytes=self.total_size,
            passes=self.passes,
            is_random=is_random,
            verify_status=verify_status,
            abort_reason=abort_reason,
        )

    def get_pass_pattern(self, pass_number, desired_mode):
        """Return: True for random, False for zeros."""
        return WipeJob._get_pass_pattern_static(pass_number, self.passes, desired_mode)

    @staticmethod
    def detect_pattern_on_disk(device_path, sample_size=16*1024):
        """Read sample after header to return random(True), zero(False), unsure(None)."""
        try:
            with device_io.open_buffered(device_path, 'rb') as device:
                # Skip past the marker area (first 16KB)
                device.seek(WipeTask.WRITE_SIZE)
                data = device.read(sample_size)

                if not data:
                    return False  # Can't read, assume zeros

                non_zero_count = sum(1 for byte in data if byte != 0)
                if non_zero_count < len(data) * 0.01:
                    return False  # Zeros detected

                return True
        except OSError:
            Utils.get_logger().debug(
                f"detect_pattern_on_disk: cannot read {device_path}: "
                f"{traceback.format_exc()}")
            return None

    def _get_device_major_minor(self):
        """Get major:minor device numbers for the device

        Returns: str: "major:minor" or None if unable to determine
        """
        try:
            stat_info = os.stat(self.device_path)
            major = os.major(stat_info.st_rdev)
            minor = os.minor(stat_info.st_rdev)
            return f"{major}:{minor}"
        except OSError:
            return None

    @staticmethod
    def read_marker_buffer(device_name, io=None):
        """Open the device and read the first 16 KB (delegates to markers)."""
        return markers.read_marker(device_name, io)

    def _write_final_marker(self):
        """Write final marker after write tasks complete (shows 100% completion)

        This marker indicates the wipe is complete, regardless of verification.
        Does NOT include verify_status - that's just for display/logging.
        """
        try:
            is_random = False  # Default to zeros
            last_write_task = None
            for task in reversed(self.tasks):
                if isinstance(task, WriteTask):
                    last_write_task = task
                    break

            if last_write_task:
                is_random = isinstance(last_write_task, WriteRandTask)

            with device_io.open_buffered(self.device_path, 'r+b') as marker_file:
                marker_file.seek(0)
                marker_file.write(self.prep_marker_buffer(is_random, verify_status=None))
                marker_file.flush()
                device_io.sync(marker_file)

        except Exception:
            if not self.exception:  # Don't overwrite existing exception
                self.exception = f"Final marker write failed: {traceback.format_exc()}"

    def _write_marker_with_verify_status(self, is_random):
        """Write marker buffer with verification status if verification was performed

        Writes marker if:
         *  Verification was performed (verify_result is set)
         *  Verification status would change from current marker, OR
         *  No marker exists but verify passed (unmarked disk that's all zeros/random)
         *  Verification was not aborted
        """
        try:
            if not self.verify_result or self.verify_result == "skipped" or self.do_abort:
                return

            verify_result_base = self.verify_result.split(' ')[0] if ' ' in self.verify_result else self.verify_result

            if self.expected_pattern == "random" and verify_result_base == "random":
                new_verify_status = "pass"
            elif self.expected_pattern == "zeroed" and verify_result_base == "zeroed":
                new_verify_status = "pass"
            elif verify_result_base in ("not-wiped", "mixed", "error"):
                new_verify_status = "fail"
            else: # Mismatch: expected one pattern but got another
                new_verify_status = "fail"

            device_name = os.path.basename(self.device_path)
            existing_marker = WipeJob.read_marker_buffer(device_name)
            existing_verify_status = (getattr(existing_marker, 'verify_status', None)
                                      if existing_marker else None)

            # For unmarked disks that verified successfully, infer the mode from verify_result
            if not existing_marker and new_verify_status == "pass":
                if verify_result_base == "random":
                    is_random = True
                    self.total_written = self.total_size  # Mark as fully wiped
                elif verify_result_base == "zeroed":
                    is_random = False
                    self.total_written = self.total_size  # Mark as fully wiped
            elif existing_marker:
                if self.total_written == 0:
                    self.total_written = existing_marker.scrubbed_bytes
                    if existing_verify_status == new_verify_status:
                        return
            else: # No marker and verify failed - don't write marker
                return

            with device_io.open_buffered(self.device_path, 'r+b') as device:
                device.seek(0)
                marker_buffer = self.prep_marker_buffer(
                    is_random, verify_status=new_verify_status)
                device.write(marker_buffer)
                device.flush()
                device_io.sync(device)

        except Exception:
            self.exception = traceback.format_exc()
        finally: # ALWAYS set done, even if there was an exception
            self.done = True


if WipeTask.buffer is None:
    # Allocate random buffer with mmap (page-aligned for O_DIRECT)
    WipeTask.buffer_mem = mmap.mmap(-1, WipeTask.BUFFER_SIZE,
                                   flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
    raw_buffer = os.urandom(WipeTask.BUFFER_SIZE)
    rebalanced = WipeJob._rebalance_buffer(raw_buffer)
    WipeTask.buffer_mem.write(rebalanced)
    WipeTask.buffer_mem.seek(0)
    WipeTask.buffer = memoryview(WipeTask.buffer_mem)

    # Allocate zero buffer with mmap
    WipeTask.zero_buffer_mem = mmap.mmap(-1, WipeTask.WRITE_SIZE,
                                        flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
    WipeTask.zero_buffer_mem.write(b'\x00' * WipeTask.WRITE_SIZE)
    WipeTask.zero_buffer_mem.seek(0)
    WipeTask.zero_buffer = memoryview(WipeTask.zero_buffer_mem)

"""
WriteTask - Abstract base class and implementations for write operations

Includes:
- WriteTask: Abstract base class with write loop, error handling, performance monitoring
- WriteZeroTask: Concrete class for writing zeros
- WriteRandTask: Concrete class for writing random data
"""
# pylint: disable=broad-exception-raised,broad-exception-caught
import os
import time
import subprocess
import traceback
from types import SimpleNamespace

from . import device_io
from . import markers
from .WipeTask import WipeTask
from .Utils import Utils


class WriteTask(WipeTask):
    """Abstract base class for write operations (WriteZeroTask, WriteRandTask)

    Implements the main write loop with:
    - O_DIRECT unbuffered I/O for maximum performance
    - Error handling with safe_write() and reopen on error
    - Performance monitoring (stall/slowdown detection)
    - Periodic marker updates for crash recovery
    - Multi-pass support

    Subclasses must implement:
    - get_buffer(chunk_size): Return buffer slice (zeros or random)
    """

    def __init__(self, device_path, total_size, opts=None, resume_from=0, pass_number=0):
        """Initialize write task

        Args:
            device_path: Path to device (e.g., '/dev/sda1')
            total_size: Total size in bytes (single pass)
            opts: Options namespace
            resume_from: Byte offset to resume from (0 for fresh start)
            pass_number: Current pass number (0-indexed, for multi-pass)
        """
        super().__init__(device_path, total_size, opts)

        # Resume support
        self.resume_from = resume_from
        self.total_written = resume_from  # Start from resume offset
        self.current_pass = pass_number

        # Marker updates for crash recovery
        self.last_marker_update_mono = time.monotonic() - 25  # Last marker write
        self.marker_update_interval = 30  # Update every 30 seconds

        # Performance monitoring
        self.slowdown_stop = getattr(opts, 'slowdown_stop', 64)
        self.stall_timeout = getattr(opts, 'stall_timeout', 60)
        self.max_slowdown_ratio = 0
        self.max_stall_secs = 0
        self.baseline_speed = None  # Bytes per second baseline
        self.baseline_end_mono = None
        self.last_progress_mono = time.monotonic()
        self.last_progress_written = resume_from
        self.last_slowdown_check = 0

        # Error handling
        self.max_consecutive_errors = 3
        self.max_total_errors = 100
        self.reopen_on_error = True
        self.reopen_count = 0
        self.total_errors = 0

        # Initialize write history
        self.wr_hists = [SimpleNamespace(mono=self.start_mono, written=resume_from)]

    def get_buffer(self, chunk_size):
        """Get buffer slice for writing (abstract method)

        Args:
            chunk_size: Number of bytes to return

        Returns:
            memoryview: Buffer slice of requested size

        Must be implemented by subclasses:
        - WriteZeroTask returns WipeTask.zero_buffer[:chunk_size]
        - WriteRandTask returns WipeTask.buffer[:chunk_size]
        """
        raise NotImplementedError("Subclasses must implement get_buffer()")

    def run_task(self):
        """Execute write operation (blocking, runs in thread)"""
        try:
            # Set low I/O priority
            self._setup_ionice()

            # Open device with O_DIRECT for unbuffered I/O
            fd = device_io.open_direct(self.device_path, write=True)

            try:
                # Start from resume offset if resuming
                offset_in_pass = self.resume_from

                # SKIP MARKER AREA - don't overwrite it!
                if offset_in_pass < WipeTask.MARKER_SIZE:
                    self.total_written += WipeTask.MARKER_SIZE - offset_in_pass
                    offset_in_pass = WipeTask.MARKER_SIZE

                # Seek to current position (O_DIRECT requires block-aligned seeks)
                device_io.lseek(fd, offset_in_pass, os.SEEK_SET)

                # Write until end of pass
                bytes_to_write = self.total_size - offset_in_pass
                bytes_written_this_run = 0

                while bytes_written_this_run < bytes_to_write and not self.do_abort:
                    current_mono = time.monotonic()

                    # Update baseline if needed (first 60 seconds)
                    self._update_baseline_if_needed(current_mono)

                    # Check for stall (frequently)
                    if self._check_for_stall(current_mono):
                        break

                    # Check for slowdown (every 10 seconds)
                    if self.baseline_speed is not None:
                        time_since_last_check = current_mono - self.last_slowdown_check
                        if time_since_last_check >= 10:
                            if self._check_for_slowdown(current_mono):
                                break
                            self.last_slowdown_check = current_mono

                    # Update progress tracking
                    if self.total_written > self.last_progress_written:
                        self.last_progress_mono = current_mono
                        self.last_progress_written = self.total_written

                    # Calculate chunk size (must be block-aligned for O_DIRECT)
                    remaining = bytes_to_write - bytes_written_this_run
                    chunk_size = min(WipeTask.WRITE_SIZE, remaining)
                    # Round down to block boundary
                    chunk_size = (chunk_size // WipeTask.BLOCK_SIZE) * WipeTask.BLOCK_SIZE
                    if chunk_size == 0:
                        break

                    # Get buffer from subclass (polymorphic)
                    chunk = self.get_buffer(chunk_size)

                    try:
                        # Write with O_DIRECT (bypasses page cache)
                        bytes_written, fd = self.safe_write(fd, chunk)
                    except Exception as e:
                        # Save exception for debugging
                        self.exception = str(e)
                        self.do_abort = True
                        bytes_written = 0

                    self.total_written += bytes_written
                    bytes_written_this_run += bytes_written

                    # Periodically update marker for crash recovery (every 30s)
                    if self.total_written > WipeTask.MARKER_SIZE:
                        self.maybe_update_marker()

                    # Check for errors or incomplete writes
                    if bytes_written < chunk_size:
                        break

            finally:
                # Close device file descriptor
                if fd is not None:
                    device_io.close(fd)

            self.done = True
        except Exception:
            self.exception = traceback.format_exc()
            self.done = True

    def safe_write(self, fd, chunk):
        """Safe write with error recovery and reopen logic

        Args:
            fd: File descriptor
            chunk: Data to write

        Returns:
            tuple: (bytes_written, fd) - fd might be new if reopened

        Raises:
            Exception: If should abort (too many consecutive/total errors)
        """
        consecutive_errors = 0
        while True:  # Keep trying until success, skip, or abort
            try:
                bytes_written = device_io.write(fd, chunk)
                self.reopen_count = 0
                return bytes_written, fd  # success

            except Exception as e:
                consecutive_errors += 1
                self.total_errors += 1

                # Check if we should abort
                if consecutive_errors >= self.max_consecutive_errors:
                    raise Exception(f"{consecutive_errors} consecutive write errors") from e

                if self.total_errors >= self.max_total_errors:
                    raise Exception(f"{self.total_errors} total write errors") from e

                # Not fatal yet - try reopening if enabled
                if self.reopen_on_error:
                    try:
                        current_pos = self.total_written
                        # Open new fd first
                        new_fd = device_io.open_direct(self.device_path, write=True)
                        try:
                            # Seek to correct position on new fd
                            device_io.lseek(new_fd, current_pos, os.SEEK_SET)
                            # Only close old fd after new one is ready
                            old_fd = fd
                            fd = new_fd
                            try:
                                device_io.close(old_fd)
                            except Exception:
                                pass  # Old fd close failed, but new fd is good
                            self.reopen_count += 1
                        except Exception:
                            # New fd setup failed, close it and keep using old fd
                            device_io.close(new_fd)
                            raise
                    except Exception:
                        # Reopen failed - count as another error and retry with old fd
                        self.total_errors += 1

                # Retry the write (continue loop)

    def maybe_update_marker(self):
        """Periodically update marker to enable crash recovery

        Updates marker every marker_update_interval seconds (default 30s).
        This allows resume to work even after crashes, power loss, or kill -9.
        """
        now_mono = time.monotonic()
        if now_mono - self.last_marker_update_mono < self.marker_update_interval:
            return  # Not time yet

        # Marker writes use separate file handle (buffered I/O, not O_DIRECT)
        # because marker buffer is not aligned
        try:
            # Determine if this is a random or zero write
            is_random = isinstance(self, WriteRandTask)
            with device_io.open_buffered(self.device_path, 'r+b') as marker_file:
                marker_file.seek(0)
                marker_file.write(self._prep_marker_buffer(is_random))
            self.last_marker_update_mono = now_mono
        except Exception:
            # If marker update fails, just continue - we'll try again in 30s
            pass

    def _prep_marker_buffer(self, is_random):
        """Prepare marker buffer for this write task

        Args:
            is_random: bool, whether random data is being written

        Returns:
            bytearray: 16KB marker buffer with JSON status
        """
        return markers.build_marker_buffer(
            scrubbed_bytes=self.total_written,
            size_bytes=self.total_size,
            passes=1,  # Single pass per WriteTask
            is_random=is_random,
        )

    def _check_for_stall(self, current_monotonic):
        """Check for stall (no progress) - called frequently"""
        if self.stall_timeout <= 0:
            return False

        time_since_progress = current_monotonic - self.last_progress_mono
        self.max_stall_secs = max(time_since_progress, self.max_stall_secs)
        if time_since_progress >= self.stall_timeout:
            self.do_abort = True
            self.exception = f"Stall detected: No progress for {time_since_progress:.1f} seconds"
            return True

        return False

    def _check_for_slowdown(self, current_monotonic):
        """Check for slowdown - called every 10 seconds"""
        if self.slowdown_stop <= 0 or self.baseline_speed is None or self.baseline_speed <= 0:
            return False

        # Calculate current speed over last 30 seconds
        floor = current_monotonic - 30
        recent_history = [h for h in self.wr_hists if h.mono >= floor]

        if len(recent_history) >= 2:
            recent_start = recent_history[0]
            recent_written = self.total_written - recent_start.written
            recent_elapsed = current_monotonic - recent_start.mono

            if recent_elapsed > 1.0:
                current_speed = recent_written / recent_elapsed
                self.baseline_speed = max(self.baseline_speed, current_speed)
                slowdown_ratio = self.baseline_speed / max(current_speed, 1)
                slowdown_ratio = int(round(slowdown_ratio, 0))
                self.max_slowdown_ratio = max(self.max_slowdown_ratio, slowdown_ratio)

                if slowdown_ratio > self.slowdown_stop:
                    self.do_abort = True
                    self.exception = (f"Slowdown abort: ({Utils.human(current_speed)}B/s)"
                                     f" is 1/{slowdown_ratio} baseline")
                    return True

        return False

    def _update_baseline_if_needed(self, current_monotonic):
        """Update baseline speed measurement if still in first 60 seconds"""
        if self.baseline_speed is not None:
            return  # Baseline already established

        if (current_monotonic - self.start_mono) >= 60:
            total_written_60s = self.total_written - self.resume_from
            elapsed_60s = current_monotonic - self.start_mono
            if elapsed_60s > 0:
                self.baseline_speed = total_written_60s / elapsed_60s
                self.baseline_end_mono = current_monotonic
                self.last_slowdown_check = current_monotonic  # Start slowdown checking

    def _setup_ionice(self):
        """Setup I/O priority to best-effort class, lowest priority"""
        try:
            # Class 2 = best-effort, priority 7 = lowest (0 is highest, 7 is lowest)
            subprocess.run(["ionice", "-c", "2", "-n", "7", "-p", str(os.getpid())],
                          capture_output=True, check=False)
        except Exception:
            pass

    def get_summary_dict(self):
        """Get final summary for this write task

        Returns:
            dict: Summary with step name, elapsed, rate, bytes written, errors, etc.
        """
        mono = time.monotonic()
        elapsed = mono - self.start_mono
        rate_bps = self.total_written / elapsed if elapsed > 0 else 0

        # Determine mode from class name
        mode = "Rand" if isinstance(self, WriteRandTask) else "Zero"

        return {
            "step": f"wipe {mode} {self.device_path}",
            "elapsed": Utils.ago_str(int(elapsed)),
            "rate": f"{Utils.human(int(rate_bps))}/s",
            "bytes_written": self.total_written,
            "bytes_total": self.total_size,
            "passes_total": 1,  # Single pass per WriteTask
            "passes_completed": 1 if self.done and not self.exception else 0,
            "current_pass": self.current_pass,
            "peak_write_rate": f"{Utils.human(int(self.baseline_speed))}/s" if self.baseline_speed else None,
            "worst_stall": Utils.ago_str(int(self.max_stall_secs)),
            "worst_slowdown_ratio": round(self.max_slowdown_ratio, 1),
            "errors": self.total_errors,
            "reopen_count": self.reopen_count,
        }


class WriteZeroTask(WriteTask):
    """Write zeros to disk"""

    def get_buffer(self, chunk_size):
        """Return zero buffer slice"""
        return WipeTask.zero_buffer[:chunk_size]

    def get_display_name(self):
        """Get display name for zeros write"""
        return "Zero"


class WriteRandTask(WriteTask):
    """Write random data to disk"""

    def get_buffer(self, chunk_size):
        """Return random buffer slice"""
        return WipeTask.buffer[:chunk_size]

    def get_display_name(self):
        """Get display name for random write"""
        return "Rand"

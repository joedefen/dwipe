"""WipeTask - Abstract base class for wipe/verify operations

 * Defines the interface and shared state for all task types.
"""
import time
from types import SimpleNamespace

from .Utils import Utils


class WipeTask:
    """Abstract base class for wipe/verify operations

    Defines the interface that all tasks must
    implement: - run_task(): Execute the task (blocking,
    runs in thread) - get_status(): Get current progress (thread-safe,
    called from main thread) - get_summary_dict(): Get final
    summary after completion - abort(): Signal task to stop

    Shared state across all task types: - Device info: device_path,
    total_size - Control: opts, do_abort, done - Progress: total_written,
    start_mono, wr_hists - Errors: exception
    """

    # O_DIRECT requires aligned buffers and write sizes
    BLOCK_SIZE = 4096  # Alignment requirement for O_DIRECT
    WRITE_SIZE = 1 * 1024 * 1024  # 1MB (must be multiple of BLOCK_SIZE)
    BUFFER_SIZE = WRITE_SIZE  # Same size for O_DIRECT

    # Marker constants (separate from O_DIRECT writes)
    MARKER_SIZE = 16 * 1024  # 16KB for marker
    STATE_OFFSET = 15 * 1024  # where json is written (for marker buffer)

    # Aligned buffers allocated with mmap (initialized at module load)
    buffer = None  # Random data buffer (memoryview)
    buffer_mem = None  # Underlying mmap object
    zero_buffer = None  # Zero buffer (memoryview)
    zero_buffer_mem = None  # Underlying mmap object

    def __init__(self, device_path, total_size, opts=None):
        """Initialize base task with common attributes

        Args:
            device_path: Path to device (e.g., '/dev/sda1')
            total_size: Total size in bytes
            opts: Options namespace (wipe_mode, verify_pct, etc.)
        """
        self.device_path = device_path
        self.total_size = total_size
        self.opts = opts

        # Control flags
        self.do_abort = False
        self.done = False
        self.exception = None
        self.more_state = ""  # Optional extra status info from derived classes

        # Progress tracking
        self.total_written = 0  # Bytes processed (write or verify)
        self.start_mono = time.monotonic()
        self.wr_hists = []  # Progress history: list of SimpleNamespace(mono, written)
        self.wr_hists.append(SimpleNamespace(mono=self.start_mono, written=0))

    def run_task(self):
        """Execute the task (blocking, runs in thread)

        Must be implemented by subclasses.
        Should: - Perform the actual work (write or verify) - Update
        self.total_written as it progresses - Check self.do_abort
        periodically and stop if True - Set self.exception if errors
        occur - Set self.done = True when complete (or use finally block)
        """
        raise NotImplementedError("Subclasses must implement run_task()")

    def get_status(self):
        """Get current progress status (thread-safe, called from main thread)

        Returns:
            tuple: (elapsed_str, pct_str, rate_str, eta_str, more_state)
                - elapsed_str: e.g., "5m23s"
                - pct_str: e.g., "45%" or "v23%" (for verify)
                - rate_str: e.g., "450MB/s"
                - eta_str: e.g., "2m15s"
                - more_state: optional extra status from derived class
        """
        mono = time.monotonic()
        elapsed_time = mono - self.start_mono

        # Calculate percentage
        pct = (self.total_written / self.total_size) * 100 if self.total_size > 0 else 0
        pct = min(pct, 100)
        pct_str = f'{int(round(pct))}%'

        if self.do_abort:
            pct_str = 'STOP'

        # Track progress for rate calculation
        self.wr_hists.append(SimpleNamespace(mono=mono, written=self.total_written))
        floor = mono - 30  # 30 second window
        while len(self.wr_hists) >= 3 and self.wr_hists[1].mono >= floor:
            del self.wr_hists[0]

        # Calculate rate from sliding window
        delta_mono = mono - self.wr_hists[0].mono
        rate = (self.total_written - self.wr_hists[0].written) / delta_mono if delta_mono > 1.0 else 0
        rate_str = f'{Utils.human(int(round(rate, 0)))}/s'

        # Calculate ETA
        if rate > 0:
            remaining = self.total_size - self.total_written
            when = int(round(remaining / rate))
            when_str = Utils.ago_str(when)
        else:
            when_str = '0'

        return Utils.ago_str(int(round(elapsed_time))), pct_str, rate_str, when_str, self.more_state

    def get_summary_dict(self):
        """Get final summary after task completion

        Returns:
            dict: Summary with step name, elapsed, rate, bytes processed, etc.
        """
        mono = time.monotonic()
        elapsed = mono - self.start_mono
        rate_bps = self.total_written / elapsed if elapsed > 0 else 0

        return {
            "step": f"task {self.__class__.__name__}",
            "elapsed": Utils.ago_str(int(elapsed)),
            "rate": f"{Utils.human(int(rate_bps))}/s",
            "bytes_processed": self.total_written,
        }

    def get_display_name(self):
        """Get human-readable name for this task type (for progress display)

        Returns:
            str: Task name like "Zero", "Rand", "Crypto", "Verify", etc.
        """
        return "Task"  # Default, should be overridden

    def abort(self):
        """Signal task to stop (thread-safe)"""
        self.do_abort = True

"""
VerifyTask - Abstract base class and implementations for verification operations

Includes:
- VerifyTask: Abstract base class with verification logic and statistical analysis
- VerifyZeroTask: Concrete class for verifying zeros (fast memcmp)
- VerifyRandTask: Concrete class for verifying random data (statistical)
"""
# pylint: disable=broad-exception-raised,broad-exception-caught
import time
import traceback
from types import SimpleNamespace

from . import device_io
from . import verification
from .WipeTask import WipeTask
from .Utils import Utils


class VerifyTask(WipeTask):
    """Abstract base class for verify operations (VerifyZeroTask, VerifyRandTask)

    Implements verification logic with:
    - Section-by-section analysis of disk content
    - Fast-fail for zero verification (memcmp)
    - Statistical analysis for random pattern verification
    - Progress tracking

    The heavy lifting lives in ``verification.verify_device``; subclasses set:
    - expected_pattern: "zeroed" or "random"
    - fast_fail: True for zero (fast memcmp), False for random (statistical)
    """

    def __init__(self, device_path, total_size, opts=None, verify_pct=2, expected_pattern=None):
        """Initialize verify task

        Args:
            device_path: Path to device (e.g., '/dev/sda1')
            total_size: Total size in bytes
            opts: Options namespace
            verify_pct: Percentage of disk to verify (e.g., 2 for 2%)
            expected_pattern: "zeroed", "random", or None (auto-detect)
        """
        super().__init__(device_path, total_size, opts)

        # Verify-specific attributes
        self.verify_pct = verify_pct
        self.expected_pattern = expected_pattern
        self.verify_result = None  # "zeroed", "random", "not-wiped", "mixed", "error"
        self.section_results = []  # Section-by-section results
        self.verify_progress = 0  # Bytes verified (for total_written tracking)

        # Fast-fail flag (set by subclasses)
        self.fast_fail = False

    def run_task(self):
        """Execute verification operation (blocking, runs in thread)"""
        try:
            if self.verify_pct == 0:
                self.verify_result = "skipped"
                self.done = True
                return

            def on_progress(nbytes):
                self.verify_progress += nbytes
                self.total_written = self.verify_progress  # Update for get_status()

            result, pattern, sections = verification.verify_device(
                device_io,
                self.device_path,
                self.total_size,
                self.verify_pct,
                self.expected_pattern,
                fast_fail=self.fast_fail,
                is_aborted=lambda: self.do_abort,
                on_progress=on_progress,
            )

            self.verify_result = result
            self.expected_pattern = pattern
            self.section_results = sections
            self.done = True
        except Exception:
            self.exception = traceback.format_exc()
            self.verify_result = "error"
            self.done = True

    def _analyze_section_randomness(self, byte_counts, total_samples):
        """Analyze if a section appears random (delegates to verification)."""
        return verification.analyze_section_randomness(byte_counts, total_samples)

    def get_status(self):
        """Get current progress status (thread-safe, called from main thread)

        Returns:
            tuple: (elapsed_str, pct_str, rate_str, eta_str, more_state)
            - pct_str has 'v' prefix (e.g., "v45%")
        """
        mono = time.monotonic()
        elapsed_time = mono - self.start_mono

        # Calculate total bytes to verify (verify_pct% of total_size)
        if self.verify_pct > 0:
            total_to_verify = self.total_size * self.verify_pct / 100
        else:
            total_to_verify = self.total_size

        # Calculate verification percentage (0-100)
        pct = int((self.verify_progress / total_to_verify) * 100) if total_to_verify > 0 else 0
        pct_str = f'v{pct}%'

        if self.do_abort:
            pct_str = 'STOP'

        # Track verification progress for rate calculation
        self.wr_hists.append(SimpleNamespace(mono=mono, written=self.verify_progress))
        floor = mono - 30
        while len(self.wr_hists) >= 3 and self.wr_hists[1].mono >= floor:
            del self.wr_hists[0]

        delta_mono = mono - self.wr_hists[0].mono
        physical_rate = (self.verify_progress - self.wr_hists[0].written) / delta_mono if delta_mono > 1.0 else 0
        # Scale rate to show "effective" verification rate (as if verifying 100% of disk)
        effective_rate = physical_rate * (100 / self.verify_pct) if self.verify_pct > 0 else physical_rate
        rate_str = f'{Utils.human(int(round(effective_rate, 0)))}/s'

        if physical_rate > 0:
            remaining = total_to_verify - self.verify_progress
            when = int(round(remaining / physical_rate))
            when_str = Utils.ago_str(when)
        else:
            when_str = '0'

        return Utils.ago_str(int(round(elapsed_time))), pct_str, rate_str, when_str, self.more_state

    def get_summary_dict(self):
        """Get final summary for this verify task

        Returns:
            dict: Summary with step name, elapsed, rate, bytes checked, result
        """
        mono = time.monotonic()
        elapsed = mono - self.start_mono
        rate_bps = self.verify_progress / elapsed if elapsed > 0 else 0

        # Determine mode from expected pattern
        mode = "Rand" if self.expected_pattern == "random" else "Zero"

        # Build verify label
        verify_label = f"verify {mode}"
        if self.verify_pct > 0 and self.verify_pct < 100:
            verify_label += f" ({self.verify_pct}% sample)"

        # Extract verify detail if present
        verify_detail = None
        if self.verify_result and '(' in str(self.verify_result):
            verify_detail = str(self.verify_result).split('(')[1].rstrip(')')

        result = {
            "step": verify_label,
            "elapsed": Utils.ago_str(int(elapsed)),
            "rate": f"{Utils.human(int(rate_bps))}/s",
            "bytes_checked": self.verify_progress,
            "result": self.verify_result,
        }

        if verify_detail:
            result["verify_detail"] = verify_detail

        return result


class VerifyZeroTask(VerifyTask):
    """Verify disk contains zeros"""

    def __init__(self, device_path, total_size, opts=None, verify_pct=2):
        super().__init__(device_path, total_size, opts, verify_pct, expected_pattern="zeroed")
        self.fast_fail = True  # Use fast memcmp verification

    def get_display_name(self):
        """Get display name for zero verification"""
        return "Verify"


class VerifyRandTask(VerifyTask):
    """Verify disk contains random pattern"""

    def __init__(self, device_path, total_size, opts=None, verify_pct=2):
        super().__init__(device_path, total_size, opts, verify_pct, expected_pattern="random")
        self.fast_fail = False  # Use statistical analysis

    def get_display_name(self):
        """Get display name for random verification"""
        return "Verify"

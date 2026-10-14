"""
FirmwareWipeTask - Firmware-based secure erase operations

Includes:
- FirmwareWipeTask: Abstract base class for firmware wipes
- NvmeWipeTask: NVMe secure erase using nvme-cli
- SataWipeTask: SATA/ATA secure erase using hdparm
"""
# pylint: disable=broad-exception-raised,broad-exception-caught
# pylint: disable=invalid-name,too-many-instance-attributes,too-many-arguments
# pylint: disable=too-many-positional-arguments,consider-using-with
# pylint: disable=too-many-return-statements
import os
import time
import random
import subprocess
import traceback

from . import device_io
from . import markers
from .WipeTask import WipeTask
from .Utils import Utils
from .SataTool import SataTool
from .NvmeTool import NvmeTool


class FirmwareWipeTask(WipeTask):
    """Abstract base class for firmware-based wipe operations

    Firmware wipes execute in the drive's controller, not via CPU writes.
    The host just sends a command and monitors for completion.

    Progress reporting is estimated since most firmware doesn't report
    real-time progress. NVMe sanitize can optionally poll for actual progress.

    Subclasses must implement:
    - _build_command(): Return list of command args
    - _check_completion(): Check if wipe is complete
    - _estimate_duration(): Estimate total time in seconds
    """

    def __init__(self, device_path, total_size, opts, command_args, wipe_name):
        """Initialize firmware wipe task

        Args:
            device_path: Path to device (e.g., '/dev/sda', '/dev/nvme0n1')
            total_size: Total size in bytes
            opts: Options namespace
            command_args: Command args from hw_caps (e.g., 'sanitize --action=0x04')
            wipe_name: Human-readable name (e.g., 'Sanitize-Crypto')
        """
        super().__init__(device_path, total_size, opts)

        self.command_args = command_args
        self.wipe_name = wipe_name
        self.process = None
        self.finish_mono = None
        self.actual_command = None  # Store actual command that was executed
        self.return_code = None     # Store process return code
        self.stderr_output = None   # Store stderr output on failure
        self.elapsed_secs = 0       # Capture elapsed time when task completes

        # Estimated duration for progress reporting
        self.estimated_duration = self._estimate_duration()

    def _estimate_duration(self):
        """Estimate total duration in seconds (override in subclasses)

        Returns:
            int: Estimated seconds for completion
        """
        return 60  # Default 1 minute

    def get_display_name(self):
        """Get display name for firmware wipe"""
        return self.wipe_name

    def _build_command(self):
        """Build command list for subprocess (must be implemented by subclasses)

        Returns:
            list: Command args like ['nvme', 'sanitize', ...]
        """
        raise NotImplementedError("Subclasses must implement _build_command()")

    def _check_completion(self):
        """Check if wipe completed successfully (can be overridden)

        Returns:
            bool or None: True if done, False if failed, None if still running
        """
        if self.process and self.process.poll() is not None:
            return self.process.returncode == 0
        return None

    def _write_marker(self):
        """Write completion marker after firmware wipe

        Firmware wipes erase the entire disk including any existing markers.
        We need to write a new marker indicating the wipe is complete.
        Retries on I/O errors as drives may need time to stabilize after sanitize.
        """
        max_retries = 20
        retry_delay = 1.0  # seconds

        for attempt in range(max_retries):
            try:
                # Force OS to re-read partition table (now empty)
                subprocess.run(['blockdev', '--rereadpt', self.device_path],
                              capture_output=True, timeout=5, check=False)
                time.sleep(0.5)  # Let kernel settle

                # Prepare marker data
                buffer = markers.build_marker_buffer(
                    scrubbed_bytes=self.total_size,
                    size_bytes=self.total_size,
                    passes=1,
                    is_random=False,
                    mode_override=self.wipe_name,  # e.g., 'Crypto', 'Enhanced'
                )

                # Write marker to beginning of device
                markers.write_marker(self.device_path, buffer, device_io, mode='wb')

                # Success
                return

            except OSError as e:
                # I/O errors (errno 5 = EIO) may be transient after sanitize
                if e.errno == 5 and attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    continue
                # For non-I/O errors or final attempt, give up
                self.exception = f"Marker write warning: {e}"
                return

            except Exception as e:
                # Don't fail the whole job if marker write fails
                self.exception = f"Marker write warning: {e}"
                return

    def run_task(self):
        """Execute firmware wipe operation (blocking, runs in thread)"""
        try:
            # Build command
            cmd = self._build_command()
            # Only set actual_command if cmd is non-empty (subclass may have already set it)
            if cmd:
                self.actual_command = cmd

            # Start subprocess (non-blocking) - skip if process already started (e.g., NVMe)
            if cmd:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )

            # Monitor progress with polling loop
            check_interval = 2  # Check every 2 seconds

            while not self.do_abort:
                # Check if process completed
                completion_status = self._check_completion()

                if completion_status is True:
                    # Success!
                    self.total_written = self.total_size
                    self.finish_mono = time.monotonic()

                    # Capture return code
                    if self.process:
                        self.return_code = self.process.returncode

                    # Write marker after successful firmware wipe
                    self._write_marker()
                    break

                if completion_status is False:
                    # Failed
                    stderr = self.process.stderr.read() if self.process.stderr else ""
                    self.stderr_output = stderr  # Store for logging
                    self.exception = f"Firmware wipe failed: {stderr}"

                    # Capture return code on failure too
                    if self.process:
                        self.return_code = self.process.returncode
                    break

                # Still running - update estimated progress
                elapsed = time.monotonic() - self.start_mono
                progress_pct = min(1.0, elapsed / self.estimated_duration)
                self.total_written = int(self.total_size * progress_pct)

                time.sleep(check_interval)

            # Handle abort
            if self.do_abort and self.process:
                self.process.terminate()
                time.sleep(0.5)
                if self.process.poll() is None:
                    self.process.kill()

        except Exception:
            self.exception = traceback.format_exc()
        finally:
            # Capture elapsed time when task completes
            self.elapsed_secs = int(time.monotonic() - self.start_mono)
            self.done = True

    def get_status(self):
        """Get current progress status (thread-safe)

        Returns:
            tuple: (elapsed_str, pct_str, rate_str, eta_str, more_state)
        """
        mono = time.monotonic()
        elapsed_time = mono - self.start_mono

        # Calculate percentage based on estimated progress
        pct = (self.total_written / self.total_size) * 100 if self.total_size > 0 else 0
        pct = min(pct, 100)
        pct_str = f'{int(round(pct))}%'

        if self.do_abort:
            pct_str = 'STOP'

        # Show "FW" to indicate firmware operation
        rate_str = 'FW'

        # Calculate ETA based on estimated duration
        if pct < 100:
            remaining = self.estimated_duration - elapsed_time
            eta_str = Utils.ago_str(max(0, int(remaining)))
        else:
            eta_str = '0'

        elapsed_str = Utils.ago_str(int(round(elapsed_time)))

        return elapsed_str, pct_str, rate_str, eta_str, self.more_state

    def get_summary_dict(self):
        """Generate summary dictionary for structured logging

        Returns:
            dict: Summary with step details including command sequence and return codes
        """
        summary = {
            "step": f"firmware {self.wipe_name} {self.device_path}",
            "elapsed": Utils.ago_str(self.elapsed_secs),
            "rate": self.wipe_name,  # Use actual wipe mode (Enhanced, Crypto, etc.)
            "bytes_written": self.total_written,
            "bytes_total": self.total_size,
            "result": "completed" if self.total_written == self.total_size else "partial"
        }

        # Include actual command (always show for debugging)
        if self.actual_command:
            summary["command"] = ' '.join(self.actual_command) if isinstance(self.actual_command, list) else str(self.actual_command)
        else:
            summary["command"] = "N/A"

        # Include return code if process completed
        if self.return_code is not None:
            summary["return_code"] = self.return_code

        # Include stderr output if the command failed
        if self.stderr_output:
            summary["stderr"] = self.stderr_output.strip()

        return summary


class NvmeWipeTask(FirmwareWipeTask):
    """NVMe firmware wipe using NvmeTool

    Supports various sanitize and format operations:
    - Sanitize: Crypto Erase, Block Erase, Overwrite
    - Format: Crypto Erase, User Data Erase

    Example command_args:
    - 'sanitize_crypto' (Sanitize with Crypto Erase)
    - 'sanitize_block' (Sanitize with Block Erase)
    - 'format_erase' (Format with Crypto Erase)
    """
    def __init__(self, device_path, total_size, opts, command_args, wipe_name):
        self.tool = NvmeTool(device_path)
        self.wipe_method = command_args  # e.g., 'sanitize_crypto', 'sanitize_block', 'format_erase'
        self.state_mono = 0
        super().__init__(device_path, total_size, opts, command_args, wipe_name)

    def _estimate_duration(self):
        """Estimate NVMe wipe duration from tool capabilities

        Most NVMe sanitize/format operations complete in seconds.
        Crypto erase: 2-10 seconds
        Block erase: 10-30 seconds
        Overwrite: 30-120 seconds
        """
        if self.tool.job.est_secs:
            return self.tool.job.est_secs

        # Fallback estimates based on method
        if 'crypto' in self.wipe_method:
            return 10
        if 'block' in self.wipe_method:
            return 30
        if 'overwrite' in self.wipe_method:
            return 120
        return 30

    def _build_command(self):
        """Build NVMe wipe command using NvmeTool

        Returns:
            None (NvmeTool handles command internally)
        """
        # Verify capabilities for the specific wipe method
        verdict = self.tool.get_wipe_verdict(method=self.wipe_method)
        if verdict != "OK":
            raise Exception(f"NVMe pre-flight failed: {verdict}")

        # Start wipe using NvmeTool
        self.tool.start_wipe(method=self.wipe_method)
        self.process = self.tool.job.process
        self.more_state = 'nvmeIP'

        # Capture the command for logging in summary
        if self.tool.last_command:
            self.actual_command = self.tool.last_command

        # Return empty list since process is already started
        return []

    def _check_completion(self):
        """Check NVMe wipe completion with real-time progress polling"""
        if self.more_state == 'nvmeIP':
            if not self.process:
                self.more_state = 'no-process'
                return False

            # For sanitize operations, poll sanitize-log for actual progress
            if 'sanitize' in self.wipe_method:
                status, percent = self.tool.get_sanitize_status()
                if status is not None:
                    # Update progress based on actual sanitize status
                    self.total_written = int(self.total_size * (percent / 100.0))

                    # sstat values: 0=Idle (done or not started), 1=In Progress, 2=Success, 3=Failed
                    if status == 0 or status == 2:
                        # Check if process exited successfully
                        if self.process.poll() is not None:
                            if self.process.returncode == 0:
                                self.more_state = 'Complete'
                                return True
                            else:
                                self.more_state = f'rv={self.process.returncode}'
                                return False
                    elif status == 3:
                        self.more_state = 'sanitize-failed'
                        return False
                    # status == 1: still in progress
                    return None

            # For format operations, just check if process completed
            if self.process.poll() is not None:
                if self.process.returncode == 0:
                    self.more_state = 'Complete'
                    return True
                else:
                    self.more_state = f'rv={self.process.returncode}'
                    return False
            return None

        if self.more_state == 'Complete':
            return True

        return False


class SataWipeTask(FirmwareWipeTask):
    """SATA/ATA firmware wipe using hdparm

    Uses ATA Security Erase command:
    - Normal Erase: Writes zeros to all sectors (slow)
    - Enhanced Erase: Cryptographic erase or vendor-specific (fast)

    Example command_args:
    - '--user-master u --security-erase NULL'
    - '--user-master u --security-erase-enhanced NULL'

    Note: Requires setting a temporary password before erase.
    """
    def __init__(self, device_path, total_size, opts, command_args, wipe_name):
        self.tool = SataTool(device_path)
        self.use_enhanced = 'enhanced' in command_args
        self.sanitize_method = None  # e.g., 'sanitize_crypto', 'sanitize_block', 'sanitize_overwrite'
        if command_args.startswith('sanitize_'):
            self.sanitize_method = command_args
        self.state_mono = 0
        self.commands_executed = []  # Store pre-erase commands for logging
        super().__init__(device_path, total_size, opts, command_args, wipe_name)

    def _estimate_duration(self):
        """Estimate SATA erase/sanitize duration from drive's reported time"""
        # For sanitize operations, use default estimate
        if self.sanitize_method:
            return 120  # Default 2 minutes for SATA Sanitize

        # For ATA Security Erase, use drive's reported time
        self.tool.refresh_secures()
        if self.tool.secures and self.tool.secures.erase_est_secs:
            idx = -1 if self.use_enhanced else 0
            return self.tool.secures.erase_est_secs[idx]
        return 4 * 60 * 60  # Default 4 hours for SATA

    def _build_command(self):
        """Build hdparm erase command (sets password first as required for SATA)

        For ATA Security Erase: Uses SataTool.start_wipe() to execute pre-erase commands
        For SATA Sanitize: Directly executes the sanitize command without password setup

        Returns:
            list: ['hdparm', '--user-master', 'u', '--security-erase', 'NULL', '/dev/sda']
        """
        # Handle SATA Sanitize methods
        if self.sanitize_method:
            # Sanitize doesn't require password setup
            process = self.tool.start_sanitize_wipe(method=self.sanitize_method)
            self.process = process  # Store process reference for monitoring
            self.actual_command = self.tool.last_command
            self.more_state = 'hdparmIP'
            # Return empty list since process is already started
            return []

        # Handle ATA Security Erase methods
        # Call start_wipe which handles password setting and pre-erase setup
        result = self.tool.start_wipe(use_enhanced=self.use_enhanced, password='NULL', db=False)

        # Handle error case (returns tuple)
        if isinstance(result, tuple):
            _, message = result
            raise Exception(f"SATA wipe setup failed: {message}")

        # Handle success case (returns dict with command sequence)
        if isinstance(result, dict):
            # Capture pre-erase commands for logging
            self.commands_executed = result.get('commands_executed', [])
            erase_cmd = result.get('erase_command', [])

            # Check if we got valid erase command
            if not erase_cmd:
                raise Exception("start_wipe() did not return erase command")

            self.more_state = 'hdparmIP'
            return erase_cmd

        # Fallback for unexpected return type
        raise Exception(f"Unexpected return from start_wipe(): {type(result)}")

    def get_summary_dict(self):
        """Generate summary dict with SATA pre-erase command sequence included"""
        summary = super().get_summary_dict()

        # Add pre-erase commands executed (for convincing evidence)
        if self.commands_executed:
            summary["commands_executed"] = self.commands_executed

        return summary

    def _check_completion(self):
        """Check SATA erase completion and verify result"""
        if self.more_state == 'hdparmIP':
            if not self.process:
                self.more_state = 'no-self.process'
                return False
            if self.process.poll() is None:
                return None # still running, same state
            if self.process.returncode != 0:
                self.more_state = f'rv={self.process.returncode}'
                return False
            self.more_state = 'NotReady' # move on
        if self.more_state == 'NotReady':
            rv, why = self.tool.verify_wipe_result()
            if not rv:
                self.more_state = why
                return None
            self.more_state = 'Pause'
            self.state_mono = time.monotonic()
        if self.more_state == 'Pause':
            if time.monotonic() - self.state_mono < 5.0:
                return None # keep waiting
            self.more_state = 'Complete'
            return True

        return False # unexpected state


class StandardPrecheckTask(WipeTask):
    """Precheck firmware wipe capabilities before proceeding

    Validates that the selected wipe method is available on the device.
    Applies to both NVMe and SATA firmware wipes.
    """

    def __init__(self, device_path, total_size, opts, selected_wipe_type, command_method=None):
        super().__init__(device_path, total_size, opts)
        self.selected_wipe_type = selected_wipe_type
        self.command_method = command_method  # The actual method name for get_wipe_verdict()
        self.capabilities_found = {}
        self.method_available = False
        self.elapsed_secs = 0

    def get_display_name(self):
        """Get display name for precheck task"""
        return "Precheck"

    def run_task(self):
        """Validate that selected wipe method is available on device"""
        try:
            # Determine device type and check capabilities
            if self.device_path.startswith('/dev/nvme'):
                # NVMe device
                tool = NvmeTool(self.device_path)
                # Check if selected method is available (use command_method if provided, else selected_wipe_type)
                method_to_check = self.command_method if self.command_method else self.selected_wipe_type
                verdict = tool.get_wipe_verdict(method=method_to_check)
                if verdict == "OK":
                    self.capabilities_found[self.selected_wipe_type] = "available"
                    self.method_available = True
                else:
                    self.capabilities_found[self.selected_wipe_type] = verdict
                    self.method_available = False
            else:
                # SATA device
                tool = SataTool(self.device_path)
                tool.refresh_secures()
                if tool.secures and tool.secures.supported:
                    self.capabilities_found[self.selected_wipe_type] = "available"
                    self.method_available = True
                else:
                    self.capabilities_found[self.selected_wipe_type] = "not_available"
                    self.method_available = False

            if not self.method_available:
                self.exception = f"Selected wipe method '{self.selected_wipe_type}' not available"

        except Exception:
            if not self.exception:
                self.exception = traceback.format_exc()
        finally:
            # Capture elapsed time when task completes
            self.elapsed_secs = int(time.monotonic() - self.start_mono)
            self.done = True

    def get_summary_dict(self):
        """Generate summary dictionary for structured logging"""
        summary = {
            "step": f"precheck firmware capabilities {self.device_path}",
            "elapsed": Utils.ago_str(self.elapsed_secs),
            "rate": "Precheck",
            "capabilities_found": self.capabilities_found,
            "selected_method": self.selected_wipe_type,
            "result": "passed" if self.method_available else "failed"
        }

        # Include error message if precheck failed
        if self.exception:
            summary["error"] = self.exception

        return summary


class FirmwarePreVerifyTask(WipeTask):
    """Write test blocks before firmware wipe for later verification

    First performs a 256KB zero wipe from offset 0 using direct I/O to ensure
    partition tables and firmware headers are cleared before test blocks are written.

    Then writes 3 x 4KB blocks of pseudo-random data at:
    - Front: 16KB after marker area
    - Middle: total_size // 2
    - End: total_size - 4096

    Stores blocks in self.original_blocks for post-verify to compare against.
    """

    BLOCK_SIZE = 4096
    NUM_BLOCKS = 3
    ZERO_WIPE_SIZE = 256 * 1024  # 256KB to clear partition tables and firmware headers

    def __init__(self, device_path, total_size, opts):
        super().__init__(device_path, total_size, opts)
        self.blocks_written = 0
        self.original_blocks = []  # Stores 3 x 4KB test blocks
        self.test_locations = []   # Stores 3 offsets
        self.elapsed_secs = 0      # Capture elapsed time when task completes
        self.zero_wipe_bytes = 0   # Track bytes written during zero wipe

    def get_display_name(self):
        """Get display name for pre-verify task"""
        return "Pre-verify"

    def run_task(self):
        """First zero wipe 256KB from offset 0, then write 3 test blocks"""
        try:
            # Phase 1: Zero wipe 256KB from offset 0 using direct I/O
            # This clears partition tables and firmware headers
            self._zero_wipe_partition_area()

            # Phase 2: Calculate locations for test blocks (skip marker area at front)
            self.test_locations = [
                WipeTask.MARKER_SIZE + 16384,    # Front: 16KB after marker
                self.total_size // 2,             # Middle
                self.total_size - self.BLOCK_SIZE # End
            ]

            # Generate pseudo-random test data
            random.seed(int(time.time()))
            for i in range(self.NUM_BLOCKS):
                block = bytes([random.randint(0, 255) for _ in range(self.BLOCK_SIZE)])
                self.original_blocks.append(block)

            # Phase 3: Write blocks to device
            with device_io.open_buffered(self.device_path, 'r+b') as device:
                for i, offset in enumerate(self.test_locations):
                    if self.do_abort:
                        break

                    device.seek(offset)
                    device.write(self.original_blocks[i])
                    device.flush()
                    self.blocks_written += 1
                    # Total written includes both zero wipe and test blocks
                    self.total_written = self.zero_wipe_bytes + (self.blocks_written * self.BLOCK_SIZE)

                device_io.sync(device)

        except Exception:
            self.exception = traceback.format_exc()
        finally:
            # Capture elapsed time when task completes (not when logged later)
            self.elapsed_secs = int(time.monotonic() - self.start_mono)
            self.done = True

    def _zero_wipe_partition_area(self):
        """Zero wipe 256KB from offset 0 using direct I/O to clear partition tables

        Uses the same O_DIRECT mechanism as logical wipes to ensure partition
        tables and firmware headers are cleared before test blocks are written.
        """
        try:
            # Open device with O_DIRECT for unbuffered I/O
            fd = device_io.open_direct(self.device_path, write=True)

            try:
                # Seek to start of device
                device_io.lseek(fd, 0, os.SEEK_SET)

                bytes_to_write = self.ZERO_WIPE_SIZE
                bytes_written_total = 0

                while bytes_written_total < bytes_to_write and not self.do_abort:
                    # Calculate chunk size (must be block-aligned for O_DIRECT)
                    remaining = bytes_to_write - bytes_written_total
                    chunk_size = min(WipeTask.WRITE_SIZE, remaining)
                    # Round down to block boundary
                    chunk_size = (chunk_size // WipeTask.BLOCK_SIZE) * WipeTask.BLOCK_SIZE
                    if chunk_size == 0:
                        break

                    # Get zero buffer from WipeTask
                    chunk = WipeTask.zero_buffer[:chunk_size]

                    try:
                        # Write with O_DIRECT (bypasses page cache)
                        bytes_written = device_io.write(fd, chunk)
                    except Exception as e:
                        self.exception = f"Zero wipe failed: {str(e)}"
                        self.do_abort = True
                        bytes_written = 0

                    bytes_written_total += bytes_written
                    self.zero_wipe_bytes = bytes_written_total
                    self.total_written = bytes_written_total

                    # Check for incomplete writes
                    if bytes_written < chunk_size:
                        break

            finally:
                # Close device file descriptor
                if fd is not None:
                    device_io.close(fd)

        except Exception as e:
            self.exception = f"Zero wipe partition area failed: {str(e)}"
            self.do_abort = True

    def get_summary_dict(self):
        """Generate summary dictionary for structured logging"""
        return {
            "step": "pre-verify test blocks",
            "elapsed": Utils.ago_str(self.elapsed_secs),
            "rate": "Test",
            "blocks_written": self.blocks_written,
            "result": "completed" if self.blocks_written == self.NUM_BLOCKS else "partial"
        }


class FirmwarePostVerifyTask(WipeTask):
    """Verify firmware wipe effectiveness by checking if test blocks changed

    Reads the 3 test blocks written by FirmwarePreVerifyTask and verifies
    that at least 95% of bytes have changed (indicating effective wipe).

    Reads block data from the pre-verify task via self.job.tasks.
    """

    THRESHOLD_PCT = 95.0  # Require 95%+ bytes different

    def __init__(self, device_path, total_size, opts):
        super().__init__(device_path, total_size, opts)
        self.diff_percentages = []
        self.verification_passed = False
        self.elapsed_secs = 0      # Capture elapsed time when task completes

    def get_display_name(self):
        """Get display name for post-verify task"""
        return "Post-verify"

    def _update_marker_verify_status(self, verify_status):
        """Update the wipe marker with verification status (pass/fail)

        Reads the existing marker written by FirmwareWipeTask, updates it with
        the verification result, and writes it back. This provides the checkmark
        display in the UI when verification passes.
        """
        try:
            device_name = self.device_path.replace('/dev/', '')

            # Read existing marker
            from .WipeJob import WipeJob
            marker = WipeJob.read_marker_buffer(device_name)
            if not marker:
                return  # No marker to update

            # Prepare updated marker data (preserve existing fields)
            buffer = markers.build_marker_from_existing(marker, verify_status=verify_status)

            # Write updated marker to beginning of device
            markers.write_marker(self.device_path, buffer, device_io, mode='wb')

        except Exception:
            # Don't fail the job if marker update fails, but record it.
            Utils.get_logger().debug(
                f"marker verify-status update failed for {self.device_path}: "
                f"{traceback.format_exc()}")

    def run_task(self):
        """Read test blocks and verify they differ from originals by 95%+"""
        try:
            # Brief delay to allow NVMe drives to finalize wipe operation
            # Some firmware implementations need a moment before reads are reliable
            time.sleep(0.5)

            # Find pre-verify task in job's task list
            pre_verify_task = None
            for task in self.job.tasks:
                if isinstance(task, FirmwarePreVerifyTask):
                    pre_verify_task = task
                    break

            if not pre_verify_task or not pre_verify_task.original_blocks:
                raise Exception("No pre-verify test blocks found")

            original_blocks = pre_verify_task.original_blocks
            test_locations = pre_verify_task.test_locations
            block_size = FirmwarePreVerifyTask.BLOCK_SIZE

            # Read current blocks from device
            with device_io.open_buffered(self.device_path, 'rb') as device:
                for i, offset in enumerate(test_locations):
                    if self.do_abort:
                        break

                    device.seek(offset)
                    current_block = device.read(block_size)

                    if len(current_block) != block_size:
                        raise Exception(f"Failed to read block {i} at offset {offset}")

                    # Calculate bytes that differ
                    diff_bytes = sum(1 for j in range(block_size)
                                    if current_block[j] != original_blocks[i][j])

                    diff_pct = (diff_bytes / block_size) * 100.0
                    self.diff_percentages.append(round(diff_pct, 1))
                    self.total_written = (i + 1) * block_size

            # Check if all blocks meet threshold
            self.verification_passed = all(pct >= self.THRESHOLD_PCT
                                          for pct in self.diff_percentages)

            if not self.verification_passed:
                min_pct = min(self.diff_percentages)
                self.exception = (f"Firmware wipe verification FAILED: "
                                f"Test block changed only {min_pct}% "
                                f"(threshold: {self.THRESHOLD_PCT}%)")

            # Update marker with verification status
            verify_status = "pass" if self.verification_passed else "fail"
            self._update_marker_verify_status(verify_status)

        except Exception as e:
            if not self.exception:
                self.exception = str(e) if str(e) else traceback.format_exc()
        finally:
            # Capture elapsed time when task completes (not when logged later)
            self.elapsed_secs = int(time.monotonic() - self.start_mono)
            self.done = True

    def get_summary_dict(self):
        """Generate summary dictionary for structured logging"""
        summary = {
            "step": "post-verify test blocks",
            "elapsed": Utils.ago_str(self.elapsed_secs),
            "rate": "Test",
            "diff_pct": self.diff_percentages,
            "result": "pass" if self.verification_passed else "fail"
        }

        # Include error message if verification failed
        if self.exception:
            summary["error"] = self.exception.strip()

        return summary

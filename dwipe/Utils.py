"""
Utils class - Utility functions for dwipe
"""
import datetime
import os
import pwd
import sys
from pathlib import Path

from .StructuredLogger import StructuredLogger


class Utils:
    """Utility functions encapsulated as a family"""

    # Singleton logger instance
    _logger = None

    @staticmethod
    def get_logger():
        """Get or create the singleton StructuredLogger instance"""
        if Utils._logger is None:
            Utils._logger = StructuredLogger(
                app_name='dwipe',
                log_dir=Utils.get_config_dir()
            )
        return Utils._logger

    @staticmethod
    def human(number):
        """Return a concise number description."""
        suffixes = ['K', 'M', 'G', 'T']
        number = float(number)
        while suffixes:
            suffix = suffixes.pop(0)
            number /= 1000  # decimal
            if number < 999.95 or not suffixes:
                return f'{number:.1f}{suffix}B'  # decimal
        return None

    @staticmethod
    def ago_str(delta_secs, signed=False):
        """Turn time differences in seconds to a compact representation;
        e.g., '18h·39m' or '450ms' for sub-second times
        """
        abs_delta = delta_secs if delta_secs >= 0 else -delta_secs

        # For sub-second times, show milliseconds
        if 0.00051 <= abs_delta < 0.99949:
            ms = int(abs_delta * 1000)
            rv = '-' if signed and delta_secs < 0 else ''
            return rv + f'{ms}ms'

        ago = int(max(0, round(abs_delta)))
        divs = (60, 60, 24, 7, 52, 9999999)
        units = ('s', 'm', 'h', 'd', 'w', 'y')
        vals = (ago % 60, int(ago / 60))  # seed with secs, mins (step til 2nd fits)
        uidx = 1  # best units
        for div in divs[1:]:
            if vals[1] < div:
                break
            vals = (vals[1] % div, int(vals[1] / div))
            uidx += 1
        rv = '-' if signed and delta_secs < 0 else ''
        rv += f'{vals[1]}{units[uidx]}' if vals[1] else ''
        rv += f'{vals[0]:d}{units[uidx - 1]}'
        return rv

    @staticmethod
    def rerun_module_as_root(module_name):
        """Rerun using the module name"""
        if os.geteuid() != 0:  # Re-run the script with sudo
            os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            args = sys.argv[1:]
            # sudo resets the environment by default, so an exported NO_COLOR
            # would not reach the process that actually draws the UI.  Carry it
            # across as the equivalent switch; --no-color already wins over
            # --theme, matching the no-color.org convention.
            if os.environ.get('NO_COLOR') and '--no-color' not in args:
                args = ['--no-color', *args]
            vp = ['sudo', sys.executable, '-m', module_name] + args
            os.execvp('sudo', vp)

    @staticmethod
    def get_config_dir():
        """Get the dwipe config directory, handling sudo correctly

        Returns the real user's ~/.config/dwipe directory, even when running with sudo
        """
        real_user = os.environ.get('SUDO_USER')
        if real_user:
            # Running with sudo - get the real user's home directory
            real_home = pwd.getpwnam(real_user).pw_dir
            config_dir = Path(real_home) / '.config' / 'dwipe'
        else:
            # Not running with sudo - use normal home
            config_dir = Path.home() / '.config' / 'dwipe'

        config_dir.mkdir(parents=True, exist_ok=True)

        # Fix ownership if running with sudo
        if real_user:
            try:
                pw_record = pwd.getpwnam(real_user)
                uid, gid = pw_record.pw_uid, pw_record.pw_gid
                os.chown(config_dir, uid, gid)
                # Also fix parent .config directory if we created it
                parent = config_dir.parent
                if parent.exists():
                    os.chown(parent, uid, gid)
            except (OSError, KeyError):
                pass  # Ignore permission errors and missing users

        return config_dir

    @staticmethod
    def fix_file_ownership(file_path):
        """Fix file ownership to the real user when running with sudo"""
        real_user = os.environ.get('SUDO_USER')
        if real_user:
            try:
                pw_record = pwd.getpwnam(real_user)
                uid, gid = pw_record.pw_uid, pw_record.pw_gid
                os.chown(file_path, uid, gid)
            except (OSError, KeyError):
                pass  # Ignore permission errors and missing users

    @staticmethod
    def get_log_path():
        """Get the path to the log file, creating directory if needed"""
        log_dir = Utils.get_config_dir()
        return log_dir / 'log.txt'

    @staticmethod
    def trim_log_if_needed(log_path, max_lines=1000):
        """Trim log file by removing oldest 1/3 if it exceeds max_lines"""
        try:
            if not log_path.exists():
                return

            with open(log_path, encoding='utf-8') as f:
                lines = f.readlines()

            if len(lines) > max_lines:
                # Keep the newest 2/3 of the log
                keep_count = len(lines) * 2 // 3
                with open(log_path, 'w', encoding='utf-8') as f:
                    f.writelines(lines[-keep_count:])
        except Exception:
            pass  # Don't fail if log trimming fails

    @staticmethod
    def get_device_dict(partitions, partition):
        """Extract device information from partition namespace as dict

        Args:
            partition: Partition namespace object with device attributes

        Returns:
            dict: Device information for structured logging
        """
        if partition.name in partitions:
            disk = partitions[partition.name]
            while disk.parent:
                disk = partitions[disk.parent]
        else:
            disk = partition
        device_dict = {
            "name": partition.name,
            "path": f"/dev/{partition.name}",
            "size": Utils.human(partition.size_bytes),
        }
        device_dict["uuid"] = partition.uuid
        if partition.type:
            device_dict["type"] = partition.type
        if partition.fstype:
            device_dict["fstype"] = partition.fstype
        if partition.label:
            device_dict["label"] = partition.label

        device_dict["model"] = disk.model
        device_dict["serial"] = disk.serial
        device_dict["port"] = disk.port

        return device_dict

    @staticmethod
    def log_wipe_structured(partitions, partition, job, mode=None):
        """Log a wipe or verify operation using structured logging

        Args:
            partition: Partition namespace object with device info
            job: WipeJob object with job statistics
            mode: Optional mode override (defaults to job.opts.wipe_mode)
        """
        logger = Utils.get_logger()

        # Determine log level based on result
        is_verify_only = getattr(job, 'is_verify_only', False)
        is_stopped = job.do_abort

        if is_verify_only:
            level = "VERIFY_STOPPED" if is_stopped else "VERIFY_COMPLETE"
        else:
            level = "WIPE_STOPPED" if is_stopped else "WIPE_COMPLETE"

        # Get the three sections
        plan = job.get_plan_dict(mode)
        device = Utils.get_device_dict(partitions, partition)
        summary = job.get_summary_dict()

        # Create summary message
        result_str = summary['result']
        size_str = device['size']
        time_str = summary['total_elapsed']
        # Get rate from wipe step (skip precheck/verify steps for firmware wipes)
        rate_str = 'N/A'
        for step in summary['steps']:
            step_name = step.get('step', '').lower()
            # Look for actual wipe steps: firmware wipes start with "firmware",
            # software wipes have "writing" in the step name
            # Skip precheck, pre-verify, post-verify steps
            if step_name.startswith('firmware') or 'writing' in step_name:
                rate_str = step.get('rate', 'N/A')
                break
        # Fallback to first step if no wipe step found
        if rate_str == 'N/A' and summary['steps']:
            rate_str = summary['steps'][0].get('rate', 'N/A')

        # Build base message
        operation = plan['operation'].capitalize()
        message = f"{operation} {result_str}: {device['name']} {size_str}"

        # Add percentage if stopped
        if result_str == 'stopped' and summary.get('pct_complete', 0) > 0:
            message += f" ({summary['pct_complete']:.0f}%)"

        # Add timing and rate
        message += f" in {time_str} @ {rate_str}"

        # Add error reason if present
        abort_reason = summary.get('abort_reason')
        if abort_reason:
            message += f" [Error: {abort_reason}]"

        # Log the structured event
        logger.put(
            level,
            message,
            data={
                "plan": plan,
                "device": device,
                "summary": summary
            }
        )

    @staticmethod
    def log_wipe(device_name, size_bytes, mode, result, elapsed_time=None, uuid=None, label=None, fstype=None, pct=None, verify_result=None):
        """Log a wipe or verify operation to ~/.config/dwipe/log.txt

        Args:
            device_name: Device name (e.g., 'sdb1')
            size_bytes: Size of device in bytes
            mode: 'Rand', 'Zero', or 'Vrfy' (for verify operations)
            result: 'completed' or 'stopped'
            elapsed_time: Optional elapsed time in seconds
            uuid: Optional UUID of the partition
            label: Optional label of the partition (only for non-wiped)
            fstype: Optional filesystem type (only for non-wiped)
            pct: Optional percentage (for stopped wipes)
            verify_result: Optional verify result (chi-squared value for Rand verifies)
        """
        log_path = Utils.get_log_path()

        # Trim log if needed before appending
        Utils.trim_log_if_needed(log_path)

        timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
        size_str = Utils.human(size_bytes)
        time_str = f' in {Utils.ago_str(int(elapsed_time))}' if elapsed_time else ''

        # Show percentage if available, otherwise 100% for completed or result status
        if result == 'completed':
            status_str = '100%'
        elif result in ('OK', 'FAIL', 'skip'):
            # For verify operations: show 100% and the result
            status_str = '100%'
        elif pct is not None:
            status_str = f'{pct:3d}%'
        else:
            status_str = f'{result:>4s}'

        # Build UUID field (last 8 chars or full if shorter)
        uuid_str = uuid[-8:] if uuid and len(uuid) >= 8 else (uuid if uuid else '-')

        # For wiped/verified disks, don't show label/fstype (they shouldn't have any)
        # Only show for non-completed operations
        if (result in ('completed', 'OK', 'FAIL', 'skip')) and mode in ('Rand', 'Zero', 'Vrfy'):
            info_str = ''
        else:
            # Show fstype and label for non-wiped disks
            fstype_str = fstype if fstype and fstype.strip() else '-'
            label_str = f"'{label}'" if label and label.strip() else "'-'"
            info_str = f' | {fstype_str} {label_str}'

        # Add result status for verify operations (OK/FAIL)
        result_str = ''
        if result in ('OK', 'FAIL', 'skip'):
            result_str = f' | {result}'

        # Add verify result details if available (failure reason or stats)
        stats_str = ''
        if verify_result:
            stats_str = f' | {verify_result}'

        log_line = f'{timestamp} | {mode:4s} | {status_str} {size_str:>8s}{time_str:>12s} | {device_name:8s} | {uuid_str:8s}{info_str}{result_str}{stats_str}\n'

        try:
            with open(log_path, 'a', encoding='utf-8') as f:
                f.write(log_line)
            # Fix ownership if running with sudo
            Utils.fix_file_ownership(log_path)
        except Exception:
            pass  # Don't fail if logging fails

    @staticmethod
    def parse_nvme_sanitize_capabilities(id_ctrl_data: dict) -> dict:
        """Parse NVMe sanitize capabilities from id-ctrl JSON data.

        Extracts the sanicap field and interprets the capability bits according
        to the NVMe specification:
        - Bit 0: Crypto Erase
        - Bit 1: Block Erase
        - Bit 2: Overwrite

        Args:
            id_ctrl_data: Dictionary from nvme id-ctrl command (with -o json)

        Returns:
            dict: Maps capability names to method names, e.g.,
                  {'Crypto': 'sanitize_crypto', 'Block': 'sanitize_block'}
        """
        modes = {}
        sanicap = id_ctrl_data.get('sanicap', 0)

        if sanicap > 0:
            if sanicap & 0x01:
                modes['Crypto'] = 'sanitize_crypto'
            if sanicap & 0x02:
                modes['Block'] = 'sanitize_block'
            if sanicap & 0x04:
                modes['Ovwr'] = 'sanitize_overwrite'

        return modes

    @staticmethod
    def parse_nvme_sanitize_flags(id_ctrl_data: dict) -> tuple:
        """Parse NVMe sanitize capabilities into individual boolean flags.

        Interprets sanicap bits according to NVMe specification:
        - Bit 0: Crypto Erase
        - Bit 1: Block Erase
        - Bit 2: Overwrite

        Args:
            id_ctrl_data: Dictionary from nvme id-ctrl command (with -o json)

        Returns:
            tuple: (has_sanitize, crypto_erase_supported, block_erase_supported,
                    overwrite_supported)
        """
        sanicap = id_ctrl_data.get('sanicap', 0)
        has_sanitize = sanicap > 0
        crypto_erase_supported = bool(sanicap & 0x01)
        block_erase_supported = bool(sanicap & 0x02)
        overwrite_supported = bool(sanicap & 0x04)

        return has_sanitize, crypto_erase_supported, block_erase_supported, overwrite_supported


class ClipboardHelper:
    """Helper for copying text to clipboard across different Linux environments.

    Supports:
    - OSC 52 escape sequence (works over SSH if terminal supports it)
    - Wayland (wl-copy)
    - X11 (xclip, xsel)
    - Terminal fallback (print to screen when clipboard unavailable)

    Detection priority:
    1. If SSH session detected -> use OSC 52 (terminal clipboard escape sequence)
    2. If WAYLAND_DISPLAY set -> try wl-copy
    3. If DISPLAY set -> try xclip, then xsel
    4. Fallback -> terminal mode (manual copy)
    """
    import shutil
    import subprocess

    # Singleton instance for caching probe results
    _instance = None
    _probed = False
    _clipboard_cmd = None  # e.g., ['wl-copy'] or ['xclip', '-selection', 'clipboard']
    _use_osc52 = False     # Use OSC 52 terminal escape sequence
    _is_ssh = False
    _method_name = None  # Human-readable method name

    @classmethod
    def _probe(cls):
        """Probe available clipboard methods (called once, results cached)."""
        if cls._probed:
            return

        cls._probed = True
        import shutil

        # Check for SSH session - use OSC 52 escape sequence
        cls._is_ssh = bool(os.environ.get('SSH_CLIENT') or os.environ.get('SSH_TTY')
                          or os.environ.get('SSH_CONNECTION'))

        if cls._is_ssh:
            # OSC 52 works through terminal emulators that support it
            # (iTerm2, kitty, alacritty, Windows Terminal, tmux with set-clipboard)
            cls._use_osc52 = True
            cls._method_name = 'OSC 52 (terminal escape sequence)'
            return

        # Check Wayland first
        if os.environ.get('WAYLAND_DISPLAY'):
            if shutil.which('wl-copy'):
                cls._clipboard_cmd = ['wl-copy']
                cls._method_name = 'wl-copy (Wayland)'
                return

        # Check X11
        if os.environ.get('DISPLAY'):
            if shutil.which('xclip'):
                cls._clipboard_cmd = ['xclip', '-selection', 'clipboard']
                cls._method_name = 'xclip (X11)'
                return
            if shutil.which('xsel'):
                cls._clipboard_cmd = ['xsel', '--clipboard', '--input']
                cls._method_name = 'xsel (X11)'
                return

        cls._method_name = 'terminal (no clipboard tool found)'

    @classmethod
    def get_method_name(cls):
        """Return human-readable description of clipboard method."""
        cls._probe()
        return cls._method_name

    @classmethod
    def has_clipboard(cls):
        """Return True if a clipboard method is available (tool or OSC 52)."""
        cls._probe()
        return cls._clipboard_cmd is not None or cls._use_osc52

    @classmethod
    def copy(cls, text):
        """Copy text to clipboard.

        Args:
            text: String to copy

        Returns:
            tuple: (success: bool, error_message: str or None)
        """
        cls._probe()
        import base64
        import subprocess

        # Try OSC 52 escape sequence (works over SSH with supporting terminals)
        if cls._use_osc52:
            try:
                encoded = base64.b64encode(text.encode('utf-8')).decode('ascii')
                # OSC 52: \033]52;c;<base64>\a
                # 'c' = clipboard selection
                osc52 = f'\033]52;c;{encoded}\a'
                sys.stdout.write(osc52)
                sys.stdout.flush()
                return True, None
            except Exception as e:
                return False, f"OSC 52 failed: {e}"

        if not cls._clipboard_cmd:
            return False, "No clipboard available"

        try:
            proc = subprocess.run(
                cls._clipboard_cmd,
                input=text.encode('utf-8'),
                capture_output=True,
                timeout=5
            )
            if proc.returncode == 0:
                return True, None
            return False, proc.stderr.decode('utf-8', errors='replace').strip()
        except subprocess.TimeoutExpired:
            return False, "Clipboard command timed out"
        except Exception as e:
            return False, str(e)

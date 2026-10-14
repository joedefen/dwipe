"""
DiskWipe class - Main application controller/singleton
"""
# pylint: disable=invalid-name,broad-exception-caught,line-too-long
# pylint: disable=too-many-nested-blocks,too-many-instance-attributes
# pylint: disable=too-many-branches,too-many-statements,too-many-locals
# pylint: disable=protected-access,too-many-return-statements
# pylint: disable=too-few-public-methods
import os
import sys
import subprocess
import re
import time
import threading
# import shutil
import json
import curses as cs
from types import SimpleNamespace
from datetime import datetime
from console_window import (ConsoleWindow, ConsoleWindowOpts, OptionSpinner,
            IncrementalSearchBar, InlineConfirmation, Theme,
            Screen, ScreenStack, Context)

from .WipeJob import WipeJob
from .DeviceInfo import DeviceInfo
from .DeviceWorker import ProbeState
from .Utils import Utils
from .PersistentState import PersistentState
from .StructuredLogger import StructuredLogger
from .DeviceChangeMonitor import DeviceChangeMonitor


def _restore_terminal():
    """Clear the screen and restore sane tty settings without a shell.

    Replaces ``os.system('clear; stty sane')`` so the exit paths do not depend
    on ``/bin/sh`` or on the external ``clear`` binary being present.
    """
    sys.stdout.write('\033[2J\033[H')
    sys.stdout.flush()
    try:
        subprocess.run(['stty', 'sane'], check=False)
    except OSError:
        pass


# Screen constants
MAIN_ST = 0
HELP_ST = 1
LOG_ST = 2
THEME_ST = 3
SCREEN_NAMES = ('MAIN', 'HELP', 'HISTORY', 'THEMES')


class DiskWipe:
    """Main application controller and UI manager"""
    singleton = None

    def __init__(self, opts=None, persistent_state=None):
        DiskWipe.singleton = self
        self.opts = opts if opts else SimpleNamespace(debug=0)
        # Use provided persistent_state or create a new one
        self.persistent_state = persistent_state if persistent_state else PersistentState()
        # Set defaults for command-line options (only if not provided)
        # The CLI exposes '--mode' (argparse dest 'mode'); map it onto the
        # internal 'wipe_mode' so the flag is actually honored downstream.
        if not hasattr(self.opts, 'wipe_mode'):
            self.opts.wipe_mode = getattr(self.opts, 'mode', None) or '+V'
        if not hasattr(self.opts, 'passes'):
            self.opts.passes = 1
        if not hasattr(self.opts, 'verify_pct'):
            self.opts.verify_pct = 1
        if not hasattr(self.opts, 'port_serial'):
            self.opts.port_serial = 'Auto'
        if not hasattr(self.opts, 'slowdown_stop'):
            self.opts.slowdown_stop = 64
        if not hasattr(self.opts, 'stall_timeout'):
            self.opts.stall_timeout = 60
        if not hasattr(self.opts, 'dense'):
            self.opts.dense = False
        self.mounts_lines = None
        self.partitions = {}  # a dict of namespaces keyed by name
        self.wids = None
        self.job_cnt = 0
        self.exit_when_no_jobs = False

        # Per-device throttle tracking (keyed by device_path)
        # Values: {'mbps': int, 'auto': bool} or None
        self.device_throttles = {}

        self.prev_filter = ''  # string
        self.filter = None  # compiled pattern
        self.pick_is_running = False
        self.dev_info = None

        self.win, self.spin = None, None
        self.screens, self.stack = [], None

        # Inline confirmation handler
        self.confirmation = InlineConfirmation()

        # Incremental search bar for filtering
        self.filter_bar = IncrementalSearchBar(
            on_change=self._on_filter_change,
            on_accept=self._on_filter_accept,
            on_cancel=self._on_filter_cancel
        )

    def _start_wipe(self):
        """Start the wipe job after confirmation"""
        if self.confirmation.identity and self.confirmation.identity in self.partitions:
            part = self.partitions[self.confirmation.identity]
            # Clear any previous verify failure message when starting wipe
            if hasattr(part, 'verify_failed_msg'):
                delattr(part, 'verify_failed_msg')

            # Get the wipe type from user's choice
            # ConsoleWindow already canonicalizes case, just strip the '*' recommendation marker
            wipe_type = self.confirmation.input_buffer.strip().rstrip('*')

            # Check if it's a firmware wipe
            if wipe_type not in ('Zero', 'Rand'):
                # Firmware wipe - check if it's available
                if not part.hw_caps or wipe_type not in part.hw_caps:
                    part.mounts = [f'⚠ Firmware wipe {wipe_type} not available']
                    self.confirmation.cancel()
                    self.win.passthrough_mode = False
                    return

                # Get command args for this wipe type
                from .DrivePreChecker import DrivePreChecker
                command_args = DrivePreChecker.get_wipe_command_args(wipe_type)

                # Import firmware task classes
                from .FirmwareWipeTask import (NvmeWipeTask, SataWipeTask,
                                               StandardPrecheckTask,
                                               FirmwarePreVerifyTask, FirmwarePostVerifyTask)

                # Determine task type based on device name
                if part.name.startswith('nvme'):
                    task_class = NvmeWipeTask
                else:
                    task_class = SataWipeTask

                # Create firmware task
                fw_task = task_class(
                    device_path=f'/dev/{part.name}',
                    total_size=part.size_bytes,
                    opts=self.opts,
                    command_args=command_args,
                    wipe_name=wipe_type
                )

                # Store wipe type for logging
                part.wipe_type = wipe_type

                # Build task list with precheck, pre/post firmware verification
                # (Software verify is not supported for firmware wipes)
                precheck = StandardPrecheckTask(
                    device_path=f'/dev/{part.name}',
                    total_size=part.size_bytes,
                    opts=self.opts,
                    selected_wipe_type=wipe_type,
                    command_method=command_args
                )

                pre_verify = FirmwarePreVerifyTask(
                    device_path=f'/dev/{part.name}',
                    total_size=part.size_bytes,
                    opts=self.opts
                )

                post_verify = FirmwarePostVerifyTask(
                    device_path=f'/dev/{part.name}',
                    total_size=part.size_bytes,
                    opts=self.opts
                )

                tasks = [precheck, pre_verify, fw_task, post_verify]

                # Create WipeJob with firmware task (and optional verify task)
                part.job = WipeJob(
                    device_path=f'/dev/{part.name}',
                    total_size=part.size_bytes,
                    opts=self.opts,
                    tasks=tasks
                )
                # Firmware wipes (SATA secure erase, NVMe sanitize) write zeros
                part.job.expected_pattern = "zeroed"
                part.job.thread = threading.Thread(target=part.job.run_tasks)
                part.job.thread.start()

                self.job_cnt += 1
                self.set_state(part, to='0%')

                # Clear confirmation and return early
                self.confirmation.cancel()
                self.win.passthrough_mode = False
                return

            # Construct full wipe mode (e.g., 'Zero+V', 'Rand', etc.)
            if self.opts.wipe_mode == '+V':
                full_wipe_mode = wipe_type + '+V'
            else:
                full_wipe_mode = wipe_type

            # Store wipe type for later logging
            part.wipe_type = wipe_type

            # Temporarily set the full wipe mode
            old_wipe_mode = self.opts.wipe_mode
            self.opts.wipe_mode = full_wipe_mode

            try:
                part.job = WipeJob.start_job(f'/dev/{part.name}',
                                              part.size_bytes, opts=self.opts)
                self.job_cnt += 1
                self.set_state(part, to='0%')
                part.hw_nopes, part.hw_caps = {}, {}
            finally:
                # Restore original wipe_mode
                self.opts.wipe_mode = old_wipe_mode

        # Clear confirmation state
        self.confirmation.cancel()
        self.win.passthrough_mode = False  # Disable passthrough

    def _start_verify(self):
        """Start the verify job after confirmation"""
        if self.confirmation.identity and self.confirmation.identity in self.partitions:
            part = self.partitions[self.confirmation.identity]

            # Safety check: Firmware wipes have built-in verification - prevent manual verify
            if self._is_firmware_wipe_marker(part):
                part.mounts = ['⚠ Firmware wipes have built-in verification - standard verify not allowed']
                self.confirmation.cancel()
                self.win.passthrough_mode = False
                return

            # Clear any previous verify failure message when starting verify
            if hasattr(part, 'verify_failed_msg'):
                delattr(part, 'verify_failed_msg')
            part.job = WipeJob.start_verify_job(f'/dev/{part.name}',
                                                part.size_bytes, opts=self.opts)
            self.job_cnt += 1
        # Clear confirmation state
        self.confirmation.cancel()
        self.win.passthrough_mode = False  # Disable passthrough

    def test_state(self, ns, to=None):
        """Test if OK to set state of partition"""
        return self.dev_info.set_one_state(self.partitions, ns, test_to=to)

    def set_state(self, ns, to=None):
        """Set state of partition"""
        result = self.dev_info.set_one_state(self.partitions, ns, to=to)

        # Save block state changes to persistent state
        if result and to in ('Blk', 'Unbl'):
            self.persistent_state.set_device_locked(ns, to == 'Blk')

        return result

    def _is_firmware_wipe(self, part):
        """Check if partition has an active firmware wipe job (unstoppable)."""
        if not part.job or part.job.done:
            return False
        from .FirmwareWipeTask import FirmwareWipeTask
        current_task = getattr(part.job, 'current_task', None)
        return current_task and isinstance(current_task, FirmwareWipeTask)

    def _is_firmware_wipe_marker(self, part):
        """Check if partition's wipe marker indicates a firmware wipe.

        Firmware wipes have modes like 'Crypto', 'Enhanced', 'Sanitize-Crypto', etc.
        Software wipes have modes like 'Zero' or 'Rand'.
        """
        if part.state not in ('s', 'W'):
            return False

        marker = WipeJob.read_marker_buffer(part.name)
        if not marker:
            return False

        # Check if mode is a firmware wipe (not 'Zero' or 'Rand')
        mode = getattr(marker, 'mode', None)
        return mode and mode not in ('Zero', 'Rand')

    def _has_any_firmware_wipes(self):
        """Check if any firmware wipes are currently running."""
        for part in self.partitions.values():
            if self._is_firmware_wipe(part):
                return True
        return False

    def do_key(self, key):
        """Handle keyboard input"""
        if self.exit_when_no_jobs:
            # Check if all jobs are done and exit
            jobs_running = sum(1 for part in self.partitions.values() if part.job)
            if jobs_running == 0:
                self.win.stop_curses()
                _restore_terminal()
                sys.exit(0)
            return True  # continue running

        if not key:
            return True

        # Handle search bar input
        if self.stack.curr.num == LOG_ST:
            screen_obj = self.stack.get_curr_obj()
            if screen_obj.search_bar.is_active:
                if screen_obj.search_bar.handle_key(key):
                    return None # key handled by search bar

        # Handle filter bar input
        if self.filter_bar.is_active:
            if self.filter_bar.handle_key(key):
                return None  # Key was handled by filter bar

        # Handle confirmation mode input (wipe or verify)
        if self.confirmation.active:
            result = self.confirmation.handle_key(key)
            if result == 'confirmed':
                if self.confirmation.action_type == 'wipe':
                    self._start_wipe()
                elif self.confirmation.action_type == 'verify':
                    self._start_verify()
            elif result == 'cancelled':
                self.confirmation.cancel()
                self.win.passthrough_mode = False
            return None

        if key in (cs.KEY_ENTER, 10):  # Handle ENTER
            # ENTER pops screen (returns from help, etc.)
            if hasattr(self.spin, 'stack') and self.spin.stack.curr.num != MAIN_ST:
                self.spin.stack.pop()
                return None

        if key in self.spin.keys:
            _ = self.spin.do_key(key, self.win)
        return None

    def get_keys_line(self):
        """Generate the header line showing available keys"""
        # Get actions for the currently picked context
        _, pick_actions = self.get_actions(None)

        line = ''
        for key, verb in pick_actions.items():
            if key[0].lower() == verb[0].lower():
                # First letter matches - use [x]verb format
                line += f' [{verb[0]}]{verb[1:]}'
            else:
                # First letter doesn't match - use key:verb format
                line += f' {key}:{verb}'
        line += ' [S]top' if self.job_cnt > 0 else ''
        line = f'{line:<20} '
        line += self.filter_bar.get_display_string(prefix=' /') or ' /'
        line += ' [r]escan [h]ist [t]heme ?:help [q]uit'
        return line[1:]

    def get_actions(self, part):
        """Determine the type of the current line and available commands."""
        name, actions = '', {}
        ctx = self.win.get_picked_context()
        if ctx and hasattr(ctx, 'partition'):
            part = ctx.partition
            name = part.name
            self.pick_is_running = bool(part.job)
            # Show stop action only for non-firmware wipes
            if self.test_state(part, to='STOP') and not self._is_firmware_wipe(part):
                actions['s'] = 'stop'
            elif self.test_state(part, to='0%'):
                actions['w'] = 'wipe'
                # DEL only for whole disks that are SATA or NVMe
                if part.parent is None and part.name[:2] in ('sd', 'hd', 'nv'):
                    actions['DEL'] = 'DEL'
            # Can verify:
            # 1. Anything with wipe markers (states 's' or 'W') - EXCEPT firmware wipes
            # 2. Unmarked whole disks (no parent, state '-' or '^') WITHOUT partitions that have filesystems
            # 3. Unmarked partitions without filesystems (has parent, state '-' or '^', no fstype)
            # 4. Only if verify_pct > 0
            # This prevents verifying filesystems which is nonsensical
            # NOTE: Firmware wipes have built-in pre/post verification, so manual verify is disallowed
            verify_pct = getattr(self.opts, 'verify_pct', 0)
            if not part.job and verify_pct > 0:
                if part.state in ('s', 'W'):
                    # Do NOT allow verify on firmware wipes - they have built-in verification
                    if not self._is_firmware_wipe_marker(part):
                        actions['v'] = 'verify'
                elif part.state in ('-', '^'):
                    # For whole disks (no parent), only allow verify if no partitions have filesystems
                    # For partitions, only allow if no filesystem
                    if not part.parent:
                        # Whole disk - check if any child partitions have filesystems
                        has_typed_partitions = any(
                            p.parent == part.name and p.fstype
                            for p in self.partitions.values()
                        )
                        if not has_typed_partitions:
                            actions['v'] = 'verify'
                    elif not part.fstype:
                        # Partition without filesystem
                        actions['v'] = 'verify'
            if self.test_state(part, to='Blk'):
                actions['b'] = 'block'
            if self.test_state(part, to='Unbl'):
                actions['b'] = 'unblk'
        return name, actions

    def _on_filter_change(self, text):
        """Callback when filter text changes - compile and apply filter in real-time"""
        text = text.strip()
        if not text:
            self.filter = None
            return

        try:
            self.filter = re.compile(text, re.IGNORECASE)
        except Exception:
            # Invalid regex - keep previous filter active
            pass

    def _on_filter_accept(self, text):
        """Callback when filter is accepted (ENTER pressed)"""
        self.prev_filter = text.strip()
        self.win.passthrough_mode = False
        # Move to top when filter is applied
        if text.strip():
            self.win.pick_pos = 0

    def _on_filter_cancel(self, original_text):
        """Callback when filter is cancelled (ESC pressed)"""
        # Restore original filter
        if original_text:
            self.filter = re.compile(original_text, re.IGNORECASE)
            self.prev_filter = original_text
        else:
            self.filter = None
            self.prev_filter = ''
        self.win.passthrough_mode = False

    def get_hw_caps_when_needed(self):
        """ Look for wipeable disks w/o hardware info """
        if not self.dev_info:
            return
        from .DeviceWorker import ProbeState
        for  ns in self.partitions.values():
            if ns.parent:
                continue
#           if ns.port.startswith('USB'):
#               continue
            if ns.name[:2] not in ('nv', 'sd', 'hd'):
                continue
            # Skip if already successfully probed (READY state) with actual results
            # But re-probe if results are empty (unknown after wipe) or if probe FAILED
            if ns.hw_caps_state == ProbeState.READY and (ns.hw_caps or ns.hw_nopes):
                continue  # Already have results
            # Device must not be actively wiping (to avoid blocking)
            if ns.job:
                continue
            # Re-probe after wipe: worker state is still READY, need force
            if not (ns.hw_caps or ns.hw_nopes):
                ns.hw_caps_state = ProbeState.PENDING
                self.worker_manager.request_hw_caps(ns.name, force=True)
            self.dev_info.get_hw_capabilities(ns)

    def _poll_hw_caps_updates(self):
        """Poll for hw_caps probe completion without full device refresh.

        Called every 0.25 seconds in main loop to quickly show hw_caps
        results as soon as worker threads complete probes.
        """
        if not self.dev_info:
            return
        from .DeviceWorker import ProbeState
        # Only update devices that are still probing
        for ns in self.partitions.values():
            if ns.parent:
                continue
            # Update any device that's still PENDING or PROBING
            if ns.hw_caps_state in (ProbeState.PENDING, ProbeState.PROBING):
                self.dev_info.get_hw_capabilities(ns)

    def main_loop(self):
        """Main event loop"""

        # Create screen instances
        ThemeScreen = Theme.create_picker_screen(DiskWipeScreen)
        self.screens = {
            MAIN_ST: MainScreen(self),
            HELP_ST: HelpScreen(self),
            LOG_ST: HistoryScreen(self),
            THEME_ST: ThemeScreen(self),
        }

        # Create console window with custom pick highlighting
        win_opts = ConsoleWindowOpts(
            head_line=True,
            body_rows=200,
            head_rows=4,
            min_cols_rows=(60,10),
            # keys=self.spin.keys ^ other_keys,
            pick_attr=cs.A_REVERSE,  # Use reverse video for pick highlighting
            ctrl_c_terminates=False,
        )
        device_monitor = DeviceChangeMonitor(check_interval=1.0)
        device_monitor.start()
        print("Discovering devices...")
        # Create persistent worker manager for hw_caps probing
        # This is reused across device refreshes to allow probes to complete
        from .DeviceWorker import DeviceWorkerManager
        from .DrivePreChecker import DrivePreChecker
        worker_manager = DeviceWorkerManager(DrivePreChecker())
        # Initialize device info and pick range before first draw
        info = DeviceInfo(opts=self.opts, persistent_state=self.persistent_state,
                         worker_manager=worker_manager)
        self.partitions = info.assemble_partitions(self.partitions)
        # Start probing hw_caps immediately instead of waiting for first 3s refresh
        self.dev_info = info
        self.worker_manager = worker_manager
        self.get_hw_caps_when_needed()
        if self.opts.dump_lsblk:
            DeviceInfo.dump(self.partitions, title="after assemble_partitions")
            exit(1)

        self.win = ConsoleWindow(opts=win_opts)
        # Initialize screen stack
        self.stack = ScreenStack(self.win, None, SCREEN_NAMES, self.screens)

        spin = self.spin = OptionSpinner(stack=self.stack)
        spin.default_obj = self.opts
        spin.add_key('dense', 'D - dense/spaced view', vals=[False, True])
        spin.add_key('hist_time_format', 'a - time format',
                     vals=['ago+time', 'ago', 'time'], scope=LOG_ST)

        spin.add_key('quit', 'q,x - quit program', keys='qx', genre='action')
        spin.add_key('screen_escape', 'ESC- back one screen',
                     keys=[10,27,cs.KEY_ENTER], genre='action')
        spin.add_key('main_escape', 'ESC - clear filter',
                     keys=27, genre='action', scope=MAIN_ST)
        spin.add_key('wipe', 'w - wipe device', genre='action')
        spin.add_key('verify', 'v - verify device', genre='action')
        spin.add_key('stop', 's - stop wipe', genre='action')
        spin.add_key('block', 'b - block/unblock disk', genre='action')
        spin.add_key('delete_device', 'DEL - remove/unbind disk from system',
                         genre='action', keys=(cs.KEY_DC))
        spin.add_key('scan_all_devices', 'r - rescan devices and recheck capabilities',
                     genre='action', scope=MAIN_ST)
        spin.add_key('stop_all', 'S - stop ALL wipes', genre='action')
        spin.add_key('help', '? - show help screen', genre='action')
        spin.add_key('history', 'h - show wipe history', genre='action')
        spin.add_key('filter', '/ - filter devices by regex', genre='action')
        spin.add_key('theme_screen', 't - theme picker', genre='action', scope=MAIN_ST)
        spin.add_key('spin_theme', 't - theme', genre='action', scope=THEME_ST)
        spin.add_key('header_mode', '_ - header style', vals=['Underline', 'Reverse', 'Off'])
        spin.add_key('expand', 'e - expand history entry', genre='action', scope=LOG_ST)
        spin.add_key('copy', 'c - copy entry to clipboard', genre='action', scope=LOG_ST)
        spin.add_key('show_keys', 'K - show keys (demo mode)', genre='action')
        # Load theme from persistent state
        self.opts.theme = self.persistent_state.state.get('theme', '')
        Theme.set(self.opts.theme)
        self.win.set_handled_keys(self.spin.keys)

        # Background device change monitor started above

        pick_range = info.get_pick_range()
        self.win.set_pick_range(pick_range[0], pick_range[1])


        check_devices_mono = time.monotonic()
        cached_worker_state = {}  # Track marker/hw_caps state to detect updates

        try:
            while True:
                # Draw current screen
                current_screen = self.screens[self.stack.curr.num]
                current_screen.draw_screen()
                self.win.render()

                # Main thread timeout for responsive UI (background monitor checks every 0.2s)
                _ = self.do_key(self.win.prompt(seconds=0.25))

                # Handle actions using perform_actions
                self.stack.perform_actions(spin)

                # Poll for hw_caps completion without full device refresh (every 0.25s)
                # This lets us show results quickly even though commands take 1-3 seconds
                self._poll_hw_caps_updates()

                # Detect suspend/resume: loop runs every ~0.25s, so a large
                # gap in monotonic time means the system was likely suspended
                now_mono = time.monotonic()
                loop_gap = now_mono - check_devices_mono
                if loop_gap > 5.0 and hasattr(self, '_last_loop_mono'):
                    suspend_gap = now_mono - self._last_loop_mono
                    if suspend_gap > 5.0:
                        self.win.flash(f'Suspend detected ({suspend_gap:.0f}s gap), rescanning...', duration=1.5)
                        self.screens[MAIN_ST].scan_all_devices_ACTION()
                self._last_loop_mono = now_mono

                # Check for device changes from background monitor
                devices_changed = device_monitor.get_and_clear()
                time_since_refresh = now_mono - check_devices_mono

                # Build current worker state for comparison
                current_worker_state = {}
                if self.worker_manager:
                    for device_name, part in self.partitions.items():
                        current_worker_state[device_name] = {
                            'marker': part.marker,
                            'hw_caps_state': part.hw_caps_state
                        }
                    # Check if worker has any updates (markers or hw_caps changes)
                    worker_has_updates = (
                        self.worker_manager.has_updates(cached_worker_state)
                    )
                else:
                    worker_has_updates = False

                if (devices_changed or worker_has_updates or
                    time_since_refresh > 3.0):
                    # Refresh if: device changes, worker updates, OR periodic (3s default)
                    info = DeviceInfo(opts=self.opts, persistent_state=self.persistent_state,
                                     worker_manager=self.worker_manager)
                    self.partitions = info.assemble_partitions(self.partitions)
                    self.dev_info = info
                    # Update pick range to highlight NAME through SIZE fields
                    pick_range = info.get_pick_range()
                    self.win.set_pick_range(pick_range[0], pick_range[1])
                    # Probe hw_caps for devices that need it (only once per refresh, not every draw)
                    self.get_hw_caps_when_needed()
                    check_devices_mono = time.monotonic()
                    # Update cached state after refresh
                    cached_worker_state = current_worker_state.copy()

                # Save any persistent state changes
                self.persistent_state.save_updated_opts(self.opts)
                self.persistent_state.sync()

                self.win.clear()
        finally:
            # Clean up monitor thread on exit
            device_monitor.stop()
            # Clean up persistent worker manager
            if hasattr(self, 'worker_manager') and self.worker_manager:
                self.worker_manager.stop_all()

class DiskWipeScreen(Screen):
    """ TBD """
    app: DiskWipe
    refresh_seconds = 3.0  # Default refresh rate for screens

    def screen_escape_ACTION(self):
        """ return to main screen """
        self.app.stack.pop()

    def show_keys_ACTION(self):
        """ Show last key for demo"""
        self.app.win.set_demo_mode(enabled=None) # toggle it

class MainScreen(DiskWipeScreen):
    """Main device list screen"""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.persist_port_serial = set()


    def _port_serial_line(self, partition, has_children=True):
        wids = self.app.wids
        sep = '  '
        # Sanitize port/serial - some USB bridges return strings with embedded nulls
        port = partition.port.replace('\x00', '') if partition.port else ''
        serial = partition.serial.replace('\x00', '') if partition.serial else ''

        # Get column widths (with defaults if wids not yet initialized)
        wid_state = wids.state if wids else 5

        # Use corner └ if no children below, or vertical │ if there are children to connect to
        connector = '│' if has_children else ' '

        # Build base line: state padding + connector + port/serial
        base = f'{"":>{wid_state}}{sep}{connector}   └────── {port:<12} {serial}'

        return base

    def do_job_maintenance(self):
        """ Check all the jobs in progress and advance their state
            appropriately.
        """
        app = self.app
        for _, partition in app.partitions.items():
            partition.line = None
            if partition.job:
                if partition.job.done:
                    # Join with timeout to avoid UI freeze if thread is stuck in blocking I/O
                    partition.job.thread.join(timeout=5.0)
                    if partition.job.thread.is_alive():
                        # Thread didn't exit cleanly - continue anyway to avoid UI freeze
                        # Leave job attached so we can try again next refresh
                        partition.mounts = ['⚠ Thread stuck, retrying...']
                        continue

                    # Check if this was a standalone verify job or a wipe job
                    is_verify_only = getattr(partition.job, 'is_verify_only', False)

                    if is_verify_only:
                        # Standalone verification completed or stopped
                        if partition.job.do_abort:
                            # Verification was stopped - read marker to get previous status
                            marker = WipeJob.read_marker_buffer(partition.name)
                            prev_status = getattr(marker, 'verify_status', None) if marker else None
                            if prev_status == 'pass':
                                was = '✓'
                            elif prev_status == 'fail':
                                was = '✗'
                            else:
                                was = '-'
                            partition.mounts = [f'Stopped verification, was {was}']
                        else:
                            # Verification completed successfully
                            verify_result = partition.job.verify_result or "unknown"
                            partition.mounts = [f'Verified: {verify_result}']

                            # Check if this was an unmarked disk/partition (no existing marker)
                            # Whole disks (no parent) or partitions without filesystems
                            was_unmarked = partition.dflt == '-' and (not partition.parent or not partition.fstype)

                            # Check if verification passed (may include debug info)
                            verify_passed = verify_result in ('zeroed', 'random') or verify_result.startswith(('zeroed', 'random'))

                            # If this was an unmarked disk that passed verification,
                            # update state to 'W' as if it had been wiped
                            if was_unmarked and verify_passed:
                                partition.state = 'W'
                                partition.dflt = 'W'
                                partition.wiped_this_session = True  # Show green
                                # Clear any previous verify failure
                                if hasattr(partition, 'verify_failed_msg'):
                                    delattr(partition, 'verify_failed_msg')
                            # If unmarked partition failed verification, set persistent error
                            # NOTE: Only for unmarked disks - marked disks just show ✗ in marker
                            elif was_unmarked and not verify_passed:
                                error_msg = '⚠ VERIFY FAILED: Not wiped w/ Zero or Rand'
                                partition.mounts = [error_msg]
                                partition.verify_failed_msg = error_msg
                            else:
                                # Marked disk or other case - clear verify failure
                                if hasattr(partition, 'verify_failed_msg'):
                                    delattr(partition, 'verify_failed_msg')

                            # Log the verify operation
                            if partition.job.verify_start_mono:
                                elapsed = time.monotonic() - partition.job.verify_start_mono

                                # Determine if verification passed or failed
                                if verify_result in ('zeroed', 'random') or verify_result.startswith('random ('):
                                    result = 'OK'
                                    verify_detail = None
                                elif verify_result == 'error':
                                    result = 'FAIL'
                                    verify_detail = 'error'
                                elif verify_result == 'skipped':
                                    result = 'skip'
                                    verify_detail = None
                                else:
                                    # Failed verification - extract reason
                                    result = 'FAIL'
                                    # verify_result like "not-wiped (non-zero at 22K)" or "not-wiped (max=5.2%)"
                                    if '(' in verify_result:
                                        verify_detail = verify_result.split('(')[1].rstrip(')')
                                    else:
                                        verify_detail = verify_result

                                # Structured logging
                                Utils.log_wipe_structured(app.partitions, partition, partition.job)
                                # Legacy text log (keep for compatibility)
                                Utils.log_wipe(partition.name, partition.size_bytes, 'Vrfy', result, elapsed,
                                              uuid=partition.uuid, verify_result=verify_detail)
                        app.job_cnt -= 1
                        # Reset state back to default (was showing percentage during verify)
                        # Unless we just updated it above for unmarked verified disk
                        if partition.state.endswith('%'):
                            partition.state = partition.dflt
                        partition.job = None
                        partition.marker_checked = False  # Reset to "dont-know" - will re-read on next scan
                        partition.marker = ''  # Clear stale marker string to avoid showing old data during re-read
                        partition.monitor_marker = False  # Stop monitoring verify job
                    else:
                        # Wipe job completed (with or without auto-verify)
                        # Check if stopped during verify phase (after successful write)
                        if partition.job.do_abort and partition.job.verify_phase:
                            # Wipe completed but verification was stopped
                            to = 'W'
                            app.set_state(partition, to=to)
                            partition.dflt = to
                            partition.wiped_this_session = True
                            # Read marker to get previous verification status
                            marker = WipeJob.read_marker_buffer(partition.name)
                            prev_status = getattr(marker, 'verify_status', None) if marker else None
                            if prev_status == 'pass':
                                was = '✓'
                            elif prev_status == 'fail':
                                was = '✗'
                            else:
                                was = '-'
                            partition.mounts = [f'Stopped verification, was {was}']
                        else:
                            # Normal wipe completion or stopped during write
                            to = 's' if partition.job.do_abort else 'W'
                            app.set_state(partition, to=to)
                            partition.dflt = to
                            # Mark as wiped in this session (for green highlighting)
                            if to == 'W':
                                partition.wiped_this_session = True
                            partition.mounts = []
                        app.job_cnt -= 1
                        # Log the wipe operation
                        elapsed = time.monotonic() - partition.job.start_mono
                        result = 'stopped' if partition.job.do_abort else 'completed'
                        # Get the wipe type that was used (stored when wipe was started)
                        mode = getattr(partition, 'wipe_type', 'Unknown')
                        # Calculate percentage if stopped
                        pct = None
                        if partition.job.do_abort and partition.job.total_size > 0:
                            pct = int((partition.job.total_written / partition.job.total_size) * 100)
                        # Structured logging
                        Utils.log_wipe_structured(app.partitions, partition, partition.job, mode=mode)
                        # Legacy text log (keep for compatibility)
                        # Only pass label/fstype for stopped wipes (not completed)
                        if result == 'stopped':
                            Utils.log_wipe(partition.name, partition.size_bytes, mode, result, elapsed,
                                          uuid=partition.uuid, label=partition.label, fstype=partition.fstype, pct=pct)
                        else:
                            Utils.log_wipe(partition.name, partition.size_bytes, mode, result, elapsed,
                                          uuid=partition.uuid, pct=pct)

                        # Log auto-verify if it happened (verify_result will be set)
                        if partition.job.verify_result and partition.job.verify_start_mono:
                            verify_elapsed = time.monotonic() - partition.job.verify_start_mono
                            verify_result = partition.job.verify_result

                            # Determine if verification passed or failed
                            if verify_result in ('zeroed', 'random') or verify_result.startswith('random ('):
                                result = 'OK'
                                verify_detail = None
                            elif verify_result == 'error':
                                result = 'FAIL'
                                verify_detail = 'error'
                            elif verify_result == 'skipped':
                                result = 'skip'
                                verify_detail = None
                            else:
                                # Failed verification - extract reason
                                result = 'FAIL'
                                # verify_result like "not-wiped (non-zero at 22K)" or "not-wiped (max=5.2%)"
                                if '(' in verify_result:
                                    verify_detail = verify_result.split('(')[1].rstrip(')')
                                else:
                                    verify_detail = verify_result

                            # Note: Structured logging for verify was already logged above as part of the wipe
                            # This is just logging the separate verify phase stats to the legacy log
                            Utils.log_wipe(partition.name, partition.size_bytes, 'Vrfy', result, verify_elapsed,
                                          uuid=partition.uuid, verify_result=verify_detail)

                        if partition.job.exception:
                            app.win.alert(
                                message=f'FAILED: wipe {repr(partition.name)}\n{partition.job.exception}',
                                title='ALERT'
                            )

                        partition.job = None
                        partition.marker_checked = False  # Reset to "dont-know" - will re-read on next scan
                        partition.marker = ''  # Clear stale marker string to avoid showing old data during re-read
                        partition.monitor_marker = True  # Start monitoring the new marker
            if partition.job:
                elapsed, pct, rate, until, more_state = partition.job.get_status()

                # Get task display name (Zero, Rand, Crypto, Verify, etc.)
                task_name = ""
                if partition.job.current_task:
                    task_name = partition.job.current_task.get_display_name()

                # FLUSH goes in mounts column, not state
                if pct.startswith('FLUSH'):
                    partition.state = partition.dflt  # Keep default state (s, W, etc)
                    if rate and until:
                        partition.mounts = [f'{task_name} {pct} {elapsed} -{until} {rate}']
                    else:
                        partition.mounts = [f'{task_name} {pct} {elapsed}']
                    if more_state:
                        partition.mounts[0] += f' {more_state}'
                else:
                    partition.state = pct
                    # Build progress line with task name
                    progress_parts = [task_name, elapsed, f'-{until}', rate]

                    # Only show slowdown/stall for WriteTask (not VerifyTask or FirmwareWipeTask)
                    from .FirmwareWipeTask import FirmwareWipeTask
                    from .WriteTask import WriteTask
                    current_task = partition.job.current_task
                    if current_task and isinstance(current_task, WriteTask) and not isinstance(current_task, FirmwareWipeTask):
                        slowdown = partition.job.max_slowdown_ratio
                        stall = partition.job.max_stall_secs
                        progress_parts.extend([f'÷{slowdown}', f'𝚫{Utils.ago_str(stall)}'])

                    if more_state:
                        progress_parts.append(more_state)

                    partition.mounts = [' '.join(progress_parts)]

            if partition.parent and partition.parent in app.partitions and (
                    app.partitions[partition.parent].state in ('Blk', 'iBlk')):
                continue


    def draw_screen(self):
        """Draw the main device list"""
        app = self.app

        def wanted(name):
            return not app.filter or app.filter.search(name)

        self.do_job_maintenance()

        app.win.set_pick_mode(True)
        if app.opts.port_serial != 'Auto':
            self.persist_port_serial = set() # name of disks
        else: # if the disk goes away, clear persistence
            for name in list(self.persist_port_serial):
                if name not in app.partitions:
                    self.persist_port_serial.discard(name)

        # process jobs and collect visible partitions, sorted by disk then partition
        visible_partitions = []
        # Get disks sorted alphabetically
        disks = sorted([p for p in app.partitions.values() if p.parent is None],
                       key=lambda p: p.name)
        for disk in disks:
            if wanted(disk.name) or disk.job:
                visible_partitions.append(disk)
            # Add partitions for this disk, sorted alphabetically
            parts = sorted([p for p in app.partitions.values() if p.parent == disk.name],
                           key=lambda p: p.name)
            # If disk is directly blocked, hide children and aggregate their mounts
            if disk.state == 'Blk':
                # Collect all mounts from children, sort with "/" first (by name/length)
                all_mounts = []
                for part in parts:
                    all_mounts.extend(part.mounts)
                # Sort: "/" first, then by name (implicitly by length since "/" is shortest)
                all_mounts.sort(key=lambda m: (m != '/', m))
                disk.aggregated_mounts = all_mounts
                # Skip adding children to visible list
                continue
            else:
                disk.aggregated_mounts = None
            for part in parts:
                if wanted(part.name) or part.job:
                    visible_partitions.append(part)

        # Re-infer parent states (like 'Busy') after updating child job states
        DeviceInfo.set_all_states(app.partitions)

        # Build mapping of parent -> last visible child
        parent_last_child = {}
        for partition in visible_partitions:
            if partition.parent:
                parent_last_child[partition.parent] = partition.name

        # Second pass: display visible partitions with tree characters and Context
        prev_disk = None
        for partition in visible_partitions:
            # Add separator line between disk groups (unless in dense mode)
            if not app.opts.dense and partition.parent is None and prev_disk is not None:
                # Add dimmed separator line between disks
                separator = '─' * app.win.get_pad_width()
                app.win.add_body(separator, attr=cs.A_DIM, context=Context(genre='DECOR'))

            if partition.parent is None:
                prev_disk = partition.name

            is_last_child = False
            if partition.parent and partition.parent in parent_last_child:
                is_last_child = bool(parent_last_child[partition.parent] == partition.name)

            partition.line, attr = app.dev_info.part_str(partition, is_last_child=is_last_child)
            # Create context with partition reference
            ctx = Context(genre='disk' if partition.parent is None else 'partition',
                         partition=partition)
            # For disks, underline just the alphanumeric part of fw capability text
            fw_ul = getattr(partition, '_fw_underline', None)
            if fw_ul:
                ul_start, ul_end = fw_ul
                app.win.add_body(partition.line[:ul_start], attr=attr, context=ctx)
                ul_attr = (attr or cs.A_NORMAL) | cs.A_UNDERLINE
                app.win.add_body(partition.line[ul_start:ul_end], attr=ul_attr, resume=True)
                app.win.add_body(partition.line[ul_end:], attr=attr, resume=True)
            else:
                app.win.add_body(partition.line, attr=attr, context=ctx)
            if partition.parent is None and app.opts.port_serial != 'Off':
                doit = bool(app.opts.port_serial == 'On')
                if not doit:
                    doit = bool(partition.name in self.persist_port_serial)
                if not doit and app.test_state(partition, to='0%'):
                    doit = True
                    self.persist_port_serial.add(partition.name)
                if doit:
                    # Check if this disk has any visible child partitions
                    has_children = partition.name in parent_last_child
                    line = self._port_serial_line(partition, has_children)
                    port_attr = (attr or cs.A_NORMAL) & ~cs.A_BOLD
                    app.win.add_body(line, attr=port_attr, context=Context(genre='DECOR'))

            # Show inline confirmation prompt if this is the partition being confirmed
            if app.confirmation.active and app.confirmation.identity == partition.name:
                # Build confirmation message
                if app.confirmation.action_type == 'wipe':
                    msg = f'⚠️ WIPE'
                else:  # verify
                    msg = f'⚠️ VERIFY [writes marker]'

                # Add mode-specific prompt (base message without input)
                if app.confirmation.mode == 'yes':
                    msg += " - Type 'yes': "
                elif app.confirmation.mode == 'identity':
                    msg += f" - Type '{partition.name}': "
                elif app.confirmation.mode == 'choices':
                    choices_str = ','.join(app.confirmation.choices)
                    msg += f" Choice ({choices_str}): "

                # Position message at fixed column (reduced from 28 to 20)
                msg = ' ' * 5 + msg

                # Add confirmation message base as DECOR (non-pickable)
                app.win.add_body(msg, attr=cs.color_pair(Theme.DANGER) | cs.A_BOLD,
                               context=Context(genre='DECOR'))

                # Add input or hint on same line
                if app.confirmation.input_buffer:
                    # Show current input with cursor
                    app.win.add_body(app.confirmation.input_buffer + '_',
                                   attr=cs.color_pair(Theme.DANGER) | cs.A_BOLD,
                                   resume=True)
                else:
                    # Show hint in dimmed italic
                    hint = app.confirmation.get_hint()
                    app.win.add_body(hint, attr=cs.A_DIM | cs.A_ITALIC, resume=True)

        app.win.add_fancy_header(app.get_keys_line(), mode=app.opts.header_mode)

        app.win.add_header(app.dev_info.head_str, attr=cs.A_DIM)
        _, col = app.win.head.pad.getyx()
        pad = ' ' * (app.win.get_pad_width() - col)
        app.win.add_header(pad, resume=True, attr=cs.A_DIM)

    ######################################### ACTIONS #####################
    @staticmethod
    def clear_hotswap_marker(part):
        """Clear the hot-swap marker (^) when user performs a hard action"""
        if part.state == '^':
            part.state = '-'
            # Also update dflt so verify/wipe operations restore to '-' not '^'
            part.dflt = '-'
        # Also clear the newly_inserted flag
        if hasattr(part, 'newly_inserted'):
            delattr(part, 'newly_inserted')

    def main_escape_ACTION(self):
        """ Handle ESC clearing filter and move to top"""
        app = self.app
        app.prev_filter = ''
        app.filter = None
        app.filter_bar._text = ''  # Also clear filter bar text
        app.win.pick_pos = 0

    def theme_screen_ACTION(self):
        """ handle 't' from Main Screen """
        self.app.stack.push(THEME_ST, self.app.win.pick_pos)

    def quit_ACTION(self):
        """Handle quit action (q or x key pressed)"""
        app = self.app

        # Check for firmware wipes - cannot quit while they're running
        if app._has_any_firmware_wipes():
            # Show alert - cannot quit during firmware wipe
            app.filter_bar._text = 'Cannot quit: firmware wipe running'
            return

        def stop_if_idle(part):
            if part.state[-1] == '%':
                if part.job and not part.job.done:
                    # Skip firmware wipes - they cannot be stopped
                    if app._is_firmware_wipe(part):
                        return 1  # Count as running but don't stop
                    part.job.do_abort = True
            return 1 if part.job else 0

        def stop_all():
            rv = 0
            for part in app.partitions.values():
                rv += stop_if_idle(part)
            return rv

        def exit_if_no_jobs():
            if stop_all() == 0:
                app.win.stop_curses()
                _restore_terminal()
                sys.exit(0)

        app.exit_when_no_jobs = True
        app.filter = re.compile('STOPPING', re.IGNORECASE)
        app.prev_filter = 'STOPPING'
        app.filter_bar._text = 'STOPPING'  # Update filter bar display
        exit_if_no_jobs()

    def wipe_ACTION(self):
        """Handle 'w' key"""
        app = self.app
        if not app.pick_is_running:
            ctx = app.win.get_picked_context()
            if ctx and hasattr(ctx, 'partition'):
                part = ctx.partition
                if app.test_state(part, to='0%'):
                    self.clear_hotswap_marker(part)
                    # Build choices: Zero, Rand, and any firmware wipe types
                    # Sort all by rank (worst to best) with '*' on recommended (last) one
                    from .DrivePreChecker import DrivePreChecker
                    choices = ['Zero', 'Rand']
                    fw_modes = []
                    if part.hw_caps:
                        # Strip '*' from hw_caps modes (already has it from display string)
                        fw_modes = [m.strip().rstrip('*') for m in part.hw_caps.split(',')]
                        choices.extend(fw_modes)
                    # Use HDD rankings (prefer software wipes) ONLY if:
                    # 1. Device reports as rotational, AND
                    # 2. Device has no crypto-capable firmware wipes
                    # Note: 'Enhanced' is available on HDDs too (slow overwrite, not crypto)
                    # Only SCrypto/Crypto/FCrypto definitively indicate SSD with crypto
                    is_rotational = getattr(part, 'is_rotational', False)
                    has_crypto_fw = any(m in fw_modes for m in ('SCrypto', 'Crypto', 'FCrypto'))
                    use_hdd_ranking = is_rotational and not has_crypto_fw
                    choices = DrivePreChecker.sort_modes_by_rank(choices, is_rotational=use_hdd_ranking)
                    app.confirmation.start(action_type='wipe',
                               identity=part.name, mode='choices', choices=choices)
                    app.win.passthrough_mode = True

    def verify_ACTION(self):
        """Handle 'v' key"""
        app = self.app
        ctx = app.win.get_picked_context()
        if ctx and hasattr(ctx, 'partition'):
            part = ctx.partition
            # Use get_actions() to ensure we use the same logic as the header display
            _, actions = app.get_actions(part)
            if 'v' in actions:
                # Safety check: Prevent verification on firmware wipes
                # (Firmware wipes have built-in pre/post verification)
                if app._is_firmware_wipe_marker(part):
                    part.mounts = ['⚠ Firmware wipes have built-in verification - standard verify not allowed']
                    return

                self.clear_hotswap_marker(part)
                # Check if this is an unmarked disk/partition (potential data loss risk)
                # Whole disks (no parent) or partitions without filesystems need confirmation
                is_unmarked = part.state == '-' and (not part.parent or not part.fstype)
                if is_unmarked:
                    # Require confirmation for unmarked partitions
                    app.confirmation.start(action_type='verify',
                                           identity=part.name, mode="yes")
                    app.win.passthrough_mode = True
                else:
                    # Marked partition - proceed directly
                    # Clear any previous verify failure message when starting new verify
                    if hasattr(part, 'verify_failed_msg'):
                        delattr(part, 'verify_failed_msg')
                    part.job = WipeJob.start_verify_job(f'/dev/{part.name}',
                                                        part.size_bytes, opts=app.opts)
                    app.job_cnt += 1

    def scan_all_devices_ACTION(self):
        """ Trigger a re-scan of all devices to make the appear
        quicker in the list"""
        # Show temporary feedback
        self.app.win.flash('Scanning devices and rechecking firmware capabilities...', duration=0.75)

        # SCSI host rescan (for SATA devices)
        base_path = '/sys/class/scsi_host'
        if os.path.exists(base_path):
            for host in os.listdir(base_path):
                scan_file = os.path.join(base_path, host, 'scan')
                if os.path.exists(scan_file):
                    try:
                        with open(scan_file, 'w', encoding='utf-8') as f:
                            f.write("- - -")
                    except Exception:
                        pass

        # Rebind any unbound NVMe devices
        self._rebind_nvme_devices()

        # Reset hw_caps stickiness for all devices so they'll be re-probed
        # This allows detecting hardware state changes after sleep/wake cycles
        if self.app.worker_manager:
            for partition in self.app.partitions.values():
                # Queue worker to re-probe this device's capabilities
                if partition.parent: # only need to do whole disks
                    continue
                self.app.worker_manager.request_hw_caps(partition.name, force=True)
                # Clear cached values so UI refreshes with new probing state
                partition.hw_caps = ''
                partition.hw_nopes = ''
                partition.hw_caps_state = ProbeState.PENDING

    def delete_device_ACTION(self):
        """ DEL key -- Cause the OS to drop a SATA device or unbind an NVMe device
            so it can be replaced sooner """
        app = self.app
        ctx = app.win.get_picked_context()
        if ctx and hasattr(ctx, 'partition'):
            part = ctx.partition
            if not part or part.parent or not app.test_state(part, to='0%'):
                return
            # NVMe unbind - write PCI address to driver unbind file
            if part.name.startswith('nvme'):
                pci_addr = self._get_nvme_pci_address(part.name)
                if pci_addr:
                    unbind_path = "/sys/bus/pci/drivers/nvme/unbind"
                    if os.path.exists(unbind_path):
                        try:
                            with open(unbind_path, 'w', encoding='utf-8') as f:
                                f.write(pci_addr)
                            return True
                        except Exception:
                            pass
            # SATA/IDE delete - write 1 to device delete file
            else:
                path = f"/sys/block/{part.name}/device/delete"
                if os.path.exists(path):
                    try:
                        with open(path, 'w', encoding='utf-8') as f:
                            f.write("1")
                        return True
                    except Exception:
                        pass

    def _get_nvme_pci_address(self, device_name):
        """Get the full PCI address for an NVMe device (e.g., '0000:01:00.0')

        The sysfs path may contain multiple PCI addresses (bridges), so we need
        the last one before /nvme/ which is the actual NVMe controller.
        """
        try:
            sysfs_path = f'/sys/class/block/{device_name}'
            if os.path.exists(sysfs_path):
                real_path = os.path.realpath(sysfs_path)
                # Find all PCI addresses and take the last one (the NVMe controller)
                pci_matches = re.findall(r'(0000:[0-9a-f]{2}:[0-9a-f]{2}\.[0-9a-f])', real_path, re.I)
                if pci_matches:
                    return pci_matches[-1]
        except Exception:
            pass
        return None

    def _rebind_nvme_devices(self):
        """Find and rebind any unbound NVMe devices.

        Scans PCI devices for NVMe controllers (class 0x010802) that have no
        driver bound, and attempts to bind them to the nvme driver.
        """
        pci_devices = '/sys/bus/pci/devices'
        nvme_bind = '/sys/bus/pci/drivers/nvme/bind'
        if not os.path.exists(pci_devices) or not os.path.exists(nvme_bind):
            return

        for pci_addr in os.listdir(pci_devices):
            device_path = os.path.join(pci_devices, pci_addr)
            # Check if this is an NVMe controller (class 0x010802)
            class_file = os.path.join(device_path, 'class')
            try:
                with open(class_file, 'r', encoding='utf-8') as f:
                    device_class = f.read().strip()
                if device_class != '0x010802':
                    continue
                # Check if driver is already bound
                driver_link = os.path.join(device_path, 'driver')
                if os.path.exists(driver_link):
                    continue
                # Try to bind to nvme driver
                with open(nvme_bind, 'w', encoding='utf-8') as f:
                    f.write(pci_addr)
            except Exception:
                pass

    def stop_ACTION(self):
        """Handle 's' key - stop current wipe (but not firmware wipes)"""
        app = self.app
        if app.pick_is_running:
            ctx = app.win.get_picked_context()
            if ctx and hasattr(ctx, 'partition'):
                part = ctx.partition
                if part.state[-1] == '%':
                    if part.job and not part.job.done:
                        # Skip firmware wipes - they cannot be safely stopped
                        if app._is_firmware_wipe(part):
                            return
                        part.job.do_abort = True


    def stop_all_ACTION(self):
        """Handle 'S' key - stop all wipes (but not firmware wipes)"""
        app = self.app
        for part in app.partitions.values():
            if part.state[-1] == '%':
                if part.job and not part.job.done:
                    # Skip firmware wipes - they cannot be safely stopped
                    if app._is_firmware_wipe(part):
                        continue
                    part.job.do_abort = True

    def block_ACTION(self):
        """Handle 'b' key"""
        app = self.app
        ctx = app.win.get_picked_context()
        if ctx and hasattr(ctx, 'partition'):
            part = ctx.partition
            self.clear_hotswap_marker(part)
            app.set_state(part, 'Unbl' if part.state == 'Blk' else 'Blk')

    def help_ACTION(self):
        """Handle '?' key - push help screen"""
        app = self.app
        if hasattr(app, 'spin') and hasattr(app.spin, 'stack'):
            app.spin.stack.push(HELP_ST, app.win.pick_pos)

    def history_ACTION(self):
        """Handle 'h' key - push history screen"""
        app = self.app
        if hasattr(app, 'spin') and hasattr(app.spin, 'stack'):
            app.spin.stack.push(LOG_ST, app.win.pick_pos)

    def filter_ACTION(self):
        """Handle '/' key - start incremental filter search"""
        app = self.app
        app.filter_bar.start(app.prev_filter)
        app.win.passthrough_mode = True


class HelpScreen(DiskWipeScreen):
    """Help screen"""

    def draw_screen(self):
        """Draw the help screen"""
        app = self.app
        spinner = self.get_spinner()

        app.win.set_pick_mode(False)
        if spinner:
            spinner.show_help_nav_keys(app.win)
            spinner.show_help_body(app.win)

        # Add CLI Options section
        app.win.add_body('Command Line Arguments:', attr=cs.A_UNDERLINE)
        opts, wid = app.opts, 8
        cli_options = [
            f'--mode: . . . .  {opts.mode:<{wid}} Wipe mode',
            f'--passes: . . .  {opts.passes:<{wid}} Passes for software wipes',
            f'--verify-pct: .  {opts.verify_pct:<{wid}} Verification %',
            f'--port-serial:   {opts.port_serial:<{wid}} Show port/serial/FwCAPS',
            f'--slowdown-stop: {opts.slowdown_stop:<{wid}} Stop if disk slows',
            f'--stall-timeout: {opts.stall_timeout:<{wid}} Stall timeout in sec',
        ]
        for opt in cli_options:
            app.win.add_body(opt, attr=cs.A_DIM)


class HistoryScreen(DiskWipeScreen):
    """History/log screen showing structured log entries with expand/collapse functionality"""

    refresh_seconds = 60.0  # Slower refresh for history screen to allow copy/paste

    def __init__(self, app):
        super().__init__(app)
        self.expands = {}  # Maps timestamp -> True (expanded) or False (collapsed)
        self.entries = []  # Cached log entries (all entries before filtering)
        self.filtered_entries = []  # Entries after search filtering
        self.window_of_logs = None  # Window of log entries (OrderedDict)
        self.window_state = None  # Window state for incremental reads
        self.search_matches = set()  # Set of timestamps with deep-only matches in JSON
        self.prev_filter = ''

        # Setup search bar
        self.search_bar = IncrementalSearchBar(
            on_change=self._on_search_change,
            on_accept=self._on_search_accept,
            on_cancel=self._on_search_cancel
        )

    def _on_search_change(self, text):
        """Called when search text changes - filter entries incrementally."""
        self._filter_entries(text)

    def _on_search_accept(self, text):
        """Called when ENTER pressed in search - keep filter active, exit input mode."""
        self.app.win.passthrough_mode = False
        self.prev_filter = text

    def _on_search_cancel(self, original_text):
        """Called when ESC pressed in search - restore and exit search mode."""
        self._filter_entries(original_text)
        self.app.win.passthrough_mode = False

    def _filter_entries(self, search_text):
        """Filter entries based on search text (shallow or deep)."""
        if not search_text:
            self.filtered_entries = self.entries
            self.search_matches = set()
            return

        # Deep search mode if starts with /
        deep_search = search_text.startswith('/')
        pattern = search_text[1:] if deep_search else search_text

        if not pattern:
            self.filtered_entries = self.entries
            self.search_matches = set()
            return

        # Use StructuredLogger's filter method
        # logger = Utils.get_logger()
        self.filtered_entries, self.search_matches = StructuredLogger.filter_entries(
            self.entries, pattern, deep=deep_search
        )

    def draw_screen(self):
        """Draw the history screen with structured log entries"""

        def format_ago(timestamp):
            nonlocal now_dt
            ts = datetime.fromisoformat(timestamp)
            delta = now_dt - ts
            return Utils.ago_str(int(round(delta.total_seconds())))


        now_dt = datetime.now()
        app = self.app
        win = app.win
        win.set_pick_mode(True)

        # Get window of log entries (chronological order - eldest to youngest)
        logger = Utils.get_logger()
        if self.window_of_logs is None:
            self.window_of_logs, self.window_state = logger.get_window_of_entries(window_size=1000)
        else:
            # Refresh window with any new entries
            self.window_of_logs, self.window_state = logger.refresh_window(
                self.window_of_logs, self.window_state, window_size=1000
            )

        # Convert to list in reverse order (newest first for display)
        self.entries = list(reversed(list(self.window_of_logs.values())))

        # Clean up self.expands: remove any timestamps that are no longer in entries
        valid_timestamps = {entry.timestamp for entry in self.entries}
        self.expands = {ts: state for ts, state in self.expands.items() if ts in valid_timestamps}

        # Apply search filter if active
        if not self.search_bar.text:
            self.filtered_entries = self.entries
            self.search_matches = set()

        # Count by level in filtered results
        level_counts = {}
        for e in self.filtered_entries:
            level_counts[e.level] = level_counts.get(e.level, 0) + 1

        # Build search display string
        search_display = self.search_bar.get_display_string(prefix='', suffix='')

        # Build level summary for header
        # level_summary = ' '.join(f'{lvl}:{cnt}' for lvl, cnt in sorted(level_counts.items()))

        # Header
        # header_line = f'ESC:back [e]xpand [/]search {len(self.filtered_entries)}/{len(self.entries)} ({level_summary}) '
        header_line = f'ESC:back [e]xpand [c]opy [/]search {len(self.filtered_entries)}/{len(self.entries)} '
        if search_display:
            header_line += f'/ {search_display}'
        else:
            header_line += '/'
        win.add_header(header_line)
        win.add_header(f'Log: {logger.log_file}')

        # Build display
        for entry in self.filtered_entries:
            timestamp = entry.timestamp

            # Get display summary from entry
            summary = entry.display_summary

            # Format timestamp based on spinner setting
            time_format = app.opts.hist_time_format 

            if time_format == 'ago':
                timestamp_display = f"{format_ago(timestamp):>6}"
            elif time_format == 'ago+time':
                ago = format_ago(timestamp)
                time_str = timestamp[:19]
                timestamp_display = f"{ago:>6} {time_str}"
            else:  # 'time'
                timestamp_display = timestamp[:19]  # Just the date and time part (YYYY-MM-DD HH:MM:SS)

            level = entry.level

            # Add deep match indicator if this entry matched only in JSON
            deep_indicator = " *" if timestamp in self.search_matches else ""

            # Choose color based on log level
            if level == 'ERR':
                level_attr = cs.color_pair(Theme.ERROR) | cs.A_BOLD
            elif level in ('WIPE_STOPPED', 'VERIFY_STOPPED'):
                level_attr = cs.color_pair(Theme.WARNING) | cs.A_BOLD
            elif level in ('WIPE_COMPLETE', 'VERIFY_COMPLETE'):
                level_attr = cs.color_pair(Theme.SUCCESS) | cs.A_BOLD
            else:
                level_attr = cs.A_BOLD

            line = f"{timestamp_display} {summary}{deep_indicator}"
            win.add_body(line, attr=level_attr, context=Context("header", timestamp=timestamp))

            # Handle expansion - show the structured data
            if self.expands.get(timestamp, False):
                # Show the full entry data as formatted JSON
                try:
                    data_dict = entry.to_dict()
                    # Format just the 'data' field if it exists, otherwise show all
                    if 'data' in data_dict and data_dict['data']:
                        formatted = json.dumps(data_dict['data'], indent=2)
                    else:
                        formatted = json.dumps(data_dict, indent=2)

                    lines = formatted.split('\n')
                    for line in lines:
                        win.add_body(f"  {line}", context=Context("body", timestamp=timestamp))

                except (TypeError, ValueError, KeyError) as e:
                    win.add_body(f"  (error formatting: {e})", attr=cs.A_DIM)

            # Empty line between entries
            win.add_body("", context=Context("DECOR"))

    def expand_ACTION(self):
        """'e' key - Expand/collapse current entry"""
        app = self.app
        win = app.win
        ctx = win.get_picked_context()

        if ctx and hasattr(ctx, 'timestamp'):
            timestamp = ctx.timestamp
            # Toggle between collapsed and expanded
            current = self.expands.get(timestamp, False)
            if current: # Collapsing
                del self.expands[timestamp]
                # Search backwards to find first context with matching timestamp
                # This should be the header line of this entry
                test_pos = win.pick_pos - 1
                while test_pos >= 0:
                    test_ctx = win.body.contexts[test_pos]
                    if test_ctx and hasattr(test_ctx, 'timestamp') and test_ctx.timestamp == timestamp:
                        win.pick_pos = test_pos
                        test_pos -= 1
                    else:
                        return
            else:
                # Expanding - just toggle, cursor stays where it is
                self.expands[timestamp] = True

    def filter_ACTION(self):
        """'/' key - Start incremental search"""
        app = self.app
        self.search_bar.start(self.prev_filter)
        app.win.passthrough_mode = True

    def copy_ACTION(self):
        """'c' key - Copy current entry to clipboard or print to terminal"""
        from .Utils import ClipboardHelper
        app = self.app
        win = app.win
        ctx = win.get_picked_context()

        if not ctx or not hasattr(ctx, 'timestamp'):
            return

        # Find the entry by timestamp
        timestamp = ctx.timestamp
        entry = None
        for e in self.entries:
            if e.timestamp == timestamp:
                entry = e
                break

        if not entry:
            return

        # Format entry as JSON
        entry_text = json.dumps(entry.to_dict(), indent=2)

        # Try clipboard first
        if ClipboardHelper.has_clipboard():
            success, error = ClipboardHelper.copy(entry_text)
            if success:
                # Show brief success indicator - use the header context temporarily
                # The message will be visible until next refresh
                win.add_header(f'Copied to clipboard ({ClipboardHelper.get_method_name()})')
            else:
                win.add_header(f'Clipboard error: {error}')
        else:
            # Terminal fallback: exit curses, print, wait for input
            self._copy_terminal_fallback(entry_text)

    def _copy_terminal_fallback(self, text):
        """Print entry to terminal when clipboard unavailable (e.g., SSH sessions)."""
        from .Utils import ClipboardHelper
        app = self.app

        # Exit curses to use the terminal
        ConsoleWindow.stop_curses()
        _restore_terminal()

        # Print with border
        print('=' * 60)
        print('LOG ENTRY (copy manually from terminal):')
        print('=' * 60)
        print(text)
        print('=' * 60)
        print(f'\nClipboard: {ClipboardHelper.get_method_name()}')
        print('\nPress ENTER to return to dwipe...')

        # Wait for user input
        try:
            input()
        except EOFError:
            pass

        # Restore curses
        ConsoleWindow.start_curses()
        app.win.pick_pos = app.win.pick_pos  # Force position refresh

"""
DeviceWorker - Background thread for non-blocking device probing

Each device gets its own worker thread that handles potentially-blocking
operations (sysfs reads, hdparm, nvme commands) without freezing the UI.
"""
import os
import time
import threading
import queue
from enum import Enum

from .DrivePreChecker import DrivePreChecker


class ProbeState(Enum):
    """State of a probe operation"""
    PENDING = "pending"    # Not yet probed
    PROBING = "probing"    # Probe in progress
    READY = "ready"        # Value available
    FAILED = "failed"      # Probe failed/timed out
    STALE = "stale"        # Needs refresh


class DeviceWorker(threading.Thread):
    """Background worker thread for a single device.

    Handles potentially-blocking operations like:
    - Reading sysfs attributes (can block if device in D state)
    - Running hdparm/nvme commands for hardware capabilities

    Main thread reads cached state via get_state() - never blocks.
    """

    def __init__(self, device_name, checker=None):
        super().__init__(daemon=True, name=f"DeviceWorker-{device_name}")
        self.device_name = device_name
        self.checker = checker or DrivePreChecker()

        # Thread control
        self._running = True
        self._work_queue = queue.Queue()

        # Thread-safe state storage
        self._lock = threading.Lock()
        self._state = {
            # Probe states
            'hw_caps_state': ProbeState.PENDING,
            'serial_state': ProbeState.PENDING,
            'model_state': ProbeState.PENDING,

            # Cached values
            'hw_caps': '',  # String with wipe mode names: "Ovwr, Block, Crypto*" (for choices, thread-safe)
            'hw_caps_summary': '',  # Summary like "ϟCrypto" (for display, thread-safe)
            'hw_nopes': '',  # String with issue names: "Frozen, Locked" (for display, thread-safe)
            'serial': '',
            'model': '',
            'vendor': '',

            # Marker handling (formatted string returned to UI)
            'marker_formatted': '',  # Empty string = no marker (or not wanted)
            'want_marker': False,  # Main thread sets this; worker reads it

            # Marker polling metadata (worker only)
            'last_marker_check_time': 0,
            'marker_found_time': 0,  # When marker was first found (0 = not found)
            'marker_search_start_time': 0,  # When we started looking for marker

            # Metadata
            'last_error': None,
            'probe_count': 0,
            'is_usb': False,  # True if device is on USB bus
            'is_rotational': False,  # True if HDD (spinning disk)
        }

        # Device type detection
        self._is_nvme = device_name.startswith('nv')
        self._is_sata = device_name.startswith('sd')
        self._is_hd = device_name.startswith('hd')

    def run(self):
        """Main worker loop - processes work queue and continuous marker polling."""
        while self._running:
            try:
                # Wait for work with short timeout (allows responsive polling and clean shutdown)
                task = self._work_queue.get(timeout=0.2)
                if task == 'stop':
                    break
                elif task == 'hw_caps':
                    self._probe_hw_caps()
                elif task == 'serial':
                    self._probe_serial()
                elif task == 'model':
                    self._probe_model()
                elif task == 'refresh_all':
                    self._probe_serial()
                    self._probe_model()
                elif task == 'check_marker':
                    # Immediate marker check (used after wipes for quick feedback)
                    self._probe_marker_once()
            except queue.Empty:
                # No task - check if we should probe marker based on polling strategy
                self._check_marker_polling()

            except Exception as e:
                with self._lock:
                    self._state['last_error'] = str(e)

    def stop(self):
        """Signal worker to stop."""
        self._running = False
        self._work_queue.put('stop')

    def request_hw_caps(self, force=False):
        """Request hardware capabilities probe (non-blocking).
        Args:
            force: If True, reset cached state and re-probe even if already done.
        """
        with self._lock:
            if force or self._state['hw_caps_state'] == ProbeState.PENDING:
                self._state['hw_caps_state'] = ProbeState.PROBING
                self._state['hw_caps'] = ''
                self._state['hw_caps_summary'] = ''
                self._state['hw_nopes'] = ''
                self._work_queue.put('hw_caps')

    def set_want_marker(self, want):
        """Set whether we want to monitor for a marker (non-blocking).

        Args:
            want: Boolean - True to enable polling, False to clear and stop
        """
        with self._lock:
            prev_want = self._state['want_marker']
            self._state['want_marker'] = want

            # If enabling monitoring, always reset search time (fresh start after wipe)
            if want:
                now = time.time()
                self._state['marker_search_start_time'] = now
                self._state['marker_found_time'] = 0
            # If disabling monitoring, clear everything
            elif prev_want:
                self._state['marker_formatted'] = ''
                self._state['marker_search_start_time'] = 0
                self._state['marker_found_time'] = 0

    def request_refresh(self):
        """Request refresh of dynamic data (non-blocking)."""
        self._work_queue.put('refresh_all')

    def request_marker_check(self):
        """Request immediate marker check (non-blocking).

        Used to get quick feedback after wipes complete without waiting
        for the polling interval.
        """
        self._work_queue.put('check_marker')

    def get_state(self):
        """Get current cached state (thread-safe, non-blocking).

        Returns:
            dict with all cached device info and probe states
        """
        with self._lock:
            return self._state.copy()

    def get_hw_caps(self):
        """Get hardware capabilities if ready.

        Returns:
            tuple: (hw_caps, hw_caps_summary, hw_nopes, state, is_usb, is_rotational)
                   hw_caps: full sorted list for choices, hw_caps_summary: compact display
        """
        with self._lock:
            return (
                self._state['hw_caps'],
                self._state['hw_caps_summary'],
                self._state['hw_nopes'],
                self._state['hw_caps_state'],
                self._state['is_usb'],
                self._state['is_rotational']
            )

    def get_marker_formatted(self):
        """Get formatted marker string (ready to display).

        Returns:
            str: Formatted marker string, or empty string if no marker
        """
        with self._lock:
            return self._state['marker_formatted']

    def _probe_hw_caps(self):
        """Probe hardware wipe capabilities (runs in worker thread)."""
        with self._lock:
            self._state['hw_caps_state'] = ProbeState.PROBING
            self._state['probe_count'] += 1
            # Detect USB (SATA-over-USB bridges can still support ATA passthrough)
            self._state['is_usb'] = self._is_usb_device()
            # Detect HDD vs SSD for ranking preferences
            self._state['is_rotational'] = self._is_rotational_device()

        try:
            dev_path = f"/dev/{self.device_name}"
            if self._is_nvme:
                result = self.checker.check_nvme_drive(dev_path)
            elif self._is_sata or self._is_hd:
                result = self.checker.check_ata_drive(dev_path)
            else:
                # Unknown device type
                with self._lock:
                    self._state['hw_caps_state'] = ProbeState.READY
                return

            # Store results as strings (immutable, no locking needed for reads)
            # Sort modes by rank (worst to best) with '*' on the recommended (last) one
            with self._lock:
                is_rotational = self._state['is_rotational']
                if result.modes:
                    # Use HDD rankings ONLY if rotational AND no crypto-capable firmware
                    # Note: 'Enhanced' is available on HDDs too (slow overwrite, not crypto)
                    # Only SCrypto/Crypto/FCrypto definitively indicate SSD with crypto
                    has_crypto_fw = any(m in result.modes for m in ('SCrypto', 'Crypto', 'FCrypto'))
                    use_hdd_ranking = is_rotational and not has_crypto_fw
                    sorted_modes = DrivePreChecker.sort_modes_by_rank(
                        result.modes.keys(), add_star=not use_hdd_ranking, is_rotational=use_hdd_ranking)
                    self._state['hw_caps'] = ', '.join(sorted_modes)
                    self._state['hw_caps_summary'] = DrivePreChecker.get_fw_caps_summary(result.modes.keys())
                else:
                    self._state['hw_caps'] = ''
                    self._state['hw_caps_summary'] = ''
                self._state['hw_nopes'] = ', '.join(result.issues.keys()) if result.issues else ''
                self._state['hw_caps_state'] = ProbeState.READY

        except Exception as e:
            with self._lock:
                self._state['hw_caps_state'] = ProbeState.FAILED
                self._state['last_error'] = f"hw_caps: {e}"

    def _probe_serial(self):
        """Probe device serial number (runs in worker thread)."""
        with self._lock:
            self._state['serial_state'] = ProbeState.PROBING

        try:
            serial = self._read_sysfs_attr('device/serial')
            if not serial:
                # Try VPD page 80 (can be slow/blocking)
                serial = self._read_vpd_serial()

            with self._lock:
                self._state['serial'] = serial
                self._state['serial_state'] = ProbeState.READY

        except Exception as e:
            with self._lock:
                self._state['serial_state'] = ProbeState.FAILED
                self._state['last_error'] = f"serial: {e}"

    def _probe_model(self):
        """Probe device model (runs in worker thread)."""
        with self._lock:
            self._state['model_state'] = ProbeState.PROBING

        try:
            vendor = self._read_sysfs_attr('device/vendor')
            model = self._read_sysfs_attr('device/model')

            with self._lock:
                self._state['vendor'] = vendor
                self._state['model'] = model
                self._state['model_state'] = ProbeState.READY

        except Exception as e:
            with self._lock:
                self._state['model_state'] = ProbeState.FAILED
                self._state['last_error'] = f"model: {e}"

    def _check_marker_polling(self):
        """Check if we should probe marker based on adaptive polling strategy.

        Polling rates:
        - 1s for first 12s when marker wanted but not found (initial search)
        - 3s when marker has been found (keep it fresh)
        - 30s when not found and initial search expired (wait for next wipe)
        - Never poll when want_marker is False
        """
        with self._lock:
            if not self._state['want_marker']:
                return  # Not monitoring

            now = time.time()
            last_check = self._state['last_marker_check_time']
            search_start = self._state['marker_search_start_time']
            found_time = self._state['marker_found_time']

            # Determine polling interval based on state
            if found_time > 0:
                # Marker was found - refresh every 3s
                interval = 3.0
            elif now - search_start < 12.0:
                # Still in initial search window (first 12s) - check every 1s
                interval = 1.0
            else:
                # Initial search expired, no marker found - wait 30s before next check
                interval = 30.0

        # Check if enough time has passed
        if now - last_check >= interval:
            self._probe_marker_once()

    def _probe_marker_once(self):
        """Probe marker once and format the result as a string."""
        try:
            # Import here to avoid circular dependency
            from .WipeJob import WipeJob
            import datetime

            marker = WipeJob.read_marker_buffer(self.device_name)

            with self._lock:
                now = time.time()
                self._state['last_marker_check_time'] = now

                if marker:
                    # Marker found - format it
                    self._state['marker_found_time'] = now
                    formatted = self._format_marker(marker)
                    self._state['marker_formatted'] = formatted
                else:
                    # No marker found
                    self._state['marker_found_time'] = 0
                    self._state['marker_formatted'] = ''

        except Exception as e:
            with self._lock:
                self._state['last_error'] = f"marker: {e}"

    def _format_marker(self, marker):
        """Format marker object as a display string.

        Returns:
            str: Formatted marker like "✓ W 100% Zero 2026/01/23 14:30"
        """
        import datetime

        try:
            now = int(round(time.time()))
            if marker.size_bytes == 0 or marker.unixtime >= now:
                return ''  # Invalid marker

            pct = min(100, int(round((marker.scrubbed_bytes / marker.size_bytes) * 100)))
            state = 'W' if pct >= 100 else 's'
            dt = datetime.datetime.fromtimestamp(marker.unixtime)

            # Build prefix with verify status
            verify_prefix = ''
            verify_status = getattr(marker, 'verify_status', None)
            if verify_status == 'pass':
                verify_prefix = '✓ '
            elif verify_status == 'fail':
                verify_prefix = '✗ '

            # Build suffix with error info
            error_suffix = ''
            abort_reason = getattr(marker, 'abort_reason', None)
            if abort_reason:
                error_suffix = f' Err[{abort_reason}]'

            return f'{verify_prefix}{state} {pct}% {marker.mode} {dt.strftime("%Y/%m/%d %H:%M")}{error_suffix}'
        except (AttributeError, TypeError, ValueError, OverflowError):
            return ''

    def _read_sysfs_attr(self, attr_path):
        """Read a sysfs attribute for this device.

        Note: This CAN block if device is in D state.
        That's why it runs in the worker thread, not main thread.
        """
        path = f"/sys/class/block/{self.device_name}/{attr_path}"
        try:
            with open(path, 'r') as f:
                # Sanitize: some USB bridges return strings with embedded nulls
                return f.read().strip().replace('\x00', '')
        except (FileNotFoundError, IOError, OSError):
            return ''

    def _read_vpd_serial(self):
        """Read serial from VPD page 80 (SCSI).

        WARNING: This can block indefinitely on unresponsive devices.
        """
        path = f"/sys/class/block/{self.device_name}/device/vpd_pg80"
        try:
            with open(path, 'rb') as f:
                data = f.read()
                if len(data) > 4:
                    # Sanitize: remove nulls that some USB bridges include
                    return data[4:].decode('ascii', errors='ignore').strip().replace('\x00', '')
        except (FileNotFoundError, IOError, OSError):
            pass
        return ''

    def _is_usb_device(self):
        """Check if device is connected via USB bus."""
        try:
            sysfs_path = f'/sys/class/block/{self.device_name}'
            real_path = os.path.realpath(sysfs_path).lower()
            return '/usb' in real_path
        except (OSError, IOError):
            return False

    def _is_rotational_device(self):
        """Check if device is rotational (HDD) vs solid-state (SSD).

        Reads /sys/block/<device>/queue/rotational:
        - 1 = HDD (spinning disk)
        - 0 = SSD (solid-state)

        Returns:
            bool: True if HDD (rotational), False if SSD or unknown
        """
        try:
            path = f'/sys/block/{self.device_name}/queue/rotational'
            with open(path, 'r', encoding='utf-8') as f:
                return f.read().strip() == '1'
        except (FileNotFoundError, PermissionError, OSError):
            return False


class DeviceWorkerManager:
    """Manages DeviceWorker instances for all devices.

    Creates workers when devices appear, stops them when devices disappear.
    Provides unified interface for main thread to access device state.
    """

    def __init__(self, checker=None):
        self.checker = checker or DrivePreChecker()
        self._workers = {}  # device_name -> DeviceWorker
        self._lock = threading.Lock()

    def update_devices(self, device_names):
        """Update worker set based on current device list.

        Args:
            device_names: set or list of current device names (e.g., {'sda', 'nvme0n1'})
        """
        device_names = set(device_names)

        with self._lock:
            current = set(self._workers.keys())

            # Stop workers for removed devices
            for name in current - device_names:
                worker = self._workers.pop(name)
                worker.stop()

            # Create workers for new devices
            for name in device_names - current:
                # Create workers for all devices (disks and partitions)
                # Partitions need workers too for marker reading
                worker = DeviceWorker(name, self.checker)
                worker.start()
                self._workers[name] = worker

    def request_hw_caps(self, device_name, force=False):
        """Request hardware capabilities probe for a device."""
        with self._lock:
            worker = self._workers.get(device_name)
            if worker:
                worker.request_hw_caps(force=force)

    def set_want_marker(self, device_name, want):
        """Set whether we want to monitor for a marker on a device.

        Args:
            device_name: Device to monitor
            want: Boolean - True to enable polling, False to disable
        """
        with self._lock:
            worker = self._workers.get(device_name)
            if worker:
                worker.set_want_marker(want)

    def request_marker_check(self, device_name):
        """Request immediate marker check for a device (non-blocking).

        Used to get quick feedback after wipes complete without waiting
        for the polling interval.
        """
        with self._lock:
            worker = self._workers.get(device_name)
            if worker:
                worker.request_marker_check()

    def get_marker_formatted(self, device_name):
        """Get formatted marker string for a device (ready to display).

        Returns:
            str: Formatted marker string, or empty string if no marker
        """
        with self._lock:
            worker = self._workers.get(device_name)
            if worker:
                return worker.get_marker_formatted()
        return ''

    def get_hw_caps(self, device_name):
        """Get hardware capabilities for a device.

        Returns:
            tuple: (hw_caps, hw_caps_summary, hw_nopes, state, is_usb, is_rotational)
        """
        with self._lock:
            worker = self._workers.get(device_name)
            if worker:
                return worker.get_hw_caps()
        return ('', '', '', ProbeState.PENDING, False, False)

    def get_state(self, device_name):
        """Get full cached state for a device."""
        with self._lock:
            worker = self._workers.get(device_name)
            if worker:
                return worker.get_state()
        return None

    def stop_all(self):
        """Stop all workers (call on shutdown)."""
        with self._lock:
            for worker in self._workers.values():
                worker.stop()
            # Wait for all to finish
            for worker in self._workers.values():
                worker.join(timeout=2.0)
            self._workers.clear()

    def has_updates(self, cached_state):
        """Check if any markers or hw_caps have changed since cached_state.

        Args:
            cached_state: dict mapping device_name -> {'marker': str, 'hw_caps_state': ProbeState}

        Returns:
            bool: True if any markers or hw_caps differ from cached_state
        """
        with self._lock:
            for device_name, worker in self._workers.items():
                cached = cached_state.get(device_name, {})
                current_state = worker.get_state()
                # Check if marker changed
                if current_state.get('marker_formatted') != cached.get('marker'):
                    return True
                # Check if hw_caps changed (from PENDING to READY)
                cached_hw_state = cached.get('hw_caps_state', ProbeState.PENDING)
                if current_state.get('hw_caps_state') != cached_hw_state:
                    return True
        return False

    @staticmethod
    def _is_whole_disk(name):
        """Check if device name is a whole disk (not a partition)."""
        # NVMe: nvme0n1 is disk, nvme0n1p1 is partition
        if name.startswith('nvme'):
            return 'p' not in name.split('n')[-1]
        # SATA/IDE: sda is disk, sda1 is partition (partitions end with digits)
        if name.startswith(('sd', 'hd')):
            return not name[-1].isdigit()
        return False

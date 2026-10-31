"""
DeviceChangeMonitor - Background thread for monitoring block device changes
"""
import ctypes
import os
import select
import threading
import traceback
from ctypes.util import find_library

# inotify constants (from Linux headers)
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_MOVED_TO = 0x00000080
IN_MOVED_FROM = 0x00000040

# Flags for inotify_init1
O_NONBLOCK = 0o4000
O_CLOEXEC = 0o2000000

# Try to load libc for inotify syscalls
try:
    libc_name = find_library('c')
    if libc_name is None:
        libc_name = 'libc.so.6'  # Fallback for systems where find_library fails
    libc = ctypes.CDLL(libc_name, use_errno=True)
    _HAVE_LIBC = True
except (OSError, TypeError):
    _HAVE_LIBC = False


class DeviceChangeMonitor:
    """Background monitor that detects block device changes via inotify or polling.

    This class monitors for device hot-plug events without using lsblk (which
    can block on devices undergoing firmware wipe). Device discovery is now
    done directly via DeviceInfo.discover_devices().

    Uses inotify on /sys/class/block for efficient event-driven monitoring.
    Falls back to polling if inotify is unavailable.
    """

    def __init__(self, check_interval=0.2):
        """
        Initialize the device change monitor.

        Args:
            check_interval: How often to check for changes in polling mode (seconds)
        """
        self.check_interval = check_interval
        self._lock = threading.Lock()
        self._thread = None
        self._stop_event = threading.Event()
        self._changes_detected = False
        self.last_fingerprint = None
        # inotify resources
        self._inotify_fd = None
        self._watch_fd = None
        self._use_inotify = False

    def start(self):
        """Start the background monitoring thread"""
        if self._thread is not None and self._thread.is_alive():
            return  # Already running

        # Try to initialize inotify
        self._try_init_inotify()

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def _try_init_inotify(self):
        """Try to initialize inotify for /sys/class/block.

        On failure, falls back to polling mode.
        """
        if not _HAVE_LIBC:
            with open('/tmp/dwipe_inotify_debug.log', 'a', encoding='utf-8') as f:
                f.write("libc not available, falling back to polling\n")
            self._use_inotify = False
            return

        try:
            # Call inotify_init1(O_NONBLOCK | O_CLOEXEC) via ctypes
            libc.inotify_init1.argtypes = [ctypes.c_int]
            libc.inotify_init1.restype = ctypes.c_int
            self._inotify_fd = libc.inotify_init1(O_NONBLOCK | O_CLOEXEC)

            if self._inotify_fd < 0:
                raise OSError("inotify_init1 returned error")

            # Call inotify_add_watch for /sys/class/block
            libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
            libc.inotify_add_watch.restype = ctypes.c_int
            mask = IN_CREATE | IN_DELETE | IN_MOVED_TO | IN_MOVED_FROM
            self._watch_fd = libc.inotify_add_watch(self._inotify_fd, b'/sys/class/block', mask)

            if self._watch_fd < 0:
                raise OSError("inotify_add_watch returned error")

            self._use_inotify = True
        except OSError as e:
            # inotify not available or permission denied - fall back to polling
            with open('/tmp/dwipe_inotify_debug.log', 'a', encoding='utf-8') as f:
                f.write(f"inotify initialization failed: {e}\n")
            self._use_inotify = False
            # Clean up any partially initialized resources
            if self._inotify_fd is not None and self._inotify_fd >= 0:
                try:
                    os.close(self._inotify_fd)
                except OSError:
                    pass
                self._inotify_fd = None
        except Exception as e:  # pylint: disable=broad-exception-caught
            # Unexpected error - log and fall back to polling
            with open('/tmp/dwipe_inotify_debug.log', 'a', encoding='utf-8') as f:
                f.write(f"Unexpected error in inotify init: {e}\n")
                traceback.print_exc(file=f)
            self._use_inotify = False

    def stop(self):
        """Stop the background monitoring thread and clean up resources"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

        # Clean up inotify resources
        if self._inotify_fd is not None and self._inotify_fd >= 0:
            try:
                if self._watch_fd is not None and self._watch_fd >= 0 and _HAVE_LIBC:
                    libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
                    libc.inotify_rm_watch.restype = ctypes.c_int
                    libc.inotify_rm_watch(self._inotify_fd, self._watch_fd)
                os.close(self._inotify_fd)
            except OSError:
                pass
            finally:
                self._inotify_fd = None
                self._watch_fd = None

    def _check_for_changes(self):
        """
        Check if block devices or partitions have changed (non-blocking).

        Returns:
            True if changes detected, False otherwise
        """
        try:
            # 1. Quickest check: Does the list of block devices match?
            # This catches "Forget" (DEL) and "Scan" (!) events immediately.
            current_devs = os.listdir('/sys/class/block')

            # 2. Secondary check: Do the partition sizes/counts match?
            with open('/proc/partitions', encoding='utf-8') as f:
                current_parts = f.read()

            # Create a combined "Fingerprint"
            fingerprint = f"{len(current_devs)}|{current_parts}"

            if fingerprint != self.last_fingerprint:
                self.last_fingerprint = fingerprint
                return True

        except Exception:  # pylint: disable=broad-exception-caught
            # If we can't read /sys or /proc, default to True
            # so we don't get stuck with a blank screen.
            return True
        return False

    def _monitor_loop(self):
        """Background thread loop that monitors for device changes.

        Dispatches to inotify-based or polling-based monitoring.
        """
        if self._use_inotify:
            self._inotify_loop()
        else:
            self._polling_loop()

    def _inotify_loop(self):
        """Monitor for device changes using inotify events.

        Sleeps until kernel wakes us up on /sys/class/block changes.
        """
        while not self._stop_event.is_set():
            try:
                # Use select with timeout to make thread interruptible
                # Timeout allows checking stop event periodically
                readable, _, _ = select.select([self._inotify_fd], [], [], 1.0)

                if readable:
                    # Read and discard inotify events
                    # We just need to know *something* changed, not the details
                    try:
                        os.read(self._inotify_fd, 4096)
                        with self._lock:
                            self._changes_detected = True
                    except (OSError, BlockingIOError):
                        pass

            except (OSError, ValueError) as e:  # pylint: disable=broad-exception-caught
                # If inotify fails, log and fall back to polling
                with open('/tmp/dwipe_inotify_debug.log', 'a', encoding='utf-8') as f:
                    f.write(f"inotify_loop error: {e}, falling back to polling\n")
                self._use_inotify = False
                self._polling_loop()
                return

    def _polling_loop(self):
        """Fallback polling-based monitoring.

        Periodically checks /proc/partitions and /sys/class/block for changes.
        """
        while not self._stop_event.is_set():
            if self._check_for_changes():
                with self._lock:
                    self._changes_detected = True

            # Sleep for the check interval
            self._stop_event.wait(self.check_interval)

    def get_and_clear(self):
        """
        Check if changes were detected since last call.

        Returns:
            True if changes detected, False otherwise
        """
        with self._lock:
            result = self._changes_detected
            self._changes_detected = False
            return result

    def peek(self):
        """
        Check if changes were detected without clearing the flag.

        Returns:
            True if changes detected, False otherwise
        """
        with self._lock:
            return self._changes_detected

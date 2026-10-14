"""
DeviceInfo class for device discovery and information management
"""
# pylint: disable=invalid-name,broad-exception-caught
# pylint: disable=line-too-long,too-many-locals,too-many-branches
# pylint: disable=too-many-return-statements,too-many-nested-blocks
# pylint: disable=too-many-statements

import os
import re
import json
import subprocess
import curses
import traceback
from fnmatch import fnmatch
from types import SimpleNamespace
from console_window import Theme
from dataclasses import asdict

from .Utils import Utils
from .DrivePreChecker import DrivePreChecker
from .DeviceWorker import DeviceWorkerManager, ProbeState


class DeviceInfo:
    """Class to dig out the info we want from the system."""
    disk_majors = set()  # major devices that are disks
    _discovery_cycle = 0  # Counter to prove discovery is running

    # Filesystem types marking an encrypted/stacked container whose contents
    # are hidden behind another layer. These are auto-blocked on first sight.
    CONTAINER_FSTYPES = (
        'crypto_LUKS', 'LVM2_member', 'linux_raid_member',
        'isw_raid_member', 'ddf_raid_member', 'swap',
    )
    CONTAINER_FSTYPE_PREFIXES = ('crypto_', 'DM_')

    @staticmethod
    def is_container_fstype(fstype):
        """True if fstype marks a container (LUKS/LVM/RAID/swap)."""
        if not fstype:
            return False
        if fstype in DeviceInfo.CONTAINER_FSTYPES:
            return True
        return any(fstype.startswith(p) for p in DeviceInfo.CONTAINER_FSTYPE_PREFIXES)

    @staticmethod
    def clean_partition_label(label):
        """Clean up partition labels by decoding escape sequences and simplifying common labels.

        Args:
            label: Raw partition label string (may contain \x20 etc.)

        Returns:
            Cleaned up label string
        """
        if not label:
            return ''

        # Decode escape sequences (like \x20 for space)
        try:
            # Handle common escape sequences
            cleaned = label.encode('utf-8').decode('unicode_escape')
        except (UnicodeDecodeError, AttributeError):
            cleaned = label

        # Simplify common Windows partition labels
        simplifications = {
            'Basic data partition': 'MS-Data',
            'Microsoft reserved partition': 'MS-Reserved',
            'EFI system partition': 'EFI',
            'EFI System Partition': 'EFI',
        }

        cleaned = simplifications.get(cleaned, cleaned)

        return cleaned

    def __init__(self, opts, persistent_state=None, worker_manager=None):
        self.opts = opts
        self.checker = DrivePreChecker()
        self.wids = SimpleNamespace(state=5, name=4, human=7, fstype=4, label=5)
        self.head_str = None
        self.partitions = None
        self.persistent_state = persistent_state
        # Use provided worker_manager or create a new one
        # Reusing allows hw_caps probing to continue across device refreshes
        if worker_manager is not None:
            self.worker_manager = worker_manager
        else:
            self.worker_manager = DeviceWorkerManager(self.checker)

    @staticmethod
    def _make_partition_namespace(major, name, size_bytes, dflt):
        return SimpleNamespace(name=name,       # /proc/partitions
                               major=major,       # /proc/partitions
                               parent=None,     # a partition
                               state=dflt,         # run-time state
                               dflt=dflt,         # default run-time state
                               label='',       # blkid
                               fstype='',      # blkid
                               type='',        # device type (disk, part)
                               model='',       # /sys/class/block/{name}/device/vendor|model
                               size_bytes=size_bytes,  # /sys/block/{name}/...
                               marker='',      # Formatted marker string from worker thread
                               want_marker=False,  # True = ask worker thread to poll for marker
                               mounts=[],        # /proc/mounts
                               minors=[],
                               job=None,         # if zap running
                               uuid='',        # filesystem UUID or PARTUUID
                               serial='',      # disk serial number (for whole disks)
                               port='',        # port (for whole disks)
                               hw_caps='',      # hw_wipe capabilities string: "Ovwr, Block, Crypto*"
                               hw_caps_summary='',  # compact display: "ϟCrypto"
                               hw_nopes='',   # hw_wipe issues string: "Frozen, Locked"
                               hw_caps_state=ProbeState.PENDING,  # probe state for hw_caps
                               is_usb=False,   # True if device is on USB bus
                               is_rotational=False,  # True if HDD (spinning disk)
                               in_use=False,   # mounted/swap or backing an active holder
                               is_container=False,  # LUKS/LVM/RAID/swap container
                               auto_blocked=False,  # blocked automatically (container)
                               )

    def get_hw_capabilities(self, ns):
        """
        Populates and returns hardware wipe capabilities for a disk.
        Non-blocking: requests probe from background worker, returns cached state.

        IMPORTANT: Skips probing if device has an active job to avoid blocking.
        """
        # 1. Check if we already have final results
        if ns.hw_caps_state == ProbeState.READY:
            return ns.hw_caps, ns.hw_nopes

        # 2. Skip probing if device has active job (would block on SATA wipe)
        if ns.job:
            return ns.hw_caps, ns.hw_nopes

        # 3. Request probe from worker (non-blocking)
        self.worker_manager.request_hw_caps(ns.name)

        # 4. Get current state from worker
        hw_caps, hw_caps_summary, hw_nopes, state, is_usb, is_rotational = self.worker_manager.get_hw_caps(ns.name)

        # 5. Update namespace with worker state
        ns.hw_caps = hw_caps
        ns.hw_caps_summary = hw_caps_summary
        ns.hw_nopes = hw_nopes
        ns.hw_caps_state = state
        ns.is_usb = is_usb
        ns.is_rotational = is_rotational

        return ns.hw_caps, ns.hw_nopes

    def _get_port_from_sysfs(self, device_name):
        try:
            sysfs_path = f'/sys/class/block/{device_name}'
            if not os.path.exists(sysfs_path):
                return ''

            real_path = os.path.realpath(sysfs_path).lower()

            # 1. USB - Format: USB:1-1.4
            if '/usb' in real_path:
                usb_match = re.search(r'/(\d+-\d+(?:\.\d+)*):', real_path)
                if usb_match:
                    return f"USB:{usb_match.group(1)}"

            # 2. SATA - Format: SATA:1
            elif '/ata' in real_path:
                ata_match = re.search(r'ata(\d+)', real_path)
                if ata_match:
                    return f"SATA:{ata_match.group(1)}"

            # 3. NVMe - Format: PCI:1b.0 (Stripped of 0000:00: noise)
            elif '/nvme' in real_path:
                # This regex ignores the 4-digit domain and the first 2-digit bus
                pci_match = re.search(r'0000:[0-9a-f]{2}:([0-9a-f]{2}\.[0-9a-f])', real_path)
                if pci_match:
                    return f"PCI:{pci_match.group(1)}"
                return "NVMe"

            # 4. MMC/eMMC - Format: MMC:0 or PCI:1a.0 (if PCI-attached)
            elif '/mmc' in real_path:
                # Try to extract mmc host number
                mmc_match = re.search(r'/mmc_host/mmc(\d+)', real_path)
                if mmc_match:
                    return f"MMC:{mmc_match.group(1)}"
                # Fallback: try to get PCI address if available
                pci_match = re.search(r'0000:[0-9a-f]{2}:([0-9a-f]{2}\.[0-9a-f])', real_path)
                if pci_match:
                    return f"PCI:{pci_match.group(1)}"
                return "MMC"

        except Exception as e:
            # Log exception to file for debugging
            with open('/tmp/dwipe_port_debug.log', 'a', encoding='utf-8') as f:
                f.write(f"Exception in _get_port_from_sysfs({device_name}): {e}\n")
                traceback.print_exc(file=f)
        return ''

    @staticmethod
    def _get_device_vendor_model(device_name):
        """Gets the vendor and model for a given device from the /sys/class/block directory.
        - Args: - device_name: The device name, such as 'sda', 'sdb', etc.
        - Returns: A string containing the vendor and model information.
        """
        def get_str(device_name, suffix):
            try:
                rv = ''
                fullpath = f'/sys/class/block/{device_name}/device/{suffix}'
                with open(fullpath, 'r', encoding='utf-8') as f:  # Read information
                    # Sanitize: some USB bridges return strings with embedded nulls
                    rv = f.read().strip().replace('\x00', '')
            except (FileNotFoundError, Exception):
                pass
            return rv

        rv = f'{get_str(device_name, "model")}'
        return rv.strip()

    @staticmethod
    def _is_rotational_device(device_name):
        """Check if device is rotational (HDD) vs solid-state (SSD).

        Reads /sys/block/<device>/queue/rotational:
        - 1 = HDD (spinning disk)
        - 0 = SSD (solid-state)

        Returns:
            bool: True if HDD (rotational), False if SSD or unknown
        """
        try:
            # For partitions, get the parent disk name
            parent = device_name.rstrip('0123456789')
            if parent.endswith('p') and parent[:-1].rstrip('0123456789'):
                # NVMe style: nvme0n1p1 -> nvme0n1
                parent = parent[:-1]
            if not parent:
                parent = device_name

            path = f'/sys/block/{parent}/queue/rotational'
            with open(path, 'r', encoding='utf-8') as f:
                return f.read().strip() == '1'
        except (FileNotFoundError, PermissionError, OSError):
            return False

    # ========================================================================
    # Non-blocking device discovery helpers (replacement for lsblk)
    # ========================================================================

    def _parse_proc_partitions(self):
        """Parse /proc/partitions for basic device list (non-blocking).

        Format: major minor #blocks name
                8     0 1048576 sda
                8     1  524288 sda1

        Returns:
            dict: {name: SimpleNamespace(major, minor, blocks, name)}
        """
        devices = {}
        try:
            with open('/proc/partitions', 'r', encoding='utf-8') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) == 4 and parts[0].isdigit():
                        major, minor, blocks, name = parts
                        devices[name] = SimpleNamespace(
                            major=int(major),
                            minor=int(minor),
                            blocks=int(blocks),  # In KB
                            name=name
                        )
        except (FileNotFoundError, PermissionError, Exception):
            pass
        return devices

    @staticmethod
    def _block_name_from_devpath(devpath):
        """Resolve a /dev path to its canonical kernel block name.

        Handles /dev/sda1, /dev/md0, /dev/dm-0 and indirection through
        /dev/mapper/<name> or /dev/disk/by-* (resolved via realpath).
        Returns '' for anything that is not a block device path.
        """
        if not devpath.startswith('/dev/'):
            return ''
        try:
            real = os.path.realpath(devpath)
        except OSError:
            real = devpath
        return os.path.basename(real)

    @staticmethod
    def _read_sysfs_dir(path):
        """List a sysfs directory, returning [] if it is missing or unreadable."""
        try:
            return os.listdir(path)
        except OSError:
            return []

    def _parse_proc_mounts(self):
        """Parse /proc/mounts for mount points (non-blocking).

        Format: device mountpoint fstype options dump pass
                /dev/sda1 /boot ext4 rw,relatime 0 0

        Device paths are resolved to canonical block names so a mount on a
        dm-crypt/LVM/RAID mapping (e.g. /dev/mapper/crypt -> dm-0) is
        attributed to the underlying block device.

        Returns:
            dict: {device_name: {'mounts': [mountpoint1, ...], 'fstype': str}}
        """
        mounts = {}
        try:
            with open('/proc/mounts', 'r', encoding='utf-8') as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3 and parts[0].startswith('/dev/'):
                        device = self._block_name_from_devpath(parts[0])
                        if not device:
                            continue
                        mountpoint = parts[1]
                        fstype = parts[2]
                        if device not in mounts:
                            mounts[device] = {'mounts': [], 'fstype': fstype}
                        mounts[device]['mounts'].append(mountpoint)
        except (FileNotFoundError, PermissionError, Exception):
            pass
        return mounts

    @staticmethod
    def _parse_proc_swaps():
        """Return canonical block names currently used as swap (non-blocking)."""
        names = set()
        try:
            with open('/proc/swaps', 'r', encoding='utf-8') as f:
                next(f, None)  # skip header line
                for line in f:
                    parts = line.split()
                    if parts:
                        name = DeviceInfo._block_name_from_devpath(parts[0])
                        if name:
                            names.add(name)
        except (FileNotFoundError, PermissionError, Exception):
            pass
        return names

    def _mark_block_usage(self, entries, swap_names):
        """Flag devices consumed by a holder (LUKS, LVM, RAID, ...).

        A device is *in use* when it is mounted, used as swap, or backs any
        holder (an active dm/md mapping). Mountpoints of a mounted holder are
        propagated down its slave chain so a LUKS container shows the mount of
        its unlocked mapping; a holder that is open but unmounted marks the
        backing device in-use (rendered as '\\busy'). All reads are sysfs-only
        and non-blocking.
        """
        holders, slaves = {}, {}
        for name in entries:
            holders[name] = self._read_sysfs_dir(f'/sys/class/block/{name}/holders')
            slaves[name] = self._read_sysfs_dir(f'/sys/class/block/{name}/slaves')

        # Seed the walk with devices that are mounted or used as swap
        queue = [n for n, ns in entries.items() if ns.mounts]
        for name in swap_names:
            if name in entries:
                entries[name].in_use = True
                if name not in queue:
                    queue.append(name)
        visited = set(queue)

        # Propagate mount visibility down to backing devices via slaves
        while queue:
            cur = queue.pop()
            cur_mounts = entries[cur].mounts if cur in entries else []
            for backing in slaves.get(cur, []):
                if backing not in entries:
                    continue
                bns = entries[backing]
                for mount in cur_mounts:
                    if mount not in bns.mounts:
                        bns.mounts.append(mount)
                bns.in_use = True
                if backing not in visited:
                    visited.add(backing)
                    queue.append(backing)

        # Any device with a holder is in use (mapping active) even if unmounted.
        # Also classify containers (by fstype or by an existing holder).
        for name, holder_list in holders.items():
            if name not in entries:
                continue
            ns = entries[name]
            if holder_list:
                ns.in_use = True
            if holder_list or self.is_container_fstype(ns.fstype):
                ns.is_container = True

    @staticmethod
    def device_in_use(name):
        """Best-effort, non-blocking check that a device is in use right now.

        Guards a wipe at the moment it starts, closing the window between a
        discovery refresh and the actual write. True if the device has any
        holder (active dm/md mapping) or is currently mounted.
        """
        if DeviceInfo._read_sysfs_dir(f'/sys/class/block/{name}/holders'):
            return True
        try:
            with open('/proc/mounts', 'r', encoding='utf-8') as f:
                for line in f:
                    parts = line.split()
                    if parts and DeviceInfo._block_name_from_devpath(parts[0]) == name:
                        return True
        except (FileNotFoundError, PermissionError, Exception):
            pass
        return False

    def _is_partition(self, name):
        """Check if device is a partition (non-blocking).

        Returns True if /sys/class/block/{name}/partition exists.
        """
        return os.path.exists(f'/sys/class/block/{name}/partition')

    def _get_parent_from_sysfs(self, name):
        """Find parent disk for a partition from sysfs (non-blocking).

        For sda1, follows /sys/class/block/sda1 symlink and extracts parent.
        Returns None for whole disks.
        """
        if not self._is_partition(name):
            return None

        try:
            # The sysfs symlink for a partition points to a path containing the parent
            # e.g., /sys/class/block/sda1 -> ../../devices/.../sda/sda1
            real_path = os.path.realpath(f'/sys/class/block/{name}')
            parent_path = os.path.dirname(real_path)
            parent_name = os.path.basename(parent_path)

            # Verify it's actually a disk (not some intermediate directory)
            if os.path.exists(f'/sys/class/block/{parent_name}'):
                return parent_name
        except (OSError, Exception):
            pass

        # Fallback: strip numeric suffix (sda1 -> sda, nvme0n1p1 -> nvme0n1)
        if name.startswith('nvme') and 'p' in name:
            return name.rsplit('p', 1)[0]
        elif name.startswith('mmcblk') and 'p' in name:
            return name.rsplit('p', 1)[0]
        else:
            # SATA/USB: strip trailing digits
            return name.rstrip('0123456789') or None

    def _get_size_from_sysfs(self, name):
        """Get device size in bytes from sysfs (non-blocking).

        Reads /sys/class/block/{name}/size which contains sector count.
        """
        try:
            with open(f'/sys/class/block/{name}/size', 'r', encoding='utf-8') as f:
                sectors = int(f.read().strip())
            return sectors * 512  # Convert to bytes
        except (FileNotFoundError, ValueError, Exception):
            return 0

    def _get_serial_from_sysfs(self, name):
        """Get device serial number from sysfs (non-blocking).

        NVMe: /sys/class/block/{name}/device/serial (plain text)
        SATA: /sys/class/block/{name}/device/vpd_pg80 (binary VPD page)
        """
        # Try NVMe-style path first (plain text)
        try:
            with open(f'/sys/class/block/{name}/device/serial', 'r', encoding='utf-8') as f:
                # Sanitize: some USB bridges return strings with embedded nulls
                return f.read().strip().replace('\x00', '')
        except (FileNotFoundError, Exception):
            pass

        # Try SATA VPD page 80 (binary format)
        try:
            with open(f'/sys/class/block/{name}/device/vpd_pg80', 'rb') as f:
                data = f.read()
                if len(data) > 4:
                    # VPD format: 00 80 00 LL [serial...] where LL is length
                    length = data[3] if len(data) > 3 else 0
                    # Sanitize: remove nulls that some USB bridges include
                    serial = data[4:4+length].decode('ascii', errors='ignore').strip().replace('\x00', '')
                    return serial
        except (FileNotFoundError, Exception):
            pass

        return ''

    def _probe_blkid(self, device_name, timeout=2.0):
        """Get fstype, label, uuid from udev cache (instant, no subprocess).

        Reads from /run/udev/data/b<major>:<minor> which is populated by udevd
        at boot time. Falls back to empty values if cache is unavailable.

        Args:
            device_name: Device name (e.g., 'sda1')
            timeout: Unused, kept for API compatibility

        Returns:
            dict with keys: fstype, label, uuid (empty strings if not found)
        """
        result = {'fstype': '', 'label': '', 'uuid': ''}
        try:
            # Get major:minor from sysfs
            with open(f'/sys/class/block/{device_name}/dev') as f:
                major_minor = f.read().strip()

            # Read udev data file
            with open(f'/run/udev/data/b{major_minor}') as f:
                for line in f:
                    if line.startswith('E:ID_FS_TYPE='):
                        result['fstype'] = line.split('=', 1)[1].strip()
                    elif line.startswith('E:ID_FS_LABEL='):
                        result['label'] = line.split('=', 1)[1].strip()
                    elif line.startswith('E:ID_PART_ENTRY_NAME=') and not result['label']:
                        raw_label = line.split('=', 1)[1].strip()
                        result['label'] = self.clean_partition_label(raw_label)
                    elif line.startswith('E:ID_FS_UUID='):
                        result['uuid'] = line.split('=', 1)[1].strip()
                    elif line.startswith('E:ID_PART_ENTRY_UUID=') and not result['uuid']:
                        result['uuid'] = line.split('=', 1)[1].strip()
        except (FileNotFoundError, IOError, OSError):
            pass
        return result

    def _build_dark_device_set(self, prev_nss):
        """Build set of device names that should not be probed (have active jobs).

        A device is "dark" if:
        - It has an active job (ns.job is not None)
        - Its parent disk has an active job
        - Any of its child partitions has an active job

        Args:
            prev_nss: Previous device namespaces

        Returns:
            set: Device names to skip probing
        """
        dark = set()
        if not prev_nss:
            return dark

        for name, ns in prev_nss.items():
            if ns.job:
                dark.add(name)
                # Also mark parent as dark
                if ns.parent:
                    dark.add(ns.parent)
                # Also mark all children as dark
                for minor in getattr(ns, 'minors', []):
                    dark.add(minor)

        return dark

    def discover_devices(self, dflt, prev_nss=None):
        """Discover devices via /proc and /sys (non-blocking replacement for lsblk).

        This method replaces parse_lsblk() to avoid blocking on devices with
        active firmware wipe jobs. All /proc and /sys reads are non-blocking.
        Only blkid (for fstype/label/uuid) may briefly block, and it's skipped
        for "dark" devices (those with active jobs).

        Args:
            dflt: Default state for new devices
            prev_nss: Previous device namespaces for merging

        Returns:
            dict: Mapping of device name to SimpleNamespace
        """
        # Build dark device set from previous state
        dark_devices = self._build_dark_device_set(prev_nss)

        # Increment discovery cycle counter (proves code is running)
        DeviceInfo._discovery_cycle += 1

        # Phase 1: Parse /proc for basic device list and mounts
        proc_devices = self._parse_proc_partitions()
        if not proc_devices:
            return {}  # Critical failure - let caller handle

        mounts = self._parse_proc_mounts()

        # Phase 2: Build device entries from sysfs
        entries = {}
        parent_map = {}  # name -> parent_name for building minors lists later

        for name, proc_info in proc_devices.items():
            # Get size from sysfs (more accurate than /proc/partitions blocks)
            size_bytes = self._get_size_from_sysfs(name)
            if size_bytes == 0:
                size_bytes = proc_info.blocks * 1024  # Fallback to /proc/partitions

            entry = self._make_partition_namespace(proc_info.major, name, size_bytes, dflt)
            mount_info = mounts.get(name, {})
            entry.mounts = mount_info.get('mounts', [])
            if entry.mounts:
                entry.fstype = mount_info.get('fstype', '')

            # Determine if partition or disk
            if self._is_partition(name):
                entry.type = 'part'
                parent_name = self._get_parent_from_sysfs(name)
                if parent_name:
                    entry.parent = parent_name
                    parent_map[name] = parent_name
            else:
                entry.type = 'disk'

            # Set mount state
            if entry.mounts:
                entry.state = 'Mnt'

            # Check if this is a dark device
            is_dark = name in dark_devices

            # Phase 3: Conditional probing (skip for dark devices)
            if is_dark and prev_nss and name in prev_nss:
                # Carry forward all data from previous state
                prev = prev_nss[name]
                entry.fstype = prev.fstype
                entry.label = prev.label
                entry.uuid = prev.uuid
                entry.serial = prev.serial
                entry.marker = prev.marker
                entry.hw_caps = getattr(prev, 'hw_caps', '')
                entry.hw_caps_summary = getattr(prev, 'hw_caps_summary', '')
                entry.hw_nopes = getattr(prev, 'hw_nopes', '')
                entry.hw_caps_state = getattr(prev, 'hw_caps_state', ProbeState.PENDING)
                entry.model = getattr(prev, 'model', '')
                entry.port = getattr(prev, 'port', '')
                entry.is_rotational = getattr(prev, 'is_rotational', False)
                entry.in_use = getattr(prev, 'in_use', False)
                entry.is_container = getattr(prev, 'is_container', False)
            else:
                # Non-dark device: probe for metadata
                if entry.type == 'disk':
                    # Disk-level info from sysfs
                    entry.serial = self._get_serial_from_sysfs(name)
                    entry.model = self._get_device_vendor_model(name)
                    entry.port = self._get_port_from_sysfs(name)
                    entry.is_rotational = self._is_rotational_device(name)

                # Get fstype/label/uuid via blkid (skip only if dark device)
                if not is_dark:
                    blkid_info = self._probe_blkid(name)
                    # If not mounted, use blkid fstype; if mounted, use mount fstype
                    if not entry.mounts:
                        entry.fstype = blkid_info['fstype']
                    # Always get label and uuid from blkid
                    entry.label = blkid_info['label']
                    entry.uuid = blkid_info['uuid']

            # Marker monitoring: ALWAYS update, even for dark devices
            # This ensures markers are detected immediately after wipes
            has_job = is_dark  # Already checked above
            has_filesystem = entry.fstype or entry.label

            # Check if device has actual partitions even before we build minors list
            has_child_partitions = any(parent_name == name for parent_name in parent_map.values())

            # Marker state is now entirely managed by worker thread
            # (no marker_checked, monitor_marker, etc. fields anymore)

            # Determine if we should ask worker to monitor for marker
            # For whole disks: no job, no children, not mounted
            # For partitions: no job, no filesystem, not mounted
            if entry.type == 'disk':
                should_want_marker = (not has_job and not has_child_partitions and
                                     not entry.mounts)
            elif entry.type == 'part':
                should_want_marker = (not has_job and not has_filesystem and
                                     not entry.mounts)
            else:
                should_want_marker = False

            # Tell worker to monitor or stop monitoring
            if self.worker_manager:
                self.worker_manager.set_want_marker(name, should_want_marker)
                # Request immediate marker check for faster feedback
                if should_want_marker:
                    self.worker_manager.request_marker_check(name)

            # Get the formatted marker string from worker (if any)
            entry.want_marker = should_want_marker
            if should_want_marker:
                entry.marker = self.worker_manager.get_marker_formatted(name)
                # Extract state from marker string format: "{prefix}{state} {pct}%"
                # State is 'W' (wiped) or 's' (scrubbing), possibly with prefix (✓ or ✗)
                if entry.marker:
                    if 'W ' in entry.marker:
                        entry.state = 'W'
                        entry.dflt = 'W'
                    elif 's ' in entry.marker:
                        entry.state = 's'
                        entry.dflt = 's'
                else:
                    # No marker found, reset state to default (not wiped/scrubbing)
                    entry.state = '-'
                    entry.dflt = '-'
            else:
                entry.marker = ''
                # Not monitoring, reset to default state
                entry.state = '-'
                entry.dflt = '-'

            entries[name] = entry

        # Phase 3c: Mark devices consumed by a holder (LUKS/LVM/RAID/swap)
        swap_names = self._parse_proc_swaps()
        self._mark_block_usage(entries, swap_names)

        # Phase 4: Build parent-child relationships (minors lists)
        for name, parent_name in parent_map.items():
            if parent_name in entries:
                entries[parent_name].minors.append(name)
                self.disk_majors.add(entries[name].major)
                # Propagate mount/in-use state to parent (holder-backed too)
                if entries[name].mounts or getattr(entries[name], 'in_use', False):
                    entries[parent_name].state = 'iMnt'

        # Phase 4b: Clear marker display and state for disks with actual partitions
        # If a disk has partitions, the partitions take precedence over the wipe marker
        for name, entry in entries.items():
            if entry.minors:  # Disk has child partitions
                entry.marker = ''  # Don't show marker when partitions exist
                # Also clear the state if it was set to a wipe state ('W' or 's')
                # Reset to default state '-' (unmarked)
                if entry.state in ('W', 's'):
                    entry.state = '-'
                if entry.dflt in ('W', 's'):
                    entry.dflt = '-'

        # Phase 5: Handle superfloppy case and clean disk rows
        final_entries = {}
        for name, entry in entries.items():
            final_entries[name] = entry

            # Only process top-level disks
            if entry.parent is None and entry.type == 'disk':
                # Superfloppy: disk with filesystem but no partitions
                if not entry.minors and (entry.fstype or entry.label or entry.mounts):
                    v_key = f"{name}_data"
                    v_child = self._make_partition_namespace(entry.major, name, entry.size_bytes, dflt)
                    v_child.name = "----"
                    v_child.type = 'part'
                    v_child.fstype = entry.fstype
                    v_child.label = entry.label
                    v_child.mounts = entry.mounts
                    v_child.parent = name
                    v_child.uuid = entry.uuid
                    v_child.in_use = getattr(entry, 'in_use', False)
                    v_child.is_container = getattr(entry, 'is_container', False)

                    final_entries[v_key] = v_child
                    entry.minors.append(v_key)

                # Clean the disk row: show model instead of fstype
                entry.fstype = entry.model if entry.model else 'DISK'
                entry.label = ''
                entry.mounts = []

        return final_entries


    @staticmethod
    def set_one_state(nss, ns, to=None, test_to=None):
        """Optionally, update a state, and always set inferred states"""
        ready_states = ('s', 'W', '-', '^')
        job_states = ('*%', 'STOP')

        def state_in(to, states):
            return to in states or fnmatch(to, states[0])

        to = test_to if test_to else to

        parent, minors = None, []
        if ns.parent:
            parent = nss.get(ns.parent)
        for minor in ns.minors:
            minor_ns = nss.get(minor, None)
            if minor_ns:
                minors.append(minor_ns)

        if to == 'STOP' and not state_in(ns.state, job_states):
            return False
        if to == 'Blk' and not state_in(ns.state, list(ready_states) + ['Mnt', 'iMnt', 'iBlk']):
            return False
        if to == 'Unbl' and ns.state != 'Blk':
            return False

        if to and fnmatch(to, '*%'):
            if not state_in(ns.state, ready_states):
                return False
            for minor in minors:
                if not state_in(minor.state, ready_states):
                    return False
        elif to in ('s', 'W') and not state_in(ns.state, job_states):
            return False
        if test_to:
            return True

        if to is not None:
            ns.state = to

        # Here we set inferences that block starting jobs
        #  -- clearing these states will be done on the device refresh
        # Propagate Mnt/iMnt from child to parent as iMnt (inherited mount)
        if parent and ns.state in ('Mnt', 'iMnt'):
            if parent.state not in ('Blk', 'iBlk', 'Mnt'):
                parent.state = 'iMnt'
        # Propagate Blk/iBlk from child to parent as iBlk (inherited block)
        if parent and ns.state in ('Blk', 'iBlk'):
            if parent.state != 'Blk':  # Direct Blk trumps inherited
                parent.state = 'iBlk'
        if state_in(ns.state, job_states):
            if parent:
                parent.state = 'Busy'
            for minor in minors:
                minor.state = 'Busy'
        return True

    @staticmethod
    def clear_inferred_states(nss):
        """Clear all inferred states so they can be re-inferred.

        Inherited states (iBlk, iMnt, Busy) are cleared.
        Direct states (Blk, Mnt) are preserved - they're set based on
        persistent state or actual mounts, not inheritance.
        """
        inferred_states = ('Busy', 'iMnt', 'iBlk')
        for ns in nss.values():
            if ns.state in inferred_states:
                ns.state = ns.dflt

    @staticmethod
    def set_all_states(nss):
        """Set every state per linkage inferences"""
        for ns in nss.values():
            DeviceInfo.set_one_state(nss, ns)

    def get_disk_partitions(self, nss):
        """Filter to only wipeable physical storage using positive criteria.

        Keeps devices that:
        - Are type 'disk' or 'part' (from lsblk)
        - Are writable (not read-only)
        - Are real block devices (not virtual)

        This automatically excludes:
        - Virtual devices (zram, loop, dm-*, etc.)
        - Read-only devices (CD-ROMs, eMMC boot partitions)
        - Special partitions (boot loaders)
        """
        ok_nss = {}
        for name, ns in nss.items():
            # Must be disk or partition type
            if ns.type not in ('disk', 'part'):
                continue
            if ns.size_bytes <= 0: # not relevant to wiping
                continue

            # Must be writable (excludes CD-ROMs, eMMC boot partitions, etc.)
            ro_path = f'/sys/class/block/{name}/ro'
            try:
                with open(ro_path, 'r', encoding='utf-8') as f:
                    if f.read().strip() != '0':
                        continue  # Skip read-only devices
            except (FileNotFoundError, Exception):
                # If we can't read ro flag, skip this device to be safe
                continue

            # Exclude common virtual and optical device prefixes as a safety net
            # (most should already be filtered by ro check or missing sysfs)
            excluded_prefixes = ('zram', 'loop', 'dm-', 'md', 'ram', 'sr', 'scd')
            if any(name.startswith(prefix) for prefix in excluded_prefixes):
                continue

            # Include this device
            ok_nss[name] = ns

        return ok_nss

    def compute_field_widths(self, nss):
        """Compute field widths for display formatting"""
        wids = self.wids
        for ns in nss.values():
            wids.state = max(wids.state, len(ns.state))
            wids.name = max(wids.name, len(ns.name) + 2)
            wids.label = max(wids.label, len(ns.label or ''))
            wids.fstype = max(wids.fstype, len(ns.fstype or ''))
        self.head_str = self.get_head_str()

    def get_head_str(self):
        """Generate header string for device list"""
        sep = '  '
        wids = self.wids
        emit = f'{"STATE":_^{wids.state}}'
        emit += f'{sep}{"NAME":_^{wids.name}}'
        emit += f'{sep}{"SIZE":_^{wids.human}}'
        emit += f'{sep}{"TYPE":_^{wids.fstype}}'
        emit += f'{sep}{"LABEL":_^{wids.label}}'
        emit += f'{sep}MOUNTS/STATUS'
        return emit

    def get_pick_range(self):
        """Calculate column range for pick highlighting (NAME through LABEL fields)"""
        sep = '  '
        wids = self.wids
        # Start just before NAME field
        start_col = wids.state + len(sep)
        # End after LABEL field (always spans through LABEL for disks)
        end_col = wids.state + len(sep) + wids.name + len(sep) + wids.human + len(sep) + wids.fstype # + len(sep) + wids.label
        return [start_col, end_col]

    def part_str(self, partition, is_last_child=False):
        """Convert partition to human value.

        Args:
            partition: Partition namespace
            is_last_child: If True and partition has parent, use └ instead of │

        Returns:
            tuple: (text, attr) where attr is curses attribute or None
        """
        def print_str_or_dash(name, width, empty='-'):
            if not name.strip():
                name = empty
            return f'{name:^{width}}'

        sep = '  '
        ns = partition  # shorthand
        wids = self.wids
        emit = f'{ns.state:^{wids.state}}'

        # Determine tree prefix character
        if ns.parent is None:
            # Physical disk: box symbol
            prefix = '■ '
        elif is_last_child:
            # Last partition of disk: rounded corner
            prefix = '└ '
        else:
            # Non-last partition: tee junction
            prefix = '├ '

        name_str = prefix + ns.name

        emit += f'{sep}{name_str:<{wids.name}}'
        emit += f'{sep}{Utils.human(ns.size_bytes):>{wids.human}}'
        emit += sep + print_str_or_dash(ns.fstype, wids.fstype)
        if ns.parent is None:
            # Physical disk - show firmware capability/status centered in LABEL field
            hw_caps = getattr(ns, 'hw_caps', '')
            hw_nopes = getattr(ns, 'hw_nopes', '')
            hw_summary = getattr(ns, 'hw_caps_summary', '')
            hw_state = getattr(ns, 'hw_caps_state', ProbeState.PENDING)
            is_usb = getattr(ns, 'is_usb', False)

            # Non-wipeable devices always show '---' regardless of capabilities
            fw_label = ''
            if ns.state in ('Mnt', 'iMnt', 'Blk', 'iBlk', 'Busy'):
                fw_label = '---'
            elif hw_caps:
                fw_label = hw_summary
            elif hw_nopes and not is_usb:
                first_issue = hw_nopes.split(',')[0].strip()
                fw_label = f'✗{first_issue}'
            elif hw_state in (ProbeState.PENDING, ProbeState.PROBING) and not is_usb:
                fw_label = '...'

            if not fw_label:
                fw_label = '---'

            # Split fw_label into symbol prefix and alphanumeric text for underline rendering
            fw_symbol = ''
            fw_text = fw_label
            if fw_label not in ('---', '...'):
                for i, ch in enumerate(fw_label):
                    if ch.isascii() and ch.isalnum():
                        fw_symbol = fw_label[:i]
                        fw_text = fw_label[i:]
                        break

            # Record positions so caller can underline just the alphanumeric text
            label_start = len(emit) + len(sep)
            centered = f'{fw_label:^{wids.label}}'
            emit += sep + centered

            if fw_symbol:
                left_pad = (wids.label - len(fw_label)) // 2
                ul_start = label_start + left_pad + len(fw_symbol)
                ul_end = ul_start + len(fw_text)
                ns._fw_underline = (ul_start, ul_end)
            else:
                ns._fw_underline = None

            # Check for aggregated mounts (from hidden children when disk is blocked)
            agg_mounts = getattr(ns, 'aggregated_mounts', None)
            if agg_mounts:
                # Show first mount + count of others (e.g., "/ + 5 more")
                first_mount = agg_mounts[0]
                remaining = len(agg_mounts) - 1
                if remaining > 0:
                    emit += f'{sep}{first_mount} + {remaining} more'
                else:
                    emit += f'{sep}{first_mount}'
            elif ns.mounts:
                # Disk has mounts - show them
                emit += f'{sep}{",".join(ns.mounts)}'
            elif getattr(ns, 'in_use', False):
                # In use but not mounted (open LUKS/LVM/RAID mapping)
                emit += f'{sep}\\busy'
            elif getattr(ns, 'auto_blocked', False):
                # Auto-blocked container (LUKS/LVM/RAID/swap)
                emit += f'{sep}auto-blocked'
            elif ns.marker and ns.marker.strip() and not ns.minors:
                # Disk has wipe status - show it ONLY if no child partitions
                # (partitions take precedence over marker)
                emit += f'{sep}{ns.marker}'
            else:
                # No wipe status
                emit += f'{sep}     ---'
        else:
            # Partition: show label and mount/status info
            emit += sep + print_str_or_dash(ns.label, wids.label)
            if ns.mounts:
                emit += f'{sep}{",".join(ns.mounts)}'
            elif getattr(ns, 'in_use', False):
                # In use but not mounted (open LUKS/LVM/RAID mapping)
                emit += f'{sep}\\busy'
            elif getattr(ns, 'auto_blocked', False):
                # Auto-blocked container (LUKS/LVM/RAID/swap)
                emit += f'{sep}auto-blocked'
            else:
                emit += f'{sep}{ns.marker}'

        # Determine color attribute based on state
        attr = None
        # Check for newly inserted flag first (hot-swapped devices should always show orange)
        if getattr(ns, 'newly_inserted', False):
            # Newly inserted device - orange/bright
            if ns.state in ('Mnt', 'iMnt', 'Blk', 'iBlk'):
                # Dim the orange for mounted/blocked devices
                attr = curses.color_pair(Theme.HOTSWAP) | curses.A_DIM
            else:
                attr = curses.color_pair(Theme.HOTSWAP) | curses.A_BOLD
        elif ns.state == 's':
            # Yellow/warning color for stopped/partial wipes (with bold for visibility)
            attr = curses.color_pair(Theme.WARNING) | curses.A_BOLD
        elif ns.state == 'W' and getattr(ns, 'wiped_this_session', False):
            # Green/success color for completed wipes (done in THIS session only) - bold and bright
            attr = curses.color_pair(Theme.SUCCESS) | curses.A_BOLD
        elif ns.state == 'W':
            # Green/success color for completed wipes before this session
            attr = curses.color_pair(Theme.OLD_SUCCESS) | curses.A_BOLD
        elif ns.state.endswith('%') and ns.state not in ('100%',):
            # Active wipe in progress - bright cyan/blue with bold
            attr = curses.color_pair(Theme.INFO) | curses.A_BOLD
        elif ns.state == '^':
            # Newly inserted device (hot-swapped) - orange/bright
            attr = curses.color_pair(Theme.HOTSWAP) | curses.A_BOLD
        elif ns.state in ('Mnt', 'iMnt', 'Blk', 'iBlk'):
            # Dim mounted or blocked devices
            attr = curses.A_DIM

        # Override with red/danger color if verify failed
        if hasattr(ns, 'verify_failed_msg') and ns.verify_failed_msg:
            attr = curses.color_pair(Theme.DANGER) | curses.A_BOLD

        # Make disk lines bold
        if ns.parent is None:
            if attr is None:
                attr = curses.A_BOLD
            else:
                attr |= curses.A_BOLD

        return emit, attr

    def merge_dev_infos(self, nss, prev_nss=None):
        """Merge old DevInfos into new DevInfos"""
        if not prev_nss:
            return nss

        # Track which devices were physically present in last scan
        prev_physical = set()
        for name, prev_ns in prev_nss.items():
            # Only count as "physically present" if not carried forward due to job
            if not (hasattr(prev_ns, 'was_unplugged') and prev_ns.was_unplugged):
                prev_physical.add(name)

        for name, prev_ns in prev_nss.items():
            # merge old jobs forward
            new_ns = nss.get(name, None)
            if new_ns:
                if prev_ns.job:
                    new_ns.job = prev_ns.job
                # Note: Do NOT preserve port - use fresh value from current scan
                new_ns.dflt = prev_ns.dflt
                # Preserve the "wiped this session" flag
                if hasattr(prev_ns, 'wiped_this_session'):
                    new_ns.wiped_this_session = prev_ns.wiped_this_session
                # Marker is now handled entirely by worker thread, no preservation needed

                # Preserve hw_caps state - once probed, it's permanent for the device
                prev_hw_state = getattr(prev_ns, 'hw_caps_state', ProbeState.PENDING)
                if prev_hw_state == ProbeState.READY:
                    new_ns.hw_caps = getattr(prev_ns, 'hw_caps', '')
                    new_ns.hw_caps_summary = getattr(prev_ns, 'hw_caps_summary', '')
                    new_ns.hw_nopes = getattr(prev_ns, 'hw_nopes', '')
                    new_ns.hw_caps_state = ProbeState.READY

                # Preserve verify failure message ONLY for unmarked disks
                # Clear if: filesystem appeared OR partition now has a marker
                if hasattr(prev_ns, 'verify_failed_msg'):
                    # Check if partition now has marker (dflt is 'W' or 's', not '-')
                    has_marker = new_ns.dflt in ('W', 's')

                    # For whole disks (no parent): check if any child partition has filesystem
                    # For partitions: check if this partition has filesystem
                    has_filesystem = False
                    if not new_ns.parent:
                        # Whole disk - check if any child has fstype or label
                        for _, child_ns in nss.items():
                            if child_ns.parent == name and (child_ns.fstype or child_ns.label):
                                has_filesystem = True
                                break
                    else:
                        # Partition - check if it has fstype or label
                        has_filesystem = bool(new_ns.fstype or new_ns.label)

                    if has_filesystem or has_marker:
                        # Filesystem appeared or now has marker - clear the error
                        # (verify_failed_msg is only for unmarked disks)
                        if hasattr(new_ns, 'verify_failed_msg'):
                            delattr(new_ns, 'verify_failed_msg')
                    else:
                        # Still unmarked with no filesystem - persist the error
                        new_ns.verify_failed_msg = prev_ns.verify_failed_msg
                        new_ns.mounts = [prev_ns.verify_failed_msg]

                if prev_ns.state == 'Blk':
                    new_ns.state = 'Blk'
                elif new_ns.state not in ('s', 'W'):
                    new_ns.state = new_ns.dflt
                    # Don't copy forward percentage states or inherited states - only persistent states
                    if prev_ns.state not in ('s', 'W', 'Busy', 'Unbl', 'iBlk', 'iMnt') and not prev_ns.state.endswith('%'):
                        new_ns.state = prev_ns.state  # re-infer these
            elif prev_ns.job:
                # unplugged device with job..
                prev_ns.was_unplugged = True  # Mark as unplugged
                nss[name] = prev_ns  # carry forward
                prev_ns.job.do_abort = True

        # Mark newly inserted devices (not present in previous physical scan)
        for name, new_ns in nss.items():
            if name not in prev_physical and new_ns.state not in ('s', 'W'):
                new_ns.state = '^'
                new_ns.newly_inserted = True  # Mark for orange color even if locked/mounted
        return nss

    def assemble_partitions(self, prev_nss=None):
        """Assemble and filter partitions for display

        Args:
            prev_nss: Previous device namespaces for merging
        """
        nss = self.discover_devices(dflt='^' if prev_nss else '-', prev_nss=prev_nss)

        # If discover_devices failed (returned empty) and we have previous data, keep previous state
        if not nss and prev_nss:
            # Device scan failed or returned no devices - preserve previous state
            # This prevents losing devices when discovery temporarily fails
            # But clear temporary status messages from completed jobs
            for ns in prev_nss.values():
                if not ns.job and ns.mounts:
                    # Job finished - clear temporary status messages like "Verified: zeroed"
                    ns.mounts = [m for m in ns.mounts if not m.startswith(('Verified:', 'Stopped'))]
            return prev_nss  # Return early - don't reprocess

        nss = self.get_disk_partitions(nss)

        nss = self.merge_dev_infos(nss, prev_nss)

        # Update device workers for background probing
        self.worker_manager.update_devices(nss.keys())

        # Apply persistent blocked states
        if self.persistent_state:
            auto_block = getattr(self.opts, 'auto_block_containers', True)
            for ns in nss.values():
                # Update last_seen timestamp
                self.persistent_state.update_device_seen(ns)
                # Auto-block containers (LUKS/LVM/RAID/swap) the first time seen.
                # Once the user unblocks, a record exists so this won't re-block.
                if auto_block and getattr(ns, 'is_container', False) \
                        and not self.persistent_state.has_device_record(ns):
                    self.persistent_state.set_device_locked(ns, True, auto=True)
                # Apply persistent block state
                if self.persistent_state.get_device_locked(ns):
                    ns.state = 'Blk'
                    if self.persistent_state.is_device_auto_blocked(ns):
                        ns.auto_blocked = True

        # Clear inferred states so they can be re-computed based on current job status
        self.clear_inferred_states(nss)

        # Re-apply Mnt state for devices with mounts (cleared above, needed for set_all_states)
        # Devices that are in use (mounted/swap/backing a holder) are treated as
        # mounted so they cannot be wiped. But Blk trumps Mnt - blocked devices
        # stay blocked even if mounted.
        for ns in nss.values():
            if (ns.mounts or getattr(ns, 'in_use', False)) and ns.state != 'Blk':
                ns.state = 'Mnt'

        self.set_all_states(nss)  # set inferred states (propagates Mnt/Busy to parents)

        self.compute_field_widths(nss)
        return nss

    @staticmethod
    def dump(parts=None, title='after lsblk'):
        """Print nicely formatted device information"""
        if not parts:
            return

        print(f'\n{"=" * 80}')
        print(f'{title}')
        print(f'{"=" * 80}\n')

        # Separate disks and partitions
        disks = {name: part for name, part in parts.items() if part.type == 'disk'}
        partitions = {name: part for name, part in parts.items() if part.type == 'part'}

        # Print each disk with its partitions
        for disk_name in sorted(disks.keys()):
            disk = disks[disk_name]

            # Disk header
            print(f'┌─ {disk.name} ({disk.model or "Unknown Model"})')
            print(f'│  Size: {Utils.human(disk.size_bytes)}  Serial: {disk.serial or "N/A"}  Port: {disk.port or "N/A"}')
            print(f'│  State: {disk.state}  Marker: {disk.marker or "(none)"}')

            # Hardware capabilities
            if disk.hw_caps:
                print(f'│  Hardware: {disk.hw_caps}')

            # Find and print partitions for this disk
            disk_parts = [(name, part) for name, part in partitions.items()
                         if part.parent == disk.name]

            if disk_parts:
                for i, (part_name, part) in enumerate(sorted(disk_parts)):
                    is_last = (i == len(disk_parts) - 1)
                    branch = '└─' if is_last else '├─'

                    # Partition info
                    label_str = f'"{part.label}"' if part.label else '(no label)'
                    fstype_str = part.fstype or '(no filesystem)'

                    print(f'│  {branch} {part.name}: {Utils.human(part.size_bytes)}')
                    print(f'│  {"  " if is_last else "│ "}  Label: {label_str}  Type: {fstype_str}')
                    print(f'│  {"  " if is_last else "│ "}  State: {part.state}  UUID: {part.uuid or "N/A"}')

                    if part.marker:
                        print(f'│  {"  " if is_last else "│ "}  Marker: {part.marker}')

                    if part.mounts:
                        mounts_str = ', '.join(part.mounts)
                        print(f'│  {"  " if is_last else "│ "}  Mounted: {mounts_str}')
            else:
                print(f'│  └─ (no partitions)')

            print('│')

        print(f'{"=" * 80}\n')

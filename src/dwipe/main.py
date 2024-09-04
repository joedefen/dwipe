#!/usr/bin/env python3
""" TBD


"""
# pylint: disable=too-many-branches,too-many-statements,import-outside-toplevel
# pylint: disable=too-many-instance-attributes,invalid-name
# pylint: disable=broad-exception-caught,consider-using-with
# pylint: disable=too-many-return-statements,too-many-locals

import os
import fnmatch
import sys
import re
import subprocess
import time
import threading
import random
import shutil
import traceback
import curses as cs
from types import SimpleNamespace
from typing import Tuple, List
from dwipe.PowerWindow import Window, OptionSpinner

def human(number):
    """ Return a concise number description."""
    suffixes = ['K', 'M', 'G', 'T']
    number = float(number)
    while suffixes:
        suffix = suffixes.pop(0)
        number /= 1024
        if number < 99.95 or not suffixes:
            return f'{number:.1f}{suffix}'
    return None
##############################################################################
def ago_str(delta_secs, signed=False):
    """ Turn time differences in seconds to a compact representation;
        e.g., '18h·39m'
    """
    ago = int(max(0, round(delta_secs if delta_secs >= 0 else -delta_secs)))
    divs = (60, 60, 24, 7, 52, 9999999)
    units = ('s', 'm', 'h', 'd', 'w', 'y')
    vals = (ago%60, int(ago/60)) # seed with secs, mins (step til 2nd fits)
    uidx = 1 # best units
    for div in divs[1:]:
        # print('vals', vals, 'div', div)
        if vals[1] < div:
            break
        vals = (vals[1]%div, int(vals[1]/div))
        uidx += 1
    rv = '-' if signed and delta_secs < 0 else ''
    rv += f'{vals[1]}{units[uidx]}' if vals[1] else ''
    rv += f'{vals[0]:d}{units[uidx-1]}'
    return rv


class ZapJob:
    """ TBD """

    # Generate a 1MB buffer of random data
    BUFFER_SIZE = 1 * 1024 * 1024  # 1MB
    WRITE_SIZE = 16 * 1024  # 16KB
    buffer = bytearray(os.urandom(BUFFER_SIZE))
    zero_buffer = bytes(WRITE_SIZE)

    # Shared status string

    def __init__(self, device_path, total_size, opts=None):
        self.opts = opts if opts else SimpleNamespace(dry_run=False)
        self.device_path = device_path
        self.total_size = total_size
        self.do_abort = False
        self.status_lock = threading.Lock()  # TODO: remove
        self.thread = None

        self.start_mono = time.monotonic()  # Track the start time
        self.total_written = 0
        self.wr_hists = []  # list of (mono, written)
        self.done = False

    @staticmethod
    def start_job(device_path, total_size, opts):
        """ TBD """
        job = ZapJob(device_path=device_path, total_size=total_size, opts=opts)
        job.thread = threading.Thread(target=job.write_random_chunk)
        job.wr_hists.append(SimpleNamespace(mono=time.monotonic(), written=0))
        job.thread.start()
        return job

    def get_status_str(self):
        """ TBD """
        elapsed_time = time.monotonic() - self.start_mono
        write_rate = self.total_written / elapsed_time if elapsed_time > 0 else 0
        percent_complete = (self.total_written / self.total_size) * 100
        return (f"Write rate: {write_rate / (1024 * 1024):.2f} MB/s, "
                         f"Completed: {percent_complete:.2f}%")

    def get_status(self):
        """ TBD """
        pct_str, rate_str, when_str = '', '', ''
        mono = time.monotonic()
        written = self.total_written
        elapsed_time = mono - self.start_mono

        pct = (self.total_written / self.total_size) * 100
        pct_str = f'{int(round(pct))}%'
        if self.do_abort:
            pct_str = 'STOP'

        self.wr_hists.append(SimpleNamespace(mono=mono, written=written))
        floor = mono - 30  # 30w moving average
        while len(self.wr_hists) >= 3 and self.wr_hists[1].mono >= floor:
            del self.wr_hists[0]
        delta_mono = mono - self.wr_hists[0].mono
        rate = (written - self.wr_hists[0].written) / delta_mono if delta_mono > 1.0 else 0
        rate_str = f'{human(int(round(rate, 0)))}/s'

        if rate > 0:
            when = int(round((self.total_size - self.total_written)/rate))
            when_str = ago_str(when)


        return ago_str(int(round(elapsed_time))), pct_str, rate_str, when_str

    def write_random_chunk(self):
        """Writes random chunks to a device and updates the progress status."""
        self.total_written = 0  # Track total bytes written
        first_write = True

        with open(self.device_path, 'wb') as device:
            # for loop in range(10000000000):
            while True:
                if self.do_abort:
                    break
                offset = random.randint(0, ZapJob.BUFFER_SIZE - ZapJob.WRITE_SIZE)
                # Use memoryview to avoid copying the data
                chunk = memoryview(ZapJob.buffer)[offset:offset + ZapJob.WRITE_SIZE]

                if self.opts.dry_run:
                    bytes_written = self.total_size // 120
                    time.sleep(0.25)
                else:
                    bytes_written = device.write(chunk)
                if first_write:
                    first_write = 0
                self.total_written += bytes_written
                # Optional: Check for errors or incomplete writes
                if bytes_written < ZapJob.WRITE_SIZE:
                    break
                if self.opts.dry_run and self.total_written >= self.total_size:
                    break
            # clear the beginning of device whether aborted or not
            # if we have started writing
            if self.total_written > 0:
                device.seek(0)
                chunk = memoryview(ZapJob.zero_buffer)
                bytes_written = device.write(chunk)
        self.done = True

class DiskWipe:
    """" TBD """
    singleton = None
    def __init__(self, opts=None):
        DiskWipe.singleton = self
        self.opts = opts if opts else SimpleNamespace( debug=0,
                        dry_run=False, loop=2, search='', units='human')
        self.DB = bool(self.opts.debug)
        self.mounts_lines = None
        self.partitions = {} # a dict of namespaces keyed by name
        self.visibles = []   # visible partitions
        self.phys_majors = set() # major devices that are physical devices
        self.virtual_majors = set() # major devices that are NOT physical devices
        self.blkid_lines = None
        self.disks = {}    # devices that don't have another device as prefix
        self.wids = None
        self.head_str = None
        self.job_cnt = 0
        self.exit_when_no_jobs = False

        self.prev_filter = '' # string
        self.filter = None # compiled pattern
        self.pick_is_running = False
        self.pick_name = ''  # device name of current pick line
        self.pick_actions = {} # key, tag

        # EXPAND
        self.win, self.spin = None, None

        self.check_preqreqs()

    @staticmethod
    def check_preqreqs():
        """ Check that needed programs are installed. """
        ok = True
        for prog in 'blkid'.split():
            if shutil.which(prog) is None:
                ok = False
                print(f'ERROR: cannot find {prog!r} on $PATH')
        if not ok:
            sys.exit(1)

    @staticmethod
    def mod_pick(line):
        """ Callback to modify the "pick line" being highlighted;
            We use it to alter the state
        """
        this = DiskWipe.singleton
        this.pick_name, this.pick_actions = this.get_actions(line)
        header = this.get_keys_line()
        # ASSUME line ends in /....
        parts = header.split('/', maxsplit=1)
        wds = parts[0].split()
        this.win.head.pad.move(0, 0)
        for wd in wds:
            if wd[0]in ('<', '|', '❚'):
                this.win.add_header(wd + ' ', resume=True)
                continue
            if wd:
                this.win.add_header(wd[0], attr=cs.A_BOLD|cs.A_UNDERLINE, resume=True)
            if wd[1:]:
                this.win.add_header(wd[1:] + ' ', resume=True)

        this.win.add_header('/', attr=cs.A_BOLD+cs.A_UNDERLINE, resume=True)
        if len(parts) > 1 and parts[1]:
            this.win.add_header(f'{parts[1]}', resume=True)
        _, col = this.win.head.pad.getyx()
        pad = ' ' * (this.win.get_pad_width()-col)
        this.win.add_header(pad, resume=True)
        return line

    @staticmethod
    def _make_partition_namespace(major, minor, name):
        return SimpleNamespace(name=name,       # /proc/partitions
                            major=major,       # /proc/partitions
                            minor=minor,       # /proc/partitions
                            parent=None,     # a partition
                            state='-',         # run-time
                            label='',       # blkid
                            blk_size=None,       # blkid
                            fstype='',      # blkid
                            used_bytes=None,  # os.statvfs() # if mounted
                            size_bytes=None,  # /sys/block/{name}/...
                            mounts=[],        # /proc/mounts
                            minors=[],
                            job=None,         # if zap running
                            line='',          # rendering
                            row=-1,
                            )

    def _slurp_command(self, command: str) -> Tuple[List[str], List[str], int]:
        """ Executes a shell command and returns its output, error, and exit code.
        Args: command (str): The shell command to execute.
              debug (bool): Whether to print the debug information.
        Returns: Tuple[List[str], List[str], int]: A tuple containing the command output lines,
                 error lines, and the exit status code.
        """
        if self.DB:
            print(f'DB + {command}')

        try:
            # Using `shlex.split()` for safety and to avoid shell=True if possible
            process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, shell=False)
            output, err = process.communicate()

            output_lines = output.splitlines(keepends=False)
            error_lines = err.splitlines(keepends=False)

            return output_lines, error_lines, process.returncode

        except subprocess.CalledProcessError as exc:
            print(f"ERR: {repr(command)} failed with return code {exc.returncode}")
            return [], [str(exc)], exc.returncode
        except Exception as exc:
            print(f"ERR: {repr(command)}: {exc}")
            return [], [str(exc)], -1

    @staticmethod
    def get_device_size(device_name):
        """
        Get block size and total size of a block device.

        :param device_name: The name of the device (e.g., 'sda', 'nvme0n1').
        :return: A SimpleNamespace containing 'block_size' and 'total_size' in bytes.
        """
        device_path = f'/sys/block/{device_name}'

        try:
            # Read block size in bytes
            with open(os.path.join(device_path,
                    'queue/hw_sector_size'), 'r', encoding='utf-8') as f:
                block_size = int(f.read().strip())

            # Read total number of sectors (blocks)
            with open(os.path.join(device_path, 'size'),
                                  'r', encoding='utf-8') as f:
                num_blocks = int(f.read().strip())

            # Calculate total size in bytes
            total_size = block_size * num_blocks

            return SimpleNamespace(
                block_size=block_size,
                total_size=total_size
            )
        except FileNotFoundError:
            print(f"Device {device_name} not found or does not have necessary information.")
            return None
        except Exception as e:
            print(f"Error reading device information for {device_name}: {e}")
            return None

    def _load_devs(self):
        """ Discover all the BTRS Devices """

        if not self.blkid_lines:
            self.blkid_lines, _, _ = self._slurp_command('blkid')
        devs = {}
        for line in self.blkid_lines:
            # /dev/nvme0n1p2: LABEL="btrfs-common"
            #   UUID="8f60fc2f-872d-4327-aff9-34c4c4cefde7"
            #   UUID_SUB="d7b0987a-1133-4844-a19b-c6c22350379a"
            #   BLOCK_SIZE="4096" TYPE="btrfs"
            #   PARTUUID="02b5122d-5229-c347-a351-142008b89149"

            matches = re.findall(r'(\w+)="([^"]+)"', line)
            ns = SimpleNamespace() # Create a dictionary to store the fields and values
            for match in matches:
                field, value = match[0].lower(), match[1]
                if field in ('type', 'block_size', 'label'):
                    setattr(ns, field, value)
            if not hasattr(ns, 'type'):
                continue
            if not hasattr(ns, 'label'):
                ns.label = ''
            ns.dev = os.path.basename(line.split(': ', maxsplit=1)[0])
            devs[ns.dev] = ns
        if self.DB:
            print('DB: --->>> after load_devs()')
            for dev, ns in devs.items():
                print(f'DB: {dev}: {vars(ns)}')
        return devs

    @staticmethod
    def get_partition_sizes():
        """
        Get the sizes of all block partitions on the system, including those with unknown or no filesystems.

        :return: A dictionary with device names as keys and sizes in bytes as values.
        """
        partition_sizes = {}
        block_devices = os.listdir('/sys/block/')

        for device in block_devices:
            device_path = f'/sys/block/{device}'

            # Check for partitions inside each block device
            for entry in os.listdir(device_path):
                partition_path = os.path.join(device_path, entry)

                # If it's a partition (not the main device itself)
                if os.path.isdir(partition_path) and entry.startswith(device):
                    size_path = os.path.join(partition_path, 'size')
                    try:
                        with open(size_path, 'r', encoding='utf-8') as size_file:
                            sectors = int(size_file.read().strip())
                            # Size in bytes: sectors * 512
                            size_bytes = sectors * 512
                            partition_sizes[entry] = size_bytes
                    except (FileNotFoundError, ValueError):
                        continue  # Skip if there's an issue reading the size

        return partition_sizes


    @staticmethod
    def _slurp_file(pathname):
        with open(pathname, "r", encoding='utf-8') as fh:
            return [line.strip() for line in fh]

    @staticmethod
    def get_filesystem_usage(path):
        """Return the filesystem usage statistics for the given path."""
        try:
            statvfs = os.statvfs(path)
        except Exception:
            return None
        return SimpleNamespace(
            total = statvfs.f_frsize * statvfs.f_blocks,
            used = statvfs.f_frsize * (statvfs.f_blocks - statvfs.f_bfree),
            free = statvfs.f_frsize * statvfs.f_bfree,
            available = statvfs.f_frsize * statvfs.f_bavail,
        )

    @staticmethod
    def name_check(device_name):
        """Check if device_name matches any pattern in whitelist."""
        # Define whitelist patterns
        WHITELIST = ['nvme*', 'sd*', 'hd*', 'mmcblk*']
        BLACKLIST = ['zram*', 'ram*', 'dm-*', 'loop*', 'sr*']
        for pattern in WHITELIST:
            if fnmatch.fnmatch(device_name, pattern):
                return 'whtLst'
        for pattern in BLACKLIST:
            if fnmatch.fnmatch(device_name, pattern):
                return 'blkLst'
        return ''

    @staticmethod
    def unwritable(device_name):
        """Check if the device is writable."""
        device_path = f'/dev/{device_name}'
        try:
            # Check if the device file exists and is writable
            rv = os.access(device_path, os.W_OK)
            return None if rv else 'notWr'
        except FileNotFoundError:
            return 'notFnd'

    @staticmethod
    def is_zappable(device_name):
        """Check if a device is a writable block device using whitelist and attributes."""
        # Check whitelist first
        state = DiskWipe.name_check(device_name)
        if state == 'whtLst':
            # print(f"{device_name} whitelisted")
            return True
        if state == 'blkLst':
            # print(f"{device_name} blacklisted")
            return False

        # Check writable status
        if DiskWipe.unwritable(device_name):
            print(f"{device_name} is not writable")
        return True  # Unsure is OK

    def _determine_mount_points(self):
        if self.mounts_lines is None:
            self.mounts_lines = self._slurp_file('/proc/mounts')
        rv = {}
        for line in self.mounts_lines:
            mat = re.match(r'/dev/([^/]*)\s', line)
            if not mat:
                continue
            name = mat.group(1)

            wds = re.split(r'\s+', line)
            if len(wds) < 4:
                continue
            mount, fstype = wds[1], wds[2]
            if name not in rv:
                rv[name] = SimpleNamespace(fstype=fstype, mounts=[])
            rv[name].mounts.append(mount)
        return rv

    def init_partitions(self):
        """ TBD """
        devs = self._load_devs()
        mounts = self._determine_mount_points()
        sizes = self.get_partition_sizes()
        # print(f'{sizes=}')
        # print(f'{mounts=}')
        lines = self._slurp_file('/proc/partitions')
        for line in lines:
            if not re.match(r'\b\d+\s+\d+\b', line):
                continue
            wds = line.split(maxsplit=3)
            if len(wds) < 4:
                continue
            major, minor, _, name = line.split(maxsplit=3)
            major, minor = int(major), int(minor)
            if major in self.virtual_majors:
                continue
            if major not in self.phys_majors:
                if self.is_zappable(name):
                    self.phys_majors.add(major)
                else:
                    self.virtual_majors.add(major)
                    continue
            self.partitions[name] = ns = self._make_partition_namespace(
                    major=major, minor=minor, name=name)

            if name in mounts:
                ns.fstype = mounts[name].fstype
                ns.mounts = mounts[name].mounts
                info = self.get_filesystem_usage(ns.mounts[0])
                if info:
                    ns.used_bytes = info.used
                    ns.size_bytes = info.total

            if not ns.size_bytes:
                ns.size_bytes = sizes.get(name, None)
            if not ns.size_bytes:
                info = self.get_device_size(name)
                if info:
                    ns.size_bytes = info.total_size

            if name in devs:
                ns.blk_size = devs[name].block_size
                ns.fstype = devs[name].type
                ns.label = devs[name].label

            if ns.mounts:
                ns.state = 'Mnt'
                
        all_names = list(self.partitions.keys())
        for name, part in self.partitions.items():
            for parent_name in all_names:
                if name[:-1].startswith(parent_name):
                    parent = ns.parent = self.partitions[parent_name]
                    parent.minors.append(part)
                    self.disks[parent_name] = parent
                    if not parent.fstype:
                        parent.fstype = 'DISK'
                    break

        for ns in self.partitions.values():
            for minor in ns.minors:
                if minor.state != '-':
                    ns.state = minor.state
                    break

        wids = self.wids = SimpleNamespace(name=4, state=4, label=5, fstype=4, human=6)
        for ns in self.partitions.values():
            wids.name = max(wids.name, len(ns.name))
            wids.label = max(wids.label, len(ns.label))
            wids.fstype = max(wids.fstype, len(ns.fstype))
        self.head_str = self.get_head_str()
        for ns in self.partitions.values():
            print(self.part_str(ns))

    def get_head_str(self):
        """ TBD """
        wids = self.wids
        emit = f'{"STAT":-^{wids.state}}'
        emit += f' {"NAME":-^{wids.name}}'
        emit += f' {"SIZE":-^{wids.human}}'
        emit += f' {"TYPE":-^{wids.fstype}}'
        emit += f' {"LABEL":-^{wids.label}}'
        emit += ' MOUNTS'
        return emit

    def part_str(self, partition):
        """ Convert partition to human value. """
        ns = partition # shorthand
        wids = self.wids
        emit = f'{ns.state:>{wids.state}}'
        emit += f' {ns.name:<{wids.name}}'
        emit += f' {human(ns.size_bytes):>{wids.human}}'
        emit += f' {ns.fstype:>{wids.fstype}}'
        emit += f' {ns.label:>{wids.label}}'
        emit += f' {",".join(ns.mounts)}'
        return emit

    def do_key(self, key):
        """ TBD """
        def stop_if_idle(part):
            if part.state[-1] == '%':
                if part.job and not part.job.done:
                    part.job.do_abort = True
            return 1 if part.job else 0

        def stop_all():
            rv = 0
            for part in self.partitions.values():
                rv += stop_if_idle(part)
            return rv # number jobs running
        
        def exit_if_no_jobs():
            if stop_all() == 0:
                self.win.stop_curses()
                os.system('clear; stty sane')
                sys.exit(0)
            return True  # continue running
        
        if self.exit_when_no_jobs:
            return exit_if_no_jobs()

        if not key:
            return True
        if key == cs.KEY_ENTER or key == 10: # Handle ENTER
            if self.opts.help_mode:
                self.opts.help_mode = False
                return None

        if key in self.spin.keys:
            value = self.spin.do_key(key, self.win)
            return value

        if key == 27: # ESCAPE
            self.prev_filter = ''
            self.filter = None
            self.win.pick_pos = 0
            return None

        if key in (ord('q'), ord('x')):
            self.exit_when_no_jobs = True
            self.filter = re.compile('STOPPING', re.IGNORECASE)
            self.prev_filter = 'STOPPING'
            return exit_if_no_jobs()

        if key == ord('w') and not self.pick_is_running:
            part = self.partitions[self.pick_name]
            if part.state in ('-', 'W', 's'):
                ans = self.win.answer(f'Type "y" to wipe {repr(part.name)}'
                       + f' ({human(part.size_bytes)} {part.fstype} {part.label})')
                if ans.strip().lower().startswith('y'):
                    part.job = ZapJob.start_job(f'/dev/{part.name}',
                                        part.size_bytes, opts=self.opts)
                    self.job_cnt += 1
                    part.state = '0%'
            return None

        if key == ord('s') and self.pick_is_running:
            part = self.partitions[self.pick_name]
            stop_if_idle(part)
            return None

        if key == ord('S'):
            for part in self.partitions.values():
                stop_if_idle(part)
            return None

        if key == ord('/'):
            # pylint: disable=protected-access
            start_filter = self.prev_filter

            prefix = ''
            while True:
                pattern = self.win.answer(f'{prefix}Enter filter regex:', seed=self.prev_filter)
                self.prev_filter = pattern

                pattern.strip()
                if not pattern:
                    self.filter = None
                    break

                try:
                    self.filter = re.compile(pattern, re.IGNORECASE)
                    break
                except Exception:
                    prefix = 'Bad regex: '

            if start_filter != self.prev_filter:
                # when filter changes, move to top
                self.win.pick_pos = 0

            return None
        return None
    
    def get_keys_line(self):
        """ TBD """
        # EXPAND
        line = ''
        for key, verb in self.pick_actions.items():
            if key[0] == verb[0]:
                line += f' {verb}'
            else:
                line += f' {key}:{verb}'
        # or EXPAND
        line += ' ❚'
        line += ' Stop' if self.job_cnt > 0 else ''
        line += f' quit ?:help /{self.prev_filter}  '
        # for action in self.actions:
            # line += f' {action[0]}:{action}'
        return line[1:]
    
    def get_actions(self, part):
        """ Determine the type of the current line and available commands."""
        name, actions = '', {}
        lines = self.win.body.texts
        if 0 <= self.win.pick_pos < len(lines):
            # line = lines[self.win.pick_pos]
            part = self.visibles[self.win.pick_pos]
            name = part.name
            self.pick_is_running = bool(part.job)
            # EXPAND
            if self.pick_is_running:
                actions['s'] = 'stop'
            elif part.state in ('-', 'W', 's'):
                actions['w'] = 'wipe'
        return name, actions

    def main_loop(self):
        """ TBD """

        spin = self.spin = OptionSpinner()
        spin.default_obj = self.opts
        spin.add_key('help_mode', '? - toggle help screen', vals=[False, True])
        other = 'ws/Sq'  # TODO: fix me
        other_keys = set(ord(x) for x in other)
        other_keys.add(cs.KEY_ENTER)
        other_keys.add(27) # ESCAPE
        other_keys.add(10) # another form of ENTER

        self.win = Window(head_line=True, body_rows=200, head_rows=4,
                          keys=spin.keys ^ other_keys, mod_pick=self.mod_pick)
        self.opts.name = "[hit 'n' to enter name]"
        while True:
            if self.opts.help_mode:
                self.win.set_pick_mode(False)
                self.spin.show_help_nav_keys(self.win)
                self.spin.show_help_body(self.win)
                # EXPAND
                lines = [
                    'GENERALLY AVAILABLE:',
                    '   K - kill ALL zaps in progress (if any)',
                    '   q or x - quit program (CTL-C disabled)',
                    '   / - filter devices by (anchored) regex',
                    '   ESC = clear filter and jump to top',
                    '   ENTER = stat, kill, or return from help',
                    'CONTEXT SENSITIVE:',
                    '   s - start zap of device',
                    '   k - kill zap of device',

                ]
                for line in lines:
                    self.win.put_body(line)
            else:
                def wanted(name):
                    return not self.filter or self.filter.search(name)
                # self.win.set_pick_mode(self.opts.pick_mode, self.opts.pick_size)
                self.win.set_pick_mode(True)

                self.visibles = []
                for name, partition in self.partitions.items():
                    partition.line = None
                    if partition.job:
                        if partition.job.done:
                            partition.job.thread.join()
                            partition.state = 's' if partition.job.do_abort else 'W'
                            partition.job = None
                            partition.mounts = []
                            self.job_cnt -= 1
                    if partition.job:
                        elapsed, pct, rate, until = partition.job.get_status()
                        partition.state = pct
                        partition.mounts = [f'{elapsed} {rate} REM:{until}']

                    if wanted(name) or partition.job:
                        partition.line = self.part_str(partition)
                        self.win.add_body(partition.line)
                        self.visibles.append(partition)

                self.win.add_header(self.get_keys_line(), attr=cs.A_BOLD)

                self.win.add_header(self.head_str)
                _, col = self.win.head.pad.getyx()
                pad = ' ' * (self.win.get_pad_width()-col)
                self.win.add_header(pad, resume=True)
            self.win.render()

            _ = self.do_key(self.win.prompt(seconds=3))
            self.win.clear()


def rerun_module_as_root(module_name):
    """ rerun using the module name """
    if os.geteuid() != 0: # Re-run the script with sudo
        os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        vp = ['sudo', sys.executable, '-m', module_name] + sys.argv[1:]
        os.execvp('sudo', vp)


def main():
    """Main loop"""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('-n', '--dry-run', action='store_true',
            help='just pretend to zap devices')
    parser.add_argument('-D', '--debug', action='count', default=0,
            help='debug mode (the more Ds, the higher the debug level)')
    parser.add_argument('-l', '--loop', type=int, default=0, dest='loop_secs',
            help='loop interval in secs [dflt=0 if -w else 0]')
    parser.add_argument('-/', '--search', default='',
            help='show items with search string in name')
    parser.add_argument('-W', '--no-window', action='store_false', dest='window',
            help='show in "curses" window [disables: -D,-t,-L]')
    opts = parser.parse_args()
    # opts.dry_run = True         # TODO: remove (for development)
    # DB(0, f'opts={opts}')
    dwipe = None

    try:
        if os.geteuid() != 0:
            # Re-run the script with sudo needed and opted
            rerun_module_as_root('dwipe.main')

        dwipe = DiskWipe(opts=opts)
        dwipe.init_partitions()
        
        dwipe.main_loop()
    except Exception as exce:
        if dwipe and dwipe.win:
            dwipe.win.stop_curses()
        print("exception:", str(exce))
        print(traceback.format_exc())
        sys.exit(15)

if __name__ == "__main__":
    main()

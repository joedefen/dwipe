#!/usr/bin/env python3
""" TBD


"""
# pylint: disable=too-many-branches,too-many-statements,import-outside-toplevel
# pylint: disable=too-many-instance-attributes,invalid-name
# pylint: disable=broad-exception-caught,consider-using-with
# pylint: disable=too-many-return-statements,too-many-locals

import os
from fnmatch import fnmatch
import sys
import re
import json
import subprocess
import time
import datetime
import threading
import random
import shutil
import traceback
import curses as cs
from types import SimpleNamespace
from dwipe.PowerWindow import Window, OptionSpinner

def human(number):
    """ Return a concise number description."""
    suffixes = ['K', 'M', 'G', 'T']
    number = float(number)
    while suffixes:
        suffix = suffixes.pop(0)
        number /= 1000 # decimal
        if number < 999.95 or not suffixes:
            return f'{number:.1f}{suffix}B' # decimal
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


class WipeJob:
    """ TBD """

    # Generate a 1MB buffer of random data
    BUFFER_SIZE = 1 * 1024 * 1024  # 1MB
    WRITE_SIZE = 16 * 1024  # 16KB
    STATE_OFFSET = 15 * 1024 # where json is written
    buffer = bytearray(os.urandom(BUFFER_SIZE))
    zero_buffer = bytes(WRITE_SIZE)

    # Shared status string

    def __init__(self, device_path, total_size, opts=None):
        self.opts = opts if opts else SimpleNamespace(dry_run=False)
        self.device_path = device_path
        self.total_size = total_size
        self.do_abort = False
        self.thread = None

        self.start_mono = time.monotonic()  # Track the start time
        self.total_written = 0
        self.wr_hists = []  # list of (mono, written)
        self.done = False

    @staticmethod
    def start_job(device_path, total_size, opts):
        """ TBD """
        job = WipeJob(device_path=device_path, total_size=total_size, opts=opts)
        job.thread = threading.Thread(target=job.write_partition)
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

    def prep_marker_buffer(self, is_random):
        """  Get the 1st 16KB to write:
             - 15K zeros
             - JSON status + zero fill to 1KB
        """
        data = { "unixtime": int(time.time()),
                    "scrubbed_bytes": self.total_written,
                    "size_bytes": self.total_size,
                    "mode": 'Rand' if is_random else 'Zero'
                    }
        json_data = json.dumps(data).encode('utf-8')
        buffer = bytearray(self.BUFFER_SIZE)
        buffer[:self.STATE_OFFSET] = b'\x00' * self.STATE_OFFSET
        buffer[self.STATE_OFFSET:self.STATE_OFFSET+len(json_data)] = json_data
        remaining_size = self.BUFFER_SIZE - (self.STATE_OFFSET+len(json_data))
        buffer[self.STATE_OFFSET+len(json_data):] = b'\x00' * remaining_size
        return buffer

    @staticmethod
    def read_marker_buffer(device_name):
        """ Open the device and read the first 16 KB """
        try:
            with open(f'/dev/{device_name}', 'rb') as device:
                device.seek(0)
                buffer = device.read(WipeJob.BUFFER_SIZE)
        except Exception:
            return None # cannot find info

        if buffer[:WipeJob.STATE_OFFSET] != b'\x00' * (WipeJob.STATE_OFFSET):
            return None # First 15 KB are not zeros

        # Extract JSON data from the next 1 KB Strip trailing zeros
        json_data_bytes = buffer[WipeJob.STATE_OFFSET:WipeJob.BUFFER_SIZE].rstrip(b'\x00')

        if not json_data_bytes:
            return None # No JSON data found

        # Deserialize the JSON data
        try:
            data = json.loads(json_data_bytes.decode('utf-8'))
        except (json.JSONDecodeError, Exception):
            return None # Invalid JSON data!

        rv = {}
        for key, value in data.items():
            if key in ('unixtime', 'scrubbed_bytes', 'size_bytes') and isinstance(value, int):
                rv[key] = value
            elif key in ('mode', ) and isinstance(value, str):
                rv[key] = value
            else:
                return None # bogus data
        if len(rv) != 4:
            return None # bogus data
        return SimpleNamespace(**rv)


    def write_partition(self):
        """Writes random chunks to a device and updates the progress status."""
        self.total_written = 0  # Track total bytes written
        is_random = self.opts.random

        with open(self.device_path, 'wb') as device:
            # for loop in range(10000000000):
            offset = 0
            chunk = memoryview(WipeJob.zero_buffer)
            while True:
                if self.do_abort:
                    break
                if is_random:
                    offset = random.randint(0, WipeJob.BUFFER_SIZE - WipeJob.WRITE_SIZE)
                    # Use memoryview to avoid copying the data
                    chunk = memoryview(WipeJob.buffer)[offset:offset + WipeJob.WRITE_SIZE]

                if self.opts.dry_run:
                    bytes_written = self.total_size // 120
                    time.sleep(0.25)
                else:
                    try:
                        bytes_written = device.write(chunk)
                    except Exception:
                        bytes_written = 0
                self.total_written += bytes_written
                # Optional: Check for errors or incomplete writes
                if bytes_written < WipeJob.WRITE_SIZE:
                    break
                if self.opts.dry_run and self.total_written >= self.total_size:
                    break
            # clear the beginning of device whether aborted or not
            # if we have started writing + status in JSON
            if not self.opts.dry_run and self.total_written > 0 and is_random:
                device.seek(0)
                # chunk = memoryview(WipeJob.zero_buffer)
                bytes_written = device.write(self.prep_marker_buffer(is_random))
        self.done = True

class DeviceInfo:
    """ Class to dig out the info we want from the system."""
    disk_majors = set() # major devices that are disks

    def __init__(self, opts):
        self.opts = opts
        self.DB = opts.debug
        self.wids = None
        self.head_str = None
        self.partitions = None

    @staticmethod
    def _make_partition_namespace(major, name, size_bytes, dflt):
        return SimpleNamespace(name=name,       # /proc/partitions
            major=major,       # /proc/partitions
            # minor=minor,       # /proc/partitions
            parent=None,     # a partition
            state=dflt,         # run-time state
            dflt=dflt,         # default run-time state
            label='',       # blkid
            fstype='',      # blkid
            model='',       # /sys/class/block/{name}/device/vendor|model
            # fsuse='-',
            size_bytes=size_bytes,  # /sys/block/{name}/...
            marker='',      #  persistent status
            mounts=[],        # /proc/mounts
            minors=[],
            job=None,         # if zap running
            )


    @staticmethod
    def get_device_vendor_model(device_name):
        """ Gets the vendor and model for a given device from the /sys/class/block directory.
        - Args: - device_name: The device name, such as 'sda', 'sdb', etc.
-       - Returns: A string containing the vendor and model information.
        """
        def get_str(device_name, suffix):
            try:
                rv = ''
                fullpath = f'/sys/class/block/{device_name}/device/{suffix}'
                with open(fullpath, 'r', encoding='utf-8') as f: # Read information
                    rv = f.read().strip()
            except (FileNotFoundError, Exception):
                # print(f"Error reading {info} for {device_name} : {e}")
                pass
            return rv

        # rv = f'{get_str(device_name, "vendor")}' #vendor seems useless/confusing
        rv = f'{get_str(device_name, "model")}'
        return rv.strip()

    def parse_lsblk(self, dflt):
        """ Parse ls_blk for all the goodies we need """
        def eat_one(device):
            entry = self._make_partition_namespace(0, '', '', dflt)
            entry.name=device.get('name', '')
            maj_min=device.get('maj:min', (-1,-1))
            wds = maj_min.split(':',  maxsplit=1)
            entry.major = -1
            if len(wds) > 0:
                entry.major = int(wds[0])
            entry.fstype = device.get('fstype', '')
            if entry.fstype is None:
                entry.fstype = ''
            entry.label = device.get('label', '')
            if not entry.label:
                entry.label=device.get('partlabel', '')
            if entry.label is None:
                entry.label = ''
        #   entry.fsuse=device.get('fsuse%', '')
        #   if entry.fsuse is None:
        #       entry.fsuse = '-'
            entry.size_bytes=int(device.get('size', 0))
            mounts = device.get('mountpoints', [])
            while len(mounts) >= 1 and mounts[0] is None:
                del mounts[0]
            entry.mounts = mounts
            if not mounts:
                marker = WipeJob.read_marker_buffer(entry.name)
                now = int(round(time.time()))
                if (marker and marker.size_bytes == entry.size_bytes
                        and 0 <= marker.scrubbed_bytes <= entry.size_bytes
                        and marker.unixtime < now):
                    pct = int(round((marker.scrubbed_bytes/marker.size_bytes)*100))
                    state = 'W' if pct >= 100 else 's'
                    dt = datetime.datetime.fromtimestamp(marker.unixtime)
                    entry.marker = f'{state} {pct}% {marker.mode} {dt.strftime('%Y/%m/%d %H:%M')}'

            return entry

               # Run the `lsblk` command and get its output in JSON format with additional columns
        result = subprocess.run(['lsblk', '-J', '--bytes', '-o',
                    'NAME,MAJ:MIN,FSTYPE,LABEL,PARTLABEL,FSUSE%,SIZE,MOUNTPOINTS', ],
                    stdout=subprocess.PIPE, text=True, check=False)
        parsed_data = json.loads(result.stdout)
        entries = {}

        # Parse each block device and its properties
        for device in parsed_data['blockdevices']:
            parent = eat_one(device)
            parent.fstype = self.get_device_vendor_model(parent.name)
            entries[parent.name] = parent
            for child in device.get('children', []):
                entry = eat_one(child)
                entries[entry.name] = entry
                entry.parent = parent.name
                parent.minors.append(entry.name)
                if not parent.fstype:
                    parent.fstype = 'DISK'
                self.disk_majors.add(entry.major)
                if entry.mounts:
                    entry.state = 'Mnt'
                    parent.state = 'Mnt'
                elif entry.marker:
                    entry.state = entry.marker[0]

        if self.DB:
            print('\n\nDB: --->>> after parse_lsblk:')
            for entry in entries.values():
                print(vars(entry))

        return entries

    @staticmethod
    def set_one_state(nss, ns, to=None, test_to=None):
        """ Optionally, update a state, and always set inferred states
        """
        ready_states = ('s', 'W', '-', '^')
        job_states = ('*%', 'STOP')
        inferred_states = ('Busy', 'Mnt', )
        # other_states = ('Lock', 'Unlk')

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
        if to == 'Lock' and (not state_in(ns.state, list(ready_states) + ['Mnt'])
                or ns.parent):
            return False
        if to == 'Unlk' and ns.state != 'Lock':
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
        #  -- clearing these states will be done on the device
        #     refresh
        if parent and state_in(ns.state, inferred_states):
            if parent.state != 'Lock':
                parent.state = ns.state
        if state_in(ns.state, job_states):
            if parent:
                parent.state = 'Busy'
            for minor in minors:
                minor.state = 'Busy'
        return True

    @staticmethod
    def set_all_states(nss):
        """ Set every state per linkage inferences """
        for ns in nss.values():
            DeviceInfo.set_one_state(nss, ns)

    def get_disk_partitions(self, nss):
        """ Determine which partitions we want some are bogus like zram """

        def whitelisted(device_name):
            """Check if device_name matches any pattern in whitelist
            which are the disk devices."""
            WHITELIST = ['nvme*', 'sd*', 'hd*', 'mmcblk*']
            for pattern in WHITELIST:
                if fnmatch(device_name, pattern):
                    return True
            return False

        def blacklisted(device_name):
            """Check if device_name matches any pattern in black list
              which are know not to be physical disks."""
            BLACKLIST = ['zram*', 'ram*', 'dm-*', 'loop*', 'sr*']
            for pattern in BLACKLIST:
                if fnmatch(device_name, pattern):
                    return 'blkLst'
            return ''

        def writable(device_name):
            """Check if the device is writable."""
            device_path = f'/dev/{device_name}'
            try: # Check if the device file exists and is writable
                return os.access(device_path, os.W_OK)
            except FileNotFoundError:
                return False

        ok_nss = {}
        for name, ns in nss.items():
            if ns.major in self.disk_majors or whitelisted(name):
                ok_nss[name] = ns
                continue
            if blacklisted(name):
                continue
            if writable(name):
                if self.DB:
                    print(r'DB:include {repr(name)} [not white/black but writable]')
                ok_nss[name] = ns
                continue
            if self.DB:
                print(r'DB:exclude {repr(name)} [not white/black but unwritable]')
        return ok_nss

    def compute_field_widths(self, nss):
        """ TBD """

        wids = self.wids = SimpleNamespace(state=5, name=4, human=7, fstype=4, label=5)
        for ns in nss.values():
            wids.state = max(wids.state, len(ns.state))
        #   wids.fsuse = max(wids.fsuse, len(ns.fsuse))
            wids.name = max(wids.name, len(ns.name)+2)
            if ns.label is None:
                pass
            wids.label = max(wids.label, len(ns.label))
            wids.fstype = max(wids.fstype, len(ns.fstype))
        self.head_str = self.get_head_str()
        if self.DB:
            print('\n\nDB: --->>> after compute_field_widths():')
            print(f'self.wids={vars(wids)}')

    def get_head_str(self):
        """ TBD """
        sep = '  '
        wids = self.wids
        emit = f'{"STATE":_^{wids.state}}'
        emit += f'{sep}{"NAME":_^{wids.name}}'
    #   emit += f'{sep}{"USE%":_^{wids.fsuse}}'
        emit += f'{sep}{"SIZE":_^{wids.human}}'
        emit += f'{sep}{"TYPE":_^{wids.fstype}}'
        emit += f'{sep}{"LABEL":_^{wids.label}}'
        emit += f'{sep}MOUNTS/STATUS'
        return emit

    def part_str(self, partition):
        """ Convert partition to human value. """
#       def print_str_or_dashes(name, width, chrs=' -'):
#           if not name.strip(): # Create a string of '─' characters of the specified width
#               result = f'{chrs}' * (width//2)
#               result += ' ' * (width%2)
#           else: # Format the name to be right-aligned within the specified width
#               result = f'{name:>{width}}'
#           return result

        def print_str_or_dash(name, width, empty='-'):
            if not name.strip(): # return
                name = empty
            return f'{name:^{width}}'

        sep = '  '
        ns = partition # shorthand
        wids = self.wids
        emit = f'{ns.state:^{wids.state}}'
        # name_str = ('' if ns.parent is None else '⮞ ') + ns.name
        #name_str = ('● ' if ns.parent is None else '  ') + ns.name
        name_str = ('■ ' if ns.parent is None else '  ') + ns.name
#       if ns.parent is None and 1 + len(name_str) <= wids.name:
#           name_str += ' ' * (wids.name - len(name_str) - 1) + '■'

        emit += f'{sep}{name_str:<{wids.name}}'
    #   emit += f'{sep}{ns.fsuse:^{wids.fsuse}}'
        emit += f'{sep}{human(ns.size_bytes):>{wids.human}}'
        emit += sep + print_str_or_dash(ns.fstype, wids.fstype)
        # emit += f'{sep}{ns.fstype:>{wids.fstype}}'
        if ns.parent is None:
            emit += sep + '■' + '─'*(wids.label-2) + '■'
        else:
            emit += sep + print_str_or_dash(ns.label, wids.label)
        # emit += f' {ns.label:>{wids.label}}'
        if ns.mounts:
            emit += f'{sep}{",".join(ns.mounts)}'
        else:
            emit += f'{sep}{ns.marker}'
        return emit

    def merge_dev_infos(self, nss, prev_nss=None):
        """ Merge old DevInfos into new DevInfos  """
        if not prev_nss:
            return nss
        for name, prev_ns in prev_nss.items():
            # merge old jobs forward
            new_ns = nss.get(name, None)
            if new_ns:
                if prev_ns.job:
                    new_ns.job = prev_ns.job
                new_ns.state = new_ns.dflt = prev_ns.dflt
                if prev_ns.state == 'Lock':
                    pass
                if prev_ns.state not in ('Busy', 'Unlk'): # re-infer these
                    new_ns.state = prev_ns.state
            elif prev_ns.job:
                # unplugged device with job..
                nss[name] = prev_ns # carry forward
                prev_ns.job.do_abort = True
        for name, prev_ns in nss.items():
            if name not in prev_nss:
                prev_ns.state = '^'
        return nss

    def assemble_partitions(self, prev_nss=None):
        """ TBD """
        nss = self.parse_lsblk(dflt='^' if prev_nss else '-')

        nss = self.get_disk_partitions(nss)

        nss = self.merge_dev_infos(nss, prev_nss)

        self.set_all_states(nss)  # set inferred states

        self.compute_field_widths(nss)

        if self.DB:
            print('\n\nDB: --->>> after assemble_partitions():')
            for name, ns in nss.items():
                print(f'DB: {name}: {vars(ns)}')
        self.partitions = nss
        return nss

class DiskWipe:
    """" TBD """
    singleton = None
    def __init__(self, opts=None):
        DiskWipe.singleton = self
        self.opts = opts if opts else SimpleNamespace(debug=0, dry_run=False)
        self.DB = bool(self.opts.debug)
        self.mounts_lines = None
        self.partitions = {} # a dict of namespaces keyed by name
        self.visibles = []   # visible partitions given the filter
        self.wids = None
        self.job_cnt = 0
        self.exit_when_no_jobs = False

        self.prev_filter = '' # string
        self.filter = None # compiled pattern
        self.pick_is_running = False
        self.pick_name = ''  # device name of current pick line
        self.pick_actions = {} # key, tag
        self.dev_info = None

        # EXPAND
        self.win, self.spin = None, None

        self.check_preqreqs()

    @staticmethod
    def check_preqreqs():
        """ Check that needed programs are installed. """
        ok = True
        for prog in 'lsblk'.split():
            if shutil.which(prog) is None:
                ok = False
                print(f'ERROR: cannot find {prog!r} on $PATH')
        if not ok:
            sys.exit(1)

    def test_state(self, ns, to=None):
        """ Test if OK to set state of partition """
        return self.dev_info.set_one_state(self.partitions, ns, test_to=to)

    def set_state(self, ns, to=None):
        """ Set state of partition """
        return self.dev_info.set_one_state(self.partitions, ns, to=to)

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
        if key in (cs.KEY_ENTER, 10): # Handle ENTER
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
            if self.test_state(part, to='0%'):
                ans = self.win.answer(f'Type "y" to wipe {repr(part.name)} : '
                        + f' st={repr(part.state)} sz={human(part.size_bytes)}'
                        + f' ty={part.fstype} label={part.label}'
                        )
                if ans.strip().lower().startswith('y'):
                    part.job = WipeJob.start_job(f'/dev/{part.name}',
                                        part.size_bytes, opts=self.opts)
                    self.job_cnt += 1
                    self.set_state(part, to='0%')
            return None

        if key == ord('s') and self.pick_is_running:
            part = self.partitions[self.pick_name]
            stop_if_idle(part)
            return None

        if key == ord('S'):
            for part in self.partitions.values():
                stop_if_idle(part)
            return None

        if key == ord('l'):
            part = self.partitions[self.pick_name]
            self.set_state(part, 'Unlk' if part.state == 'Lock' else 'Lock')

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
        # KEYS
        line = ''
        for key, verb in self.pick_actions.items():
            if key[0] == verb[0]:
                line += f' {verb}'
            else:
                line += f' {key}:{verb}'
        line += ' ❚'
        line += ' Stop' if self.job_cnt > 0 else ''
        line += f' quit ?:help /{self.prev_filter}  Mode='
        line += f'{"Random" if self.opts.random else "Zeros"}'
        # for action in self.actions:
            # line += f' {action[0]}:{action}'
        return line[1:]

    def get_actions(self, part):
        """ Determine the type of the current line and available commands."""
        # KEYS
        name, actions = '', {}
        lines = self.win.body.texts
        if 0 <= self.win.pick_pos < len(lines):
            # line = lines[self.win.pick_pos]
            part = self.visibles[self.win.pick_pos]
            name = part.name
            self.pick_is_running = bool(part.job)
            # EXPAND
            if self.test_state(part, to='STOP'):
                actions['s'] = 'stop'
            elif self.test_state(part, to='0%'):
                actions['w'] = 'wipe'
            if self.test_state(part, to='Lock'):
                actions['l'] = 'lock'
            if self.test_state(part, to='Unlk'):
                actions['l'] = 'unlk'
        return name, actions

    def main_loop(self):
        """ TBD """

        spin = self.spin = OptionSpinner()
        spin.default_obj = self.opts
        spin.add_key('help_mode', '? - toggle help screen', vals=[False, True])
        spin.add_key('random', 'r - toggle whether random or zeros', vals=[True, False])
        # KEYS
        other = 'wsl/Sqx'
        other_keys = set(ord(x) for x in other)
        other_keys.add(cs.KEY_ENTER)
        other_keys.add(10) # another form of ENTER
        other_keys.add(27) # ESCAPE

        self.win = Window(head_line=True, body_rows=200, head_rows=4,
                          keys=spin.keys ^ other_keys, mod_pick=self.mod_pick)
        self.opts.name = "[hit 'n' to enter name]"
        check_devices_mono = time.monotonic()
        while True:
            if self.opts.help_mode:
                self.win.set_pick_mode(False)
                self.spin.show_help_nav_keys(self.win)
                self.spin.show_help_body(self.win)
                # KEYS
                lines = [
                    'CONTEXT SENSITIVE:',
                    '   w - wipe device',
                    '   s - stop wipe device',
                    '   l - lock/unlock disk',
                    '   S - stop ALL wipes in progress',
                    'GENERALLY AVAILABLE:',
                    '   q or x - quit program (CTL-C disabled)',
                    '   / - filter devices by (anchored) regex',
                    '   ESC = clear filter and jump to top',
                    '   ENTER = return from help',

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
                            to='s' if partition.job.do_abort else 'W'
                            self.set_state(partition, to=to)
                            partition.dflt = to
                            partition.job = None
                            partition.mounts = []
                            self.job_cnt -= 1
                    if partition.job:
                        elapsed, pct, rate, until = partition.job.get_status()
                        partition.state = pct
                        partition.mounts = [f'{elapsed} {rate} REM:{until}']

                    if partition.parent and self.partitions[partition.parent].state == 'Lock':
                        continue

                    if wanted(name) or partition.job:
                        partition.line = self.dev_info.part_str(partition)
                        self.win.add_body(partition.line)
                        self.visibles.append(partition)

                self.win.add_header(self.get_keys_line(), attr=cs.A_BOLD)

                self.win.add_header(self.dev_info.head_str)
                _, col = self.win.head.pad.getyx()
                pad = ' ' * (self.win.get_pad_width()-col)
                self.win.add_header(pad, resume=True)
            self.win.render()

            seconds = 3.0
            _ = self.do_key(self.win.prompt(seconds=seconds))

            if time.monotonic() - check_devices_mono > (seconds * 0.95):
                info = DeviceInfo(opts=self.opts)
                self.partitions = info.assemble_partitions(self.partitions)
                self.dev_info = info
                check_devices_mono = time.monotonic()

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
    opts = parser.parse_args()

    try:
        if os.geteuid() != 0:
            # Re-run the script with sudo needed and opted
            rerun_module_as_root('dwipe.main')

        dwipe = DiskWipe(opts=opts)
        dwipe.dev_info = info = DeviceInfo(opts=opts)
        dwipe.partitions = info.assemble_partitions()
        if dwipe.DB:
            sys.exit(1)

        dwipe.main_loop()
    except Exception as exce:
        if dwipe and dwipe.win:
            dwipe.win.stop_curses()
        print("exception:", str(exce))
        print(traceback.format_exc())
        sys.exit(15)

if __name__ == "__main__":
    main()

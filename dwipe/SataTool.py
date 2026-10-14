#!/usr/bin/env python3
""" TBD """
# pylint: disable=invalid-name,multiple-statements,line-too-long
# pylint: disable=broad-exception-caught,too-many-nested-blocks
# pylint: disable=too-many-locals,too-many-instance-attributes
# pylint: disable=too-many-boolean-expressions

import os
import json
import subprocess
import re
import time
from types import SimpleNamespace
from .Utils import Utils
from .Tunables import Tunables

class WipeIpDb:
    """Manages the persistent state of in-progress firmware wipes."""

    def __init__(self, directory):
        self.path = os.path.join(directory, "firmware-wipes-ip.json")
        self._ensure_file()

    def _ensure_file(self):
        """Creates an empty JSON set if the file doesn't exist."""
        if not os.path.exists(self.path):
            self._write_dict({})
            Utils.fix_file_ownership(self.path)

    def _read_dict(self):
        try:
            now = time.time()
            with open(self.path, 'r', encoding='utf-8') as f:
                data, cleaned = json.load(f), {}
                for key, unixtime in data.items():
                    if now - unixtime <= 7 * 24 * 3600:
                        cleaned[key] = unixtime
                if len(cleaned) != len(data):
                    self._write_dict(cleaned)
                return cleaned
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write_dict(self, key_dict):
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(key_dict, f, indent=4)

    def start_wipe(self, key):
        """Adds a key to the journal."""
        key_dict = self._read_dict()
        key_dict[key] = time.time()
        self._write_dict(key_dict)

    def end_wipe(self, key):
        """Removes a key from the journal (successful completion)."""
        self.purge(key)

    def purge(self, key):
        """Removes key if exists and returns True/False for recovery logic."""
        key_dict = self._read_dict()
        if key in key_dict:
            del key_dict[key]
            self._write_dict(key_dict)
            return True
        return False

class SataTool:
    """ TBD """
    tunables = Tunables()

    def __init__(self, device_name, timeout=10):
        self.timeout = timeout
        if device_name.startswith('/dev/'):
            self.device_name = device_name[len('/dev/'):]
            self.device_path = device_name
        else:
            self.device_name = device_name
            self.device_path = f'/dev/{self.device_name}'
        self.secures = None  # last parse result of security section values
        self.job = SimpleNamespace() # data about running wipe job
        self.job.process = None # set when wipe job is running
        self.job.wipe_started_mono = None # set when wipe job started
        self.job.est_secs = 0
        self.original_data = None
        self.model_serial_key = '<=>'.join(self.get_disk_identifiers())
        self.wipe_ip_db = WipeIpDb(directory=Utils.get_config_dir())

    def get_disk_identifiers(self):
        """
        Non-blocking retrieval of Model and Serial.
        Works even if the drive is in a 'D' state (SATA Wipe).
        """
        model, serial = 'unknown', 'unknown'
        dev_name = self.device_name

        # 1. Get Model from sysfs (Fast and Safe)
        model_path = f"/sys/class/block/{dev_name}/device/model"
        try:
            with open(model_path, 'r', encoding='utf-8') as f:
                model = f.read().strip()
        except OSError as e:
            Utils.get_logger().debug(f"model read failed for {dev_name}: {e}")

        # 2. Get Serial from /dev/disk/by-id (Kernel Cache)
        disk_id_path = "/dev/disk/by-id"
        if os.path.exists(disk_id_path):
            for link in os.listdir(disk_id_path):
                # We want the primary link, not the partition links (which have -partX)
                if "-part" in link:
                    continue

                # Check if this link points to our device (e.g., ../../sdb)
                try:
                    target = os.readlink(os.path.join(disk_id_path, link))
                    if os.path.basename(target) == dev_name:
                        # Logic for ATA/SATA
                        if link.startswith('ata-'):
                            # Format: ata-MODEL_SERIAL
                            # Example: ata-ST1000LM035-1RK172_WDE12345
                            parts = link[4:].split('_')
                            if len(parts) > 1:
                                serial = parts[-1]
                            break

                        # Logic for NVMe
                        if link.startswith('nvme-'):
                            # Format: nvme-MODEL_SERIAL
                            parts = link[5:].split('_')
                            if len(parts) > 1:
                                serial = parts[-1]
                            break
                except OSError:
                    continue

        return model, serial

    def run_cmd(self, argv, timeout=None):
        """ RUN a command return the output """
        timeout = self.timeout if timeout is None else timeout
        cmd = ['hdparm']
        cmd += argv if isinstance(argv, (list, tuple)) else [argv]
        cmd += [self.device_path]
        output = subprocess.run(cmd, check=False, capture_output=True,
                              text=True, timeout=timeout)
        return output

    @staticmethod
    def _make_ns():
        return SimpleNamespace(supported=False, enabled=False,
                frozen=False, locked=False, expired=False,
                enhanced_erase_supported=False, erase_est_secs=[4*60*60],
                sanitize_supported=False, sanitize_crypto_supported=False,
                sanitize_block_supported=False, sanitize_overwrite_supported=False,
                has_security_feature=False)  # True if hdparm found a Security block

    def _parse_output_to_secures(self, output):
        secures = self._make_ns()
        self.secures = secures
        # Look specifically for the security block
        security_match = re.search(r"(?i)Security:.*?(?=\n\w|\Z)", output, re.DOTALL)
        if not security_match:
            return secures  # has_security_feature stays False

        secures.has_security_feature = True
        sec_block = security_match.group(0)

        # Better logic: Check if the keyword exists AND is not preceded by 'not'
        # Match a bare "supported" token that is NOT preceded by "not".
        # hdparm separates tokens with TABs (e.g. "not\tsupported"), so a
        # space-based "not supported" substring check is unreliable.
        secures.supported = bool(re.search(r'(?<!not)\s+supported', sec_block))
        secures.enabled = bool(re.search(r"(?<!not)\s+enabled", sec_block))
        secures.locked = bool(re.search(r"(?<!not)\s+locked", sec_block))
        secures.frozen = bool(re.search(r"(?<!not)\s+frozen", sec_block))
        secures.expired = bool(re.search(r"(?<!not)\s+expired", sec_block))
        secures.enhanced_erase_supported = "supported: enhanced erase" in sec_block

        minutes = re.findall(r"(\d+)min", sec_block)
        if minutes:
            secures.erase_est_secs = [60*int(m) for m in minutes]

        # Look for SANITIZE capabilities in Commands/features section
        # hdparm -I shows these as separate lines like:
        #    *    SANITIZE feature set
        #    *    CRYPTO_SCRAMBLE_EXT command
        #    *    BLOCK_ERASE_EXT command
        #    *    OVERWRITE_EXT command
        if re.search(r'\*\s+SANITIZE feature set', output):
            secures.sanitize_supported = True
            secures.sanitize_crypto_supported = bool(re.search(r'\*\s+CRYPTO_SCRAMBLE_EXT', output))
            secures.sanitize_block_supported = bool(re.search(r'\*\s+BLOCK_ERASE_EXT', output))
            secures.sanitize_overwrite_supported = bool(re.search(r'\*\s+OVERWRITE_EXT', output))

        return secures

    def refresh_secures(self):
        """ Run a command to get the security values from hdparm """
        was_secures = self.secures
        output = self.run_cmd('-I')
        secures = self._parse_output_to_secures(output.stdout)
        if not was_secures and secures:
            self.rescue_locked_drive() # just in case
            secures = self.secures # may have been updated
        return secures

    # Logic for your App's Verdict
    def get_wipe_verdict(self): # , override_usb=False):
        """ TBD """

#       if data["bridge"]["vendor_id"] and not data["bridge"]["whitelisted"] and not override_usb:
#           return "FwErr: USB-Disallowed"
        if not self.secures:
            self.refresh_secures()

        if not self.secures.has_security_feature:
            return "DumbDevice"  # No ATA security feature (e.g., USB thumb drive)
        if not self.secures.supported:
            return "Unsupported"
        if self.secures.frozen:
            return "Frozen"
        if self.secures.expired:
            return "SecurityExpired"
        if self.secures.locked:
            return "Locked" # Already locked with unknown pass
        return "OK"

    def start_async_wipe(self, use_enhanced=False):
        """Starts the wipe and returns the process object immediately.
        # --- Monitoring Loop in your Main App ---
        # wipe_proc = tool.start_async_wipe()
        # while wipe_proc.poll() is None:
        #     print("Wipe in progress... (Drive is busy)")
        #     time.sleep(30)
        #
        # out, err = wipe_proc.communicate()
        """

        erase_flag = '--security-erase-enhanced' if use_enhanced else '--security-erase'
        cmd = ['hdparm', '--user-master', 'u', erase_flag, 'NULL', self.device_path]

        # Popen does NOT block. It returns immediately.
        self.job.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.job.wipe_started_mono = time.monotonic()
        idx = 0 if use_enhanced else -1
        self.job.est_secs = self.secures.erase_est_secs[idx]
        return self.job.process

    def start_sanitize_wipe(self, method='crypto'):
        """Execute SATA Sanitize command (Crypto, Block, or Overwrite).

        Args:
            method: 'crypto', 'block', or 'overwrite'

        Returns:
            Popen object for the sanitize command
        """
        # Map method names to hdparm sanitize flags
        sanitize_map = {
            'sanitize_crypto': '--sanitize-crypto-scramble',
            'sanitize_block': '--sanitize-block-erase',
            'sanitize_overwrite': '--sanitize-overwrite'
        }

        # Convert method to full method name if needed
        if not method.startswith('sanitize_'):
            method = f'sanitize_{method}'

        sanitize_flag = sanitize_map.get(method)
        if not sanitize_flag:
            raise ValueError(f"Unknown sanitize method: {method}")

        cmd = ['hdparm', '--yes-i-know-what-i-am-doing', sanitize_flag]

        # Overwrite requires a pattern (use zeros)
        if method == 'sanitize_overwrite':
            cmd.append('hex:00000000')

        cmd.append(self.device_path)

        # Store command for logging (similar to NvmeTool)
        self.last_command = cmd

        # Execute asynchronously
        self.job.process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.job.wipe_started_mono = time.monotonic()
        self.job.est_secs = 120  # Estimate for sanitize operations
        return self.job.process


    def start_wipe(self, use_enhanced=False, password="NULL", db=False):
        """
        Executes the two-stage ATA Security Erase process.
        Returns structured dict with commands_executed list and final process.

        Example return:
        {
            'commands_executed': [
                {'cmd': ['hdparm', ...], 'returncode': 0, 'sleep_after': 0},
                {'cmd': ['hdparm', ...], 'returncode': 0, 'sleep_after': 5}
            ],
            'process': <Popen object>
        }
        """
        commands_executed = []

        # 1. Pre-flight check: Re-verify not frozen
        self.refresh_secures()
        verdict = self.get_wipe_verdict()
        if verdict != "OK":
            return False, f'Drive not in OK state [{verdict}]'

        # 2. Set the Security Password
        # This moves the drive from 'not enabled' to 'enabled'
        set_pass_argv = ['hdparm'] + ['--user-master', 'u', '--security-set-pass', password]
        if db: print(f"Setting security password for {self.device_name}...")
        res = self.run_cmd(['--user-master', 'u', '--security-set-pass', password])

        # Record command execution
        commands_executed.append({
            'cmd': ' '.join(set_pass_argv),
            'returncode': res.returncode,
            'sleep_after': self.tunables.post_password_delay
        })

        time.sleep(self.tunables.post_password_delay)

        if res.returncode != 0:
            return False, f"Failed to set password: {res.stderr.strip()}"

        # 3. Verify the drive is now 'enabled'
        self.refresh_secures()
        if not self.secures.enabled:
            return False, "Password command accepted but drive is not 'enabled'."
        time.sleep(self.tunables.post_unlock_delay)

        # 4. Build erase command (to be executed asynchronously)
        erase_flag = '--security-erase-enhanced' if use_enhanced else '--security-erase'
        erase_cmd = ['hdparm', '--user-master', 'u', erase_flag, 'NULL', self.device_path]

        # Start async wipe process
        if db:
            process = self.start_async_wipe(use_enhanced=use_enhanced)
        else:
            # When not in db mode, return structured info for logging
            process = None  # Will be started by caller

        return {
            'commands_executed': commands_executed,
            'erase_command': erase_cmd,  # The final command to be executed
            'process': process
        }


    def rescue_locked_drive(self, password="NULL", db=False):
        """Attempts to unlock and disable security on a bricked drive."""

        secures = self.secures
        if secures.supported and not secures.frozen and secures.expired and (
                    secures.locked or secures.enabled
                    ) and self.wipe_ip_db.purge(self.model_serial_key):

            if db: print(f"Attempting rescue on locked device {self.device_name}...")
            if secures.locked:
                # 1. Unlock
                self.run_cmd(['--user-master', 'u', '--security-unlock', password])
            if secures.enabled or secures.locked:
                # 2. Disable (removes the 'enabled' requirement)
                self.run_cmd(['--user-master', 'u', '--security-disable', password])

            # 3. Verify
            secures = self.refresh_secures()
            if not secures.locked and not secures.enabled:
                return True, "Drive successfully rescued and unlocked."
            return False, "Rescue failed. Password may be incorrect or drive is 'Expired'."
        return True, "Drive not rescued for any reason"

    def verify_wipe_result(self):
        """
        Runs AFTER start_wipe finishes to see if the drive
        actually cleared its security state.
        """
        # Refresh the namespace
        self.refresh_secures()
        if not self.secures.supported:
            return False, "NotSupported"

        # If the wipe worked, the drive automatically disables
        # the security password and clears the 'enabled' bit.
        if not self.secures.enabled and not self.secures.locked:
            return True, "ReadyUnlocked"

        if self.secures.locked:
            return False, "StillLocked"

        return False, "SecurityEnabled"

    def test_and_restore_block(self, offset=0):
        """
        Reads, Writes a test pattern, Reads, and Restores the original data.
        Returns (success_bool, message)
        """
        test_pattern = b"WIPE_VERIFY_PATTERN_" + os.urandom(8)
        test_pattern = test_pattern.ljust(512, b'\x00')

        try:
            # Step 1: Read original
            if not self.original_data:
                with open(self.device_path, 'rb') as f:
                    f.seek(offset)
                    self.original_data = f.read(512)

                if len(self.original_data) != 512:
                    self.original_data = None
                    return False, "Could not read full 512 bytes for backup."

            # Step 2: Write Test Pattern
            # Using O_SYNC to ensure it hits the controller
            fd = os.open(self.device_path, os.O_WRONLY | os.O_SYNC)
            try:
                os.lseek(fd, offset, os.SEEK_SET)
                os.write(fd, test_pattern)
                os.fsync(fd)
            finally:
                os.close(fd)

            # Step 3: Clear Cache and Verify Write
            subprocess.run(['blockdev', '--flushbufs', self.device_path], check=True)

            with open(self.device_path, 'rb') as f:
                f.seek(offset)
                check_write = f.read(512)

            if check_write != test_pattern:
                return False, "Verification failed: Test pattern was not correctly written."

            # Step 4: Restore original data
            fd = os.open(self.device_path, os.O_WRONLY | os.O_SYNC)
            try:
                os.lseek(fd, offset, os.SEEK_SET)
                os.write(fd, self.original_data)
                os.fsync(fd)
            finally:
                os.close(fd)

            # Step 5: Final Verification of Restoration
            subprocess.run(['blockdev', '--flushbufs', self.device_path], check=True)

            with open(self.device_path, 'rb') as f:
                f.seek(offset)
                final_check = f.read(512)

            if final_check == self.original_data:
                return True, "DevReadyForWrite"
            return False, "CRITICAL: Failed to restore original data correctly!"

        except Exception as e:
            return False, f"I/O Transaction error: {str(e)}"


def main():
    """ TBD"""
    # pylint: disable=import-outside-toplevel
    import glob
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--wipe', action='store_true',
                        help='firmware wipe of given disk')
    parser.add_argument('devs', nargs='*', help="list if sdX")
    opts = parser.parse_args()


    if not opts.wipe and not opts.devs:
        pattern = '/dev/sd?'
        opts.devs = glob.glob(pattern)
    for dev in opts.devs:
        tool = SataTool(os.path.basename(dev))
        secures = tool.refresh_secures()
        verdict = tool.get_wipe_verdict()
        print(dev, f'{verdict=}')
        for attr, val in vars(secures).items():
            print(f'    {attr}: {val}')
        if opts.wipe and verdict == 'OK':
            print(f'     + start wipe {tool.device_path!r}')
            # --- Monitoring Loop ---
            job = tool.start_wipe(secures.enhanced_erase_supported, db=True)
            while job.process.poll() is None:
                elapsed = round(time.monotonic() - job.wipe_started_mono)
                elapsed_str = f'{elapsed//60}m{elapsed%60}s'
                left = max(job.est_secs - elapsed, 1)
                left_str = f'{left//60}m{left%60}s'
                print(f"Wipe in progress... {elapsed_str} -{left_str}")
                time.sleep(10)
            elapsed = round(time.monotonic() - job.wipe_started_mono)
            elapsed_str = f'{elapsed//60}m{elapsed%60}s'
            print("Wipe complete at {elapsed_str}")
            out, err = job.process.communicate()
            print('\nOUT:\n' + out)
            print('\nERR:\n' + err)

if __name__ == "__main__":
    main()

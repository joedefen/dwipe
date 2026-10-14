import os
import json
import subprocess
import io
import re
import time
from types import SimpleNamespace
from .Utils import Utils


class NvmeTool:
    """NVMe equivalent of SataTool using nvme-cli."""
    
    def __init__(self, device_name, timeout=10):
        self.timeout = timeout
        # Handle /dev/nvme0n1 or nvme0n1
        if device_name.startswith('/dev/'):
            self.device_name = device_name[len('/dev/'):]
            self.device_path = device_name
        else:
            self.device_name = device_name
            self.device_path = f'/dev/{self.device_name}'
        
        self.caps = None  # Stores parsed sanitize/format capabilities
        self.job = SimpleNamespace()
        self.job.process = None
        self.job.wipe_started_mono = None
        self.job.est_secs = 60 # NVMe is usually much faster than SATA
        self.last_command = None  # Store the last command executed
        
    def run_nvme_cmd(self, args, json_out=True):
        """Helper to run nvme-cli commands."""
        cmd = ['nvme'] + args
        if json_out:
            cmd += ['-o', 'json']
        
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
            if json_out and res.stdout:
                return json.loads(res.stdout)
            return res
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            Utils.get_logger().debug(f"nvme command {cmd!r} failed: {e}")
            return None

    def get_supported_lbaf(self):
        """Get a supported LBA format index.

        Queries the namespace to find supported LBA formats and returns
        one that should work. Returns the currently active format as first choice.

        Returns:
            int: LBAF index (0-15)
        """
        try:
            # Query namespace
            data = self.run_nvme_cmd(['id-ns', self.device_path])
            if data:
                # Get currently active format
                flbas = data.get('flbas', 0)
                if isinstance(flbas, dict):
                    current_lbaf = flbas.get('format', 0)
                else:
                    current_lbaf = int(flbas) & 0x0F

                # Try to get list of supported formats
                lbafs = data.get('lbafs', [])
                if lbafs and isinstance(lbafs, list):
                    # Check if current format is in supported list
                    if current_lbaf < len(lbafs):
                        return current_lbaf
                    # Otherwise return first supported format
                    if len(lbafs) > 0:
                        return 0

                return current_lbaf
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            Utils.get_logger().debug(f"get_supported_lbaf failed: {e}")
        return 0  # Default to format 0

    def refresh_capabilities(self):
        """
        Detects if Sanitize and Format (Crypto/Erase) are supported.
        NVMe Sanitize: Check 'id-ctrl' for 'sanicap'
        NVMe Format: Check 'id-ctrl' for 'fna'
        """
        data = self.run_nvme_cmd(['id-ctrl', self.device_path])
        caps = SimpleNamespace(
            has_sanitize=False,
            crypto_erase_supported=False,
            block_erase_supported=False,
            overwrite_supported=False,
            format_supported=False,
            format_crypto_supported=False,
            raw_data=data
        )

        if data:
            # Sanitize Capabilities (sanicap)
            has_san, crypt, block, ovwr = Utils.parse_nvme_sanitize_flags(data)
            caps.has_sanitize = has_san
            caps.crypto_erase_supported = crypt
            caps.block_erase_supported = block
            caps.overwrite_supported = ovwr

            # Optional NVM Command Support (oncs)
            # Bit 2: Format NVM command is supported
            oncs = data.get('oncs', 0)
            caps.format_supported = bool(oncs & 0x04)

            # Format Capabilities (fna)
            # Bit 2 indicates if Crypto Erase is supported via Format command
            fna = data.get('fna', 0)
            caps.format_crypto_supported = bool(fna & 0x04) and caps.format_supported

        self.caps = caps
        return caps

    def get_wipe_verdict(self, method='sanitize_block'):
        """Determines if the drive can be wiped with the specified method.

        Args:
            method: Wipe method ('sanitize_block', 'sanitize_crypto', 'sanitize_overwrite',
                               'format_erase', 'format_crypto')

        Returns:
            'OK' if supported, or error reason string
        """
        if not self.caps:
            self.refresh_capabilities()

        # Check if the specific method is supported
        if method == 'sanitize_block':
            if not self.caps.block_erase_supported:
                return "Block Erase not supported"
        elif method == 'sanitize_crypto':
            if not self.caps.crypto_erase_supported:
                return "Crypto Erase not supported"
        elif method == 'sanitize_overwrite':
            if not self.caps.overwrite_supported:
                return "Overwrite not supported"
        elif method in ('format_erase', 'format_crypto'):
            if not self.caps.format_supported:
                return "Format not supported"
        else:
            return f"Unknown method: {method}"

        # NVMe drives don't 'freeze' like SATA, but they can be Read-Only
        # or have Namespace management locks.
        return "OK"


    def start_wipe(self, method='format_crypto'):
        ctrl_path = re.sub(r'n\d+$', '', self.device_path)

        # 1. Determine the Best Command
        # If the drive supports Sanitize, it's MUCH more 'general' than Format
        if method.startswith('sanitize'):
            mode_map = {'sanitize_block': '2',
                        'sanitize_crypto': '4',
                        'sanitize_overwrite': '3'}
            cmd = ['nvme', 'sanitize', '-a', mode_map[method], self.device_path]
        else:
            # For 'Format', we send the BARE MINIMUM. 
            # No --lbaf, no --ms, no --pi. 
            # This forces the drive to use its current valid hardware configuration.
            ses_type = '2' if method == 'format_crypto' else '1'
            cmd = [
                'nvme', 'format', ctrl_path,
                '--namespace-id=0xffffffff', # Target all namespaces (prevents locks)
                f'--ses={ses_type}',
                '--force'
            ]

        # Store command for logging in task summary
        self.last_command = cmd

        try:
            # We run synchronously for the format command because it's fast
            # and we need to check for that 0x410a immediately.
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)

            # FALLBACK: If 0xffffffff (all namespaces) is rejected, try just the specific one
            if res.returncode != 0 and '0x410a' in (res.stdout + res.stderr):
                nsid_match = re.search(r'n(\d+)$', self.device_path)
                nsid = nsid_match.group(1) if nsid_match else "1"
                cmd[3] = f'--namespace-id={nsid}'
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)

            # 2. Wrap the result to fix your 'AttributeError'
            # We use io.StringIO so that .read() works in your Task manager
            self.job.process = SimpleNamespace(
                poll=lambda: 0, # Marks it as finished
                returncode=res.returncode,
                stdout=io.StringIO(res.stdout),
                stderr=io.StringIO(res.stderr),
                communicate=lambda: (res.stdout, res.stderr)
            )
            self.job.wipe_started_mono = time.monotonic()
            return self.job.process

        except Exception as e:
            self.job.process = SimpleNamespace(
                poll=lambda: 1,
                returncode=1,
                stdout=io.StringIO(""),
                stderr=io.StringIO(str(e)),
                communicate=lambda: ("", str(e))
            )
            return self.job.process

    def get_sanitize_status(self):
        """
        NVMe Sanitize runs in the background. 
        We must poll 'sanitize-log' to see if it's actually done.
        """
        log = self.run_nvme_cmd(['sanitize-log', self.device_path])
        if log:
            # sstat: 0=Idle, 1=In Progress, 2=Success
            status = log.get('sstat', 0) & 0x7 
            progress = log.get('sprog', 0) # 65535 = 100%
            percent = (progress / 65535.0) * 100
            return status, percent
        return None, 0


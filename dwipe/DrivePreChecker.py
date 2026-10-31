#!/usr/bin/env python3
import json
import subprocess
from dataclasses import dataclass

from .SataTool import SataTool
from .Utils import Utils


@dataclass
class PreCheckResult:
    # Dict of capabilities and issues
    issues: dict = None  # e.g. {"Frozen": reason, "Locked": reason}
    modes: dict = None   # e.g. {"Crypto": ..., "Block": ..., "Ovwr": ...}

    def __post_init__(self):
        if self.issues is None:
            self.issues = {}
        if self.modes is None:
            self.modes = {}

class DrivePreChecker:
    # Firmware wipe rankings for SSDs: lower number = better (1 = most desirable)
    # SSDs benefit from firmware wipes due to wear leveling and hidden blocks
    WIPE_RANKS_SSD = {
        # NVMe modes (rank 1-5)
        'Crypto': 1,    # Sanitize Cryptographic Erase - fastest, instant key invalidation
        'Block': 2,     # Sanitize Block Erase - fast, deallocates blocks
        'FCrypto': 3,   # Format with Crypto Erase - fast, namespace reformat
        'FErase': 4,    # Format with User Data Erase - less thorough than crypto
        'Ovwr': 5,      # Sanitize Overwrite - slow, pattern write
        # SATA SSD modes (rank 1-5)
        'SCrypto': 1,   # Sanitize Cryptographic Erase - fastest
        'Enhanced': 2,  # ATA Security Erase Enhanced - fast with crypto key destruction
        'SBlock': 3,    # Sanitize Block Erase - fast
        'Erase': 4,     # ATA Security Erase Normal - slow but widely supported
        'SOverwrite': 5,  # Sanitize Overwrite - slow
        # Software wipes (fallback - can't reach wear-leveled blocks on SSDs)
        'Rand': 6,      # Software Random Write - fallback
        'Zero': 7,      # Software Zero Write - fallback
    }

    # Rankings for HDDs: software wipes preferred (interruptible, resumable, progress)
    # HDDs have no wear leveling or hidden blocks, so software wipes are equally thorough
    WIPE_RANKS_HDD = {
        # Software wipes preferred for HDDs
        'Rand': 1,      # Software Random Write - preferred, interruptible, resumable
        'Zero': 2,      # Software Zero Write - preferred, fast verification
        # Firmware wipes available but not recommended (slow, no progress, not interruptible)
        'Enhanced': 3,  # ATA Security Erase Enhanced - slow, no advantage over software
        'Erase': 4,     # ATA Security Erase Normal - slow, no advantage over software
        'SCrypto': 5,   # Sanitize Cryptographic Erase - rare on HDDs
        'SBlock': 6,    # Sanitize Block Erase - rare on HDDs
        'SOverwrite': 7,  # Sanitize Overwrite - very slow
    }

    # Default to SSD rankings (used for display, etc.)
    WIPE_RANKS = WIPE_RANKS_SSD

    @staticmethod
    def sort_modes_by_rank(modes, add_star=True, is_rotational=False):
        """Sort wipe modes from worst to best rank (highest rank number first).

        Args:
            modes: iterable of mode names (e.g., ['Crypto', 'Block', 'Ovwr'])
            add_star: if True, append '*' to the last (best) mode
            is_rotational: if True, use HDD rankings (prefer software wipes)

        Returns:
            list of mode names sorted worst to best, optionally with '*' on last
        """
        ranks = DrivePreChecker.WIPE_RANKS_HDD if is_rotational else DrivePreChecker.WIPE_RANKS_SSD
        # Sort by rank descending (worst first), unknown modes get rank 99
        sorted_modes = sorted(modes, key=lambda m: ranks.get(m, 99), reverse=True)
        if add_star and sorted_modes:
            sorted_modes[-1] = sorted_modes[-1] + '*'
        return sorted_modes

    @staticmethod
    def get_fw_caps_summary(modes):
        """Get a compact summary of firmware wipe capabilities.

        Args:
            modes: iterable of mode names (e.g., ['Crypto', 'Block', 'Ovwr'])

        Returns:
            str: Summary like 'ϟCrypto', 'ϟErase', or 'ϟOverwrite' based on best available,
                 or empty string if no firmware modes available
        """
        if not modes:
            return ''

        ranks = DrivePreChecker.WIPE_RANKS
        # Find the best (lowest rank) mode
        best_mode = min(modes, key=lambda m: ranks.get(m, 99))
        best_rank = ranks.get(best_mode, 99)

        # Categorize: Crypto (rank 1 or name contains Crypto), Erase (2-4), Overwrite (5)
        if best_rank == 1 or 'Crypto' in best_mode:
            return 'ϟCrypto'
        elif best_rank <= 4:
            return 'ϟErase'
        elif best_rank == 5:
            return 'ϟOverwrite'
        return ''

    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    @staticmethod
    def get_wipe_command_args(wipe_type: str) -> str:
        """Get command arguments for a given wipe type name.

        Maps wipe mode names to their corresponding firmware command arguments.
        This is a static mapping independent of device capabilities.

        Args:
            wipe_type: Wipe mode name (e.g., 'Crypto', 'Block', 'Enhanced', 'Erase')

        Returns:
            str: Command argument string (e.g., 'sanitize_crypto', 'enhanced')
                 Empty string if wipe_type is unknown
        """
        wipe_args_map = {
            # NVMe sanitize operations
            'Crypto': 'sanitize_crypto',
            'Block': 'sanitize_block',
            'Ovwr': 'sanitize_overwrite',
            # NVMe format operations
            'FCrypto': 'format_crypto',
            'FErase': 'format_erase',
            # SATA sanitize operations
            'SCrypto': 'sanitize_crypto',
            'SBlock': 'sanitize_block',
            'SOverwrite': 'sanitize_overwrite',
            # SATA/ATA security erase operations
            'Enhanced': 'enhanced',
            'Erase': 'normal',
        }
        return wipe_args_map.get(wipe_type, '')

    def check_nvme_drive(self, device: str) -> PreCheckResult:
        result = PreCheckResult()
        try:
            id_ctrl = subprocess.run(
                ['nvme', 'id-ctrl', device, '-o', 'json'],
                check=False, capture_output=True, text=True, timeout=self.timeout
            )

            if id_ctrl.returncode != 0:
                result.issues['Unresponsive'] = "NVMe controller did not respond"
                return result

            data = json.loads(id_ctrl.stdout)

            # 1. Sanitize Support
            result.modes.update(Utils.parse_nvme_sanitize_capabilities(data))

            # 2. Format Support
            oncs = data.get('oncs', 0)
            if oncs & 0x04:  # Format NVM command supported
                fna = data.get('fna', 0)
                if fna & 0x04:
                    result.modes['FCrypto'] = 'format_crypto'
                result.modes['FErase'] = 'format_erase'

            if not result.modes:
                result.issues['Unsupported'] = "Drive lacks Sanitize or Format NVM capabilities"

        except Exception as e:
            result.issues['Error'] = f"NVMe Probe Exception: {str(e)}"

        return result

    def check_ata_drive(self, device: str) -> PreCheckResult:
        result = PreCheckResult()
        try:
            tool = SataTool(device)
            verdict = tool.get_wipe_verdict()
            if verdict == 'OK':
                # Populate Modes only if no fatal issues
                secures = tool.secures

                # ATA Security Erase modes
                if secures.enhanced_erase_supported:
                    result.modes['Enhanced'] = 'enhanced'
                result.modes['Erase'] = 'normal'

                # SATA Sanitize modes (if supported)
                if secures.sanitize_supported:
                    if secures.sanitize_crypto_supported:
                        result.modes['SCrypto'] = 'sanitize_crypto'
                    if secures.sanitize_block_supported:
                        result.modes['SBlock'] = 'sanitize_block'
                    if secures.sanitize_overwrite_supported:
                        result.modes['SOverwrite'] = 'sanitize_overwrite'
            elif verdict == 'DumbDevice':
                pass  # No security feature - don't report as error (e.g., USB thumb drive)
            else:
                result.issues[verdict] = verdict

        except Exception as e:
            result.issues['Error'] = f"ATA Probe Exception: {str(e)}"

        return result

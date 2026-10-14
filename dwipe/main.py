#!/usr/bin/env python3
"""
dwipe: curses-based tool to wipe physical disks or partitions including
  markers to know their state when wiped.
"""
# pylint: disable=too-many-branches,too-many-statements,import-outside-toplevel
# pylint: disable=too-many-instance-attributes,invalid-name
# pylint: disable=broad-exception-caught,consider-using-with
# pylint: disable=too-many-return-statements,too-many-locals

import os
import sys
import traceback

from .DiskWipe import DiskWipe
from .DeviceInfo import DeviceInfo
from .Utils import Utils
from .Prereqs import Prereqs
from .PersistentState import PersistentState


def main():
    """Main entry point"""
    import argparse
    from pathlib import Path

    # Load persistent state first so we can use previous values as defaults
    config_dir = Utils.get_config_dir()
    config_path = config_dir / 'state.json'
    persist = PersistentState(config_path=config_path)

    parser = argparse.ArgumentParser()
    parser.add_argument('--dump-lsblk', action='store_true',
                        help='dump parsed lsblk and exit for debugging')
    parser.add_argument('--mode', choices=['-V', '+V'], default=persist.state['wipe_mode'],
                        help='verification mode: -V (none) or +V (verify after wipe)')
    parser.add_argument('--passes', type=int, choices=[1, 2, 4], default=persist.state['passes'],
                        help='number of passes for software wipes (1, 2, or 4)')
    parser.add_argument('--verify-pct', type=int, choices=[1, 3, 10, 30, 100], default=persist.state['verify_pct'],
                        help='verification percentage after wipe (1, 3, 10, 30, or 100)')
    parser.add_argument('--port-serial', choices=['Auto', 'On', 'Off'], default=persist.state['port_serial'],
                        help='display mode for disk port/serial info (Auto, On, or Off)')
    parser.add_argument('--slowdown-stop', type=int, choices=[0, 4, 16, 64, 256], default=persist.state['slowdown_stop'],
                        help='stop wipe if disk slows down (0 = disabled, else check interval in ms)')
    parser.add_argument('--stall-timeout', type=int, choices=[0, 60, 120, 300, 600], default=persist.state['stall_timeout'],
                        help='stall timeout in seconds (0 = disabled)')
    parser.add_argument('--dense', choices=['True', 'False'], default='True' if persist.state['dense'] else 'False',
                        help='dense view: True (compact) or False (spaced lines)')
    parser.add_argument('--auto-block-containers', choices=['on', 'off'],
                        default='on' if persist.state['auto_block_containers'] else 'off',
                        help='auto-block LUKS/LVM/RAID/swap containers on first sight (on/off)')
    opts = parser.parse_args()

    # Convert string booleans to actual booleans
    opts.dense = opts.dense == 'True'
    opts.auto_block_containers = opts.auto_block_containers == 'on'

    dwipe = None  # Initialize to None so exception handler can reference it
    try:
        if os.geteuid() != 0:
            # Re-run the script with sudo needed and opted
            Utils.rerun_module_as_root('dwipe.main')

        prereqs = Prereqs(verbose=True)
        # blkid for filesystem detection; hdparm and nvme for firmware wipes
        prereqs.check_all(['blkid', 'hdparm', 'nvme'])
        prereqs.report_and_exit_if_failed()

        dwipe = DiskWipe(opts=opts, persistent_state=persist)

        dwipe.main_loop()
    except Exception as exce:
        # Try to clean up curses if it was initialized
        try:
            if dwipe and dwipe.win:
                dwipe.win.stop_curses()
        except Exception:
            pass  # Ignore errors during cleanup

        # Always print the error to ensure it's visible
        print("exception:", str(exce))
        print(traceback.format_exc())
        sys.exit(15)


if __name__ == "__main__":
    main()

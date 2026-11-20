#!/usr/bin/env python3
"""dwipe: TUI to wipe whole disks or partitions w persistent state markers."""

import os
import sys
import traceback

from console_window import cli

from . import __version__
from .DiskWipe import DiskWipe
from .PersistentState import PersistentState
from .Prereqs import Prereqs
from .Utils import Utils


def main():
    """Main entry point"""
    import argparse

    if '--version' in sys.argv[1:]:
        print(f'dwipe {__version__}')
        return

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
    cli.add_display_arguments(parser, prog='dwipe')
    opts = parser.parse_args()

    # Convert string booleans to actual booleans
    opts.dense = opts.dense == 'True'
    opts.auto_block_containers = opts.auto_block_containers == 'on'

    dwipe = None
    try:
        if os.geteuid() != 0: # Re-run the script with sudo needed and opted
            Utils.rerun_module_as_root('dwipe.main')

        cli.apply_display_arguments(opts, prog='dwipe')

        prereqs = Prereqs(verbose=True)
        prereqs.check_all(['blkid', 'hdparm', 'nvme'])
        prereqs.report_and_exit_if_failed()

        dwipe = DiskWipe(opts=opts, persistent_state=persist)

        dwipe.main_loop()
    except Exception as exce:
        try:
            if dwipe and dwipe.win:
                dwipe.win.stop_curses()
        except Exception:
            pass  # Ignore errors during cleanup

        print("exception:", str(exce))
        print(traceback.format_exc())
        sys.exit(15)


if __name__ == "__main__":
    main()

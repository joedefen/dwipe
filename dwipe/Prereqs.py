#!/usr/bin/env python3
import shutil
import sys


class Prereqs:
    """Manages tool dependencies and provides actionable install suggestions."""

    # Mapping tool binary to the actual package name for different managers
    TOOL_MAP = {
        'lsblk': {
            'apt': 'util-linux', 'dnf': 'util-linux', 'pacman': 'util-linux',
            'zypper': 'util-linux', 'apk': 'util-linux'
        },
        'hdparm': {
            'apt': 'hdparm', 'dnf': 'hdparm', 'pacman': 'hdparm',
            'zypper': 'hdparm', 'apk': 'hdparm'
        },
        'nvme': {
            'apt': 'nvme-cli', 'dnf': 'nvme-cli', 'pacman': 'nvme-cli',
            'zypper': 'nvme-cli', 'apk': 'nvme-cli'
        }
    }

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.pm = self._detect_package_manager()
        # Track status of required tools
        self.results = {}

    def _detect_package_manager(self) -> str | None:
        managers = ['apt', 'dnf', 'yum', 'pacman', 'zypper', 'apk', 'brew']
        for cmd in managers:
            if shutil.which(cmd):
                return cmd
        return None

    def check_all(self, tools: list[str]):
        """Runs the check for a list of tools."""
        for tool in tools:
            path = shutil.which(tool)
            self.results[tool] = path is not None
        return all(self.results.values())

    def get_install_hint(self, tool: str) -> str:
        """Returns a string suggesting how to fix the missing tool."""
        if not self.pm:
            return "Please install via your system's package manager."

        # Get package name (default to tool name if mapping missing)
        pkg = self.TOOL_MAP.get(tool, {}).get(self.pm, tool)

        commands = {
            'apt': f"sudo apt update && sudo apt install {pkg}",
            'dnf': f"sudo dnf install {pkg}",
            'yum': f"sudo yum install {pkg}",
            'pacman': f"sudo pacman -S {pkg}",
            'zypper': f"sudo zypper install {pkg}",
            'apk': f"apk add {pkg}",
            'brew': f"brew install {pkg}"
        }
        return commands.get(self.pm, f"Use {self.pm} to install {pkg}")

    def report_and_exit_if_failed(self):
        """Prints a clean summary to stdout. Exits if critical tools are missing."""
        print("\n--- System Prerequisite Check ---")
        failed = False

        for tool, available in self.results.items():
            mark = "✓" if available else "✗"
            status = "FOUND" if available else "MISSING"
            print(f"  {mark} {tool:<10} : {status}")

            if not available:
                failed = True
                print(f"    └─ Suggestion: {self.get_install_hint(tool)}")

        if failed:
            print("\nERROR: Dwipe cannot start until all prerequisites are met.\n")
            sys.exit(1)

        if self.verbose:
            print("All prerequisites satisfied.\n")
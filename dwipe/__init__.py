"""A curses-based tool to wipe physical disks and partitions on Linux.

The release number lives here and nowhere else.  ``[project]`` in
``pyproject.toml`` declares ``dynamic = ["version"]``, so the build backend
reads this line when it builds, ``./bless`` reads it to decide what to tag and
upload, ``dwipe --version`` prints it, and ``tests/inspect_dist.py`` checks the
built artifacts against it.
"""

__version__ = "3.2.0"

# zapdev
`zapdev` is yet-another-tool to wiped disks and partitions for Linux for security.

> **WORK-IN-PROGRESS**: soon this will be complete and deployed to PyPi.org. Until then, patiently await completion.

* Install `zapdev` using `pipx install zapdev`, or however you do so.


`zapman` features include:
* showing the disks and partitions that could be wiped along with useful information to help choose them (i.e., labels, sizes, and types)
* permit starting wipes and show their progress.
* filtering for devices by name in case of too many for one screen.
* stopping a wipe in progress.
* not offering to wipe disks that are mounted or would have conflicting/overlapping wipes in progress.
  
## Usage
* Run `zapdev` from the command line.

**TBD...**
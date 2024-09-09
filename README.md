# dwipe
`dwipe` to wipe disks and partitions for Linux for your security.

> **WORK-IN-PROGRESS**: soon this will be complete and deployed to PyPi.org. Until then, patiently await completion.

* Install `dwipe` using `pipx install dwipe`, or however you python scripts from PyPi.org.


`dwipe` features include:
* shows the disks and partitions that could be wiped along with useful information to help choose them (i.e., labels, sizes, and types)
* allows starting multiple wipes and shows their progress.
* allows filtering for devices by name in case of too many for one screen.
* allows stopping wipes in progress.
* not offering to wipe disks that are mounted or would have conflicting/overlapping wipes in progress.
* allowing to "lock" a disk to prevent mistaken wipes on that disk.
  
## Usage
* Run `dwipe` from the command line.

Here is a typical screen:

![dwipe-main](https://github.com/joedefen/dwipe/blob/main/resources/dwipe-main-screen.png?raw=true)

The possible state values and meaning are:
* **-** : indicates the device is ready for wiping if desired.
* **^** : similar to **-**, but also indicates the device was added after `dwipe` started
* **Mnt** :  the partition is mounted or the disk has partitions that are mounted.  You cannot wipe the device in this state.
* **N%** : if a percent is seen, then a wipe is in progress.
* **STOP** :  indicates that a wipe is being stopped.
* **s** :  indicates that a wipe was stopped (so the device is partly wiped) and wiping can be restarted.
* **W** :  indicates that a wipe was completed and wiping can be restarted.
* **Lock** :  indicates that the disk is manually locked, its partitions are hidden, and you cannot wipe the disk or its partitions.
* **Unlk** :  indicates that the disk was manually unlocked after a manual lock; this is a transitory state.

The top line shows available actions and other info. Some actions are context sensitive:
* **w** : **wipe** : wipe the selected device; before starting, you must confirm your intent.
* **s** : **stop** : stops the selected wipe in progress (can take a while).
* **S** : **Stop** : stops all wipes in progress (can take a while).
* **q** : **quit** : quits the app after stopping wipes in progress.
* **?** : **help** : bring up help screen with all actions and navigation keys explained.
* **/** : **search** : limits the show devices to those matching the given regex plus all wipes in progress.

The top line shows the "Mode" which is Random or Zeros. For some disks, zeroing may be faster than random.  Typing **r** toggles the mode (this is seen on the help screen). When Random, a wiped device is filled with random data and then the first 16KB is zeroed.

The write rate and estimating remaining times are shown when wiping a device.  Due to write queueing, the initial rates may be inflated, final rates are deflated, and the times are optimistic.


### The Help Screen
When **?** is typed, the help screen looks like:

![dwipe-help](https://github.com/joedefen/dwipe/blob/main/resources/dwipe-help-screen.png?raw=true)

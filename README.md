# dwipe
`dwipe` is tool to wipe disks and partitions for Linux helps secure you data. `dwipes` aims to reduce mistakes by providing ample information about your devices during selection.

> **Quick Start:**
> * Install `dwipe` using `pipx install dwipe`, or however you install python scripts from PyPi.org.
> * Run `dwipe` from a terminal and observe the context sensitive help on the 1st line.

To help with your disk scrubbing, `dwipe`:
* shows disks and partitions that can be wiped along with selected information to help choose them (i.e.,; labels, sizes, and types); disallowed are mounted devices and overlapping wipes and manually "locked" disks.
* updates the device list when it changes;  newly added devices are marked differently to make it easier to see them.
* supports starting multiple wipes, shows their progress, and shows completion states.
* supports either zeroing devices or filling with random data.
* supports filtering for devices by name/pattern in case of too many for one screen, etc.
* supports stopping wipes in progress.

`dwipe` shows file system labels, and if not the partition label.  It is best practice to label partitions and file systems well to make selection easier.
  
## Usage
> Simply run `dwipe` from the command line w/o arguments normally. Its command line arguments mostly for debugging including "--dry-run" which lets you test/practice the interface w/o risk.

Here is a typical screen:

![dwipe-help](https://raw.githubusercontent.com/joedefen/dwipe/master/resources/dwipe-main-screen.png?raw=true)

The possible state values and meanings are:
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

The 'W' (Wiped) and 's' (partly wiped) states are disk persistent.  For those states, more information is provided about the wipe including when and percent complete.


### The Help Screen
When **?** is typed, the help screen looks like:

![dwipe-help](https://raw.githubusercontent.com/joedefen/dwipe/master/resources/dwipe-help-screen.png?raw=true)

You can navigate the list of devices with arrow keys and vi-like keys.
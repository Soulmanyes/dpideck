# Mouse Settings for ATK / VXE mice on Linux

A small desktop app for changing the settings of a **VXE MAD R** (and probably other
ATK / VXE mice) on Linux. You don't need the ATK HUB website, so it works no matter
which browser you use. It works over the 2.4 GHz dongle and over the USB cable.

What it can change:

- DPI: 1 to 8 stages, 50 to 26000 DPI, plus the active stage
- polling rate (125 to 1000 Hz)
- sensor mode, lift-off distance, motion sync, angle snapping, ripple control
- click debounce and sleep timer
- button mapping: clicks, back/forward, DPI cycle/up/down, scrolling, disabled

It can also save and restore a backup, and do a factory reset.

## Install

```sh
git clone https://github.com/Soulmanyes/vxe-atk-linux
cd vxe-atk-linux
./install.sh
```

The script asks for your password once. It uses it to:

1. install Python, GTK 4 and libadwaita if they are missing (Arch, Debian/Ubuntu,
   Fedora and openSUSE are handled automatically);
2. add a udev rule, so the app can talk to the mouse without root.

The script installs the app for your user only. Afterwards, open **Mouse Settings**
from your app menu, or run `vxe-atk` in a terminal. If the mouse is not found,
unplug the mouse or its dongle and plug it back in.

To remove it:

```sh
./install.sh --uninstall
```

Your settings are stored in the mouse itself, so they stay after you uninstall,
and they work on Windows and other computers too.

## Use

- Each change is saved to the mouse immediately, then read back to check that it
  stuck. If it didn't stick, you see "Not saved" and the app shows what the mouse
  really has.
- A sleeping mouse doesn't answer. Move it, then press **Retry**.
- You can plug in the cable or the dongle while the app is open. It reconnects on
  its own within a few seconds.
- The MAD R has no DPI button. To switch DPI stages from the mouse, set a side
  button to **DPI cycle**.
- `python3 atk.py` prints all current settings in the terminal.

## Tested

Tested only on a **VXE MAD R** (firmware 3.00), over the 1K dongle and over the
cable.

- **Confirmed on the mouse:** polling rate, DPI stage values, number of stages,
  button mapping, backup and restore.
- **Not yet confirmed on hardware:** sensor mode, debounce, sleep timer, the three
  switches, factory reset. These use the same write-and-check path as the
  confirmed settings, so a failure shows up as "Not saved".
- **Lift-off distance:** the mouse stores a 0.7 mm setting, but it made no
  noticeable difference, so the app offers only 1 mm and 2 mm.

Other ATK / VXE mice that use the same "COMPX" protocol may work. The app checks the
DPI format before trusting it, but it is not tested on them. Mice with
8000 Hz polling or other sensors may need changes. Make a backup first (☰ menu).

## How it works

The app talks to the mouse through Linux `hidraw`, with the same 16-byte vendor
reports the ATK HUB website sends over WebHID. The protocol details come from
[OpenATK](https://github.com/SunnybeeXD/OpenATK), which documents them from ATK's
own web configurator.

## License

- `atk.py` contains protocol tables and DPI codecs ported from OpenATK, so it is
  under the MIT license. See `LICENSE-OpenATK`.
- Everything else is released into the public domain under CC0 1.0. See `LICENSE`.

This project is not affiliated with VXE, ATK or Compx. Product names are used only
to say which hardware it works with.

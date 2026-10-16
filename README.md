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

## Other mice

ATK-made mice (VXE, ATK, Dragonfly and others) use one of two protocols. Which
one depends on the mouse's hardware revision, not its model name. This app
supports only **COMPX**. It detects the protocol when the mouse is plugged in, and
tells you if a mouse uses the other one (**BITMOUSE**). For those mice, use
[OpenATK](https://github.com/SunnybeeXD/OpenATK) in Chrome or Chromium.

OpenATK's device list contains about 150 COMPX mice. None of them except the MAD R
have been tested with this app. How likely each one is to work depends on its
sensor:

| Sensor (DPI format) | Mice | What to expect |
|---|---|---|
| PAW3395 / PAW3950, like the MAD R | ~37 | Best chance: the same code paths as the tested mouse. |
| PAW3950 Ultra | ~46 | The DPI format is in the code but untested. DPI is capped at 26000, below these sensors' maximum. |
| PAW3955 | ~31 | DPI settings are greyed out, because these sensors store DPI elsewhere. Other settings may still work. |
| Unknown | ~36 | Unknown. |

All of them share these limits:

- The polling rate menu only offers up to 1000 Hz, so 2K/4K/8K rates can't be set.
- Only the first six buttons can be remapped.

If you try another mouse, make a backup first (☰ menu). Reports of what does and
doesn't work are welcome as GitHub issues.

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

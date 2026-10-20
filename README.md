# DPIdeck

Settings for ATK / VXE mice on Linux and Windows.

A small desktop app for changing the settings of ATK / VXE mice, such as the
**VXE MAD R**, on Linux and Windows. You don't need the ATK HUB website, so it works
no matter which browser you use. It works over the 2.4 GHz dongle and over the USB
cable.

What it can change:

- DPI: 1 to 8 stages, up to 50000 DPI depending on the sensor, plus the active stage
- polling rate, from 125 Hz up to 8000 Hz depending on the connection
- sensor mode, lift-off distance, motion sync, angle snapping, ripple control
- long range mode (stronger wireless signal, more battery use), on mice where
  ATK HUB offers it
- lighting: the DPI light (off, steady or breathing, brightness, speed) and a
  colour for each DPI stage; and the receiver's own light, on receivers that
  have one (the app asks the receiver, so it's hidden on receivers without one)
- click debounce and sleep timer
- button mapping: clicks, back/forward, DPI cycle/up/down, scrolling, disabled

It can also do a factory reset, and save and restore a backup (COMPX mice only).

## Windows

There is also a Windows version with the same features: download
`DPIdeck-windows.zip` from the
[Releases](https://github.com/Soulmanyes/dpideck/releases) page, unzip it and
run `DPIdeck.exe`. Nothing needs installing.

- Windows may say "Windows protected your PC" because the app isn't signed. Click
  "More info", then "Run anyway".
- Its caches and backups are kept in `%AppData%\dpideck`.

The Windows version is a separate program written in Go (`windows/`), built with
`windows/build.sh`. It shares no code with the Linux version, but `PARITY.md`
lists every behaviour both must have, and tests check that the two give the same
answers.

## Install (Linux)

```sh
git clone https://github.com/Soulmanyes/dpideck
cd dpideck
./install.sh
```

The script asks for your password once. It uses it to:

1. install Python, GTK 4 and libadwaita if they are missing (Arch, Debian/Ubuntu,
   Fedora and openSUSE are handled automatically);
2. add a udev rule, so the app can talk to the mouse without root.

The script installs the app for your user only. Afterwards, open **DPIdeck**
from your app menu, or run `dpideck` in a terminal. If the mouse is not found,
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

## Supported mice

ATK-made mice (VXE, ATK, Dragonfly and others) use one of two protocols, **COMPX**
and **BITMOUSE**. Which one depends on the mouse's hardware revision, not its model
name. The app supports both and picks the right one when the mouse is plugged in.

It identifies the mouse and its sensor from ATK HUB's own list of about 230 mice.
That list isn't part of this project: the app downloads it from ATK HUB the first
time it runs (see Privacy). It then offers what ATK HUB offers for that sensor:

| Sensor | DPI | Lift-off distance |
|---|---|---|
| PAW3395 / 3395 Ultra, like the MAD R | 100 to 30000 | 1 / 2 mm |
| PAW3950 / 3950DM | 50 to 36000 | 1 / 2 mm, plus 0.7 mm on COMPX mice |
| PAW3950 Ultra | 10 to 42000 | 0.7 to 1.7 mm in 0.1 mm steps |
| PAW3955 | 1 to 50000 | 0.7 to 1.7 mm in 0.1 mm steps |
| CORE26K | 50 to 26000 | 1 / 2 mm |
| PAW3395SE | 200 to 18000 (10000 on COMPX mice) | 1 / 2 mm |
| PAW3311 / PAW3315 | 200 to 15000 / 16000 (10000 on COMPX mice) | none |

On COMPX mice with the PAW3395SE, PAW3311 or PAW3315, DPI stops at 10000, because
ATK HUB's own code can't store higher values for them.

The polling rates offered are the ones the mouse supports. Like ATK HUB, the
app asks the mouse what it is connected through (1K/2K/4K/8K receiver or cable,
or NearLink) and offers rates up to that. A mouse that doesn't answer is offered
every rate.

Sensor mode offers Basic and Competitive on every mouse. Competitive Max appears
only where ATK HUB offers it: on COMPX mice it depends on the model and firmware
version (the app reads that list from ATK's server, see Privacy), and on BITMOUSE
mice on the model.

Only the first six buttons can be remapped.

## Tested

- **VXE MAD R (COMPX, PAW3395, firmware 3.00)**, over the 1K dongle and the cable:
  polling rate, DPI stage values up to 30000, number of stages, button mapping,
  sensor mode, backup and restore are confirmed. So are the DPI light and the
  colour of each DPI stage: the light follows the setting. Debounce, sleep timer, the three
  switches and factory reset use the same write-and-check path, so a failure shows
  up as "Not saved". The mouse stores 0.7 mm lift-off, but it made no noticeable
  difference, which matches ATK HUB offering only 1 and 2 mm on this sensor.
  The receiver light is untested: this mouse's dongle has no light. The Windows app
  was also checked with this mouse, over the dongle and the cable.
- **ATK Blazing Sky Phantom Ultimate**: a user confirmed it works with the Windows
  app. Which settings were checked one by one isn't recorded.
- **Every other mouse is untested.** The encodings
  follow ATK HUB 3.2.28's own code and OpenATK's documentation, and were checked
  against a simulated mouse, but not against real hardware. On BITMOUSE mice,
  backup and restore aren't available, and the number of DPI stages is shown but
  can't be changed, because how ATK HUB changes it there isn't known.

If you try another mouse:

1. Run `python3 atk.py` in the folder. It only reads, and prints what the app
   sees. A COMPX mouse can also be backed up from the ☰ menu.
2. Change one setting in the app, such as the polling rate, and change it back.
3. Report what does and doesn't work as a GitHub issue, with the output of step 1.

## Privacy

The app talks to ATK's servers for two things, and sends no information about you
or your mouse either time. As with any download, ATK's servers see your IP address.

- **The list of mice.** The installer, and later the app at most once every 30
  days, downloads ATK HUB's public web app from `hub.atk.pro` and
  `bpcdn.atkgear.com` (about 13 MB) and reads the list of mice and sensors out of
  it. The JavaScript is only read as text, never run. The list is cached in
  `~/.config/dpideck/devices.json`.
- **Which mice offer Competitive Max.** The app downloads
  `https://bpcdn.atkgear.com/device/constant/mouse-upgrade.json` the first time a
  mouse connects, then at most once a week, and caches it in the same folder.

Without a connection the app uses its cached copies. If it has never been able
to download the list, it can't tell which sensor a mouse has: DPI is then greyed
out on COMPX mice and Competitive Max is hidden, but every other setting works.
`python3 atk.py --update-devices` downloads the list again.

## How it works

The app talks to the mouse through Linux `hidraw`, with the same vendor reports the
ATK HUB website sends over WebHID. The protocol details come from
[OpenATK](https://github.com/SunnybeeXD/OpenATK), which documents them from ATK's
own web configurator, and were cross-checked against ATK HUB 3.2.28's own code.

## License

- `atk.py` and its Go port in `windows/core/` contain protocol tables and DPI
  codecs ported from OpenATK, so they are under the MIT license. See
  `LICENSE-OpenATK`.
- The Windows build includes the Walk UI library, its dependencies and the Go
  runtime (BSD and MIT licenses); their notices are in
  `windows/THIRD_PARTY_LICENSES.txt` and in the release zip.
- No data from ATK is included. The list of mice is downloaded from ATK HUB by
  each user's own copy of the app.
- Everything else is released into the public domain under CC0 1.0. See `LICENSE`.

This project is not affiliated with VXE, ATK or Compx. Product names are used only
to say which hardware it works with.

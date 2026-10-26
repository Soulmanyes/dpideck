#!/bin/sh
# Installs DPIdeck for the current user.
#   - installs Python/GTK 4/libadwaita if missing (asks for your password)
#   - installs a udev rule so the app can talk to the mouse without root
#   - copies the app to ~/.local/share/dpideck and adds a launcher
#   - if the Noctalia shell is installed, adds the DPIdeck bar widget to it
# Run ./install.sh --uninstall to remove it again.
# Without cloning the repo:
#   curl -fsSL https://raw.githubusercontent.com/Soulmanyes/dpideck/main/install.sh | sh
# The whole script is one { } block, so a download cut off midway runs nothing.
{
set -eu

NAME=dpideck
SRC=$(cd "$(dirname "$0")" && pwd)
DEST=${XDG_DATA_HOME:-$HOME/.local/share}/$NAME
APPS=${XDG_DATA_HOME:-$HOME/.local/share}/applications
BIN=$HOME/.local/bin
RULE=/etc/udev/rules.d/70-dpideck.rules
OLD_RULE=/etc/udev/rules.d/70-atk-mouse.rules   # before the rename to DPIdeck

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

# run through curl | sh: the repo isn't next to the script, so fetch it and run its installer
if [ ! -f "$SRC/70-dpideck.rules" ]; then
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    say "Downloading DPIdeck"
    # the latest release; main if there is none yet. releases/latest redirects to .../tag/<tag>
    tag=$(curl -fsSLo /dev/null -w '%{url_effective}' \
        https://github.com/Soulmanyes/dpideck/releases/latest | sed -n 's|.*/tag/||p')
    ref=${tag:+tags/$tag}
    curl -fsSL "https://github.com/Soulmanyes/dpideck/archive/refs/${ref:-heads/main}.tar.gz" |
        tar -xz -C "$tmp" --strip-components=1
    sh "$tmp/install.sh" "$@" < /dev/tty
    exit
fi

if [ "${1:-}" = "--uninstall" ]; then
    say "Removing DPIdeck"
    if command -v noctalia >/dev/null; then
        noctalia msg plugins disable soulmanyes/dpideck >/dev/null 2>&1 || true
        noctalia msg plugins source remove $NAME >/dev/null 2>&1 || true
    fi
    rm -rf "$DEST" "$APPS/$NAME.desktop" "$BIN/$NAME"
    if [ -e "$RULE" ] || [ -e "$OLD_RULE" ]; then
        sudo rm -f "$RULE" "$OLD_RULE"
        sudo udevadm control --reload
    fi
    say "Done. Your settings stay on the mouse."
    exit 0
fi

have_deps() {
    python3 - <<'EOF' 2>/dev/null
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 5)
EOF
}

if ! have_deps; then
    say "Installing Python, GTK 4 and libadwaita"
    if command -v pacman >/dev/null; then
        sudo pacman -S --needed --noconfirm python python-gobject gtk4 libadwaita
    elif command -v apt-get >/dev/null; then
        sudo apt-get update
        sudo apt-get install -y python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
    elif command -v dnf >/dev/null; then
        sudo dnf install -y python3 python3-gobject gtk4 libadwaita
    elif command -v zypper >/dev/null; then
        sudo zypper --non-interactive install python3 python3-gobject python3-gobject-Gdk \
            typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1
    else
        echo "Unknown package manager. Install Python 3, PyGObject, GTK 4 and" >&2
        echo "libadwaita 1.5 or newer yourself, then run this script again." >&2
        exit 1
    fi
    if ! have_deps; then
        echo "libadwaita 1.5 or newer is needed, but your system has an older one." >&2
        exit 1
    fi
fi

# skipped when the rule is already there, so the app's own updater (no terminal,
# so no password prompt) can run this script
if ! cmp -s "$SRC/70-dpideck.rules" "$RULE"; then
    say "Allowing your user to access ATK/VXE mice (udev rule)"
    sudo install -m 644 "$SRC/70-dpideck.rules" "$RULE"
    sudo rm -f "$OLD_RULE"
    sudo udevadm control --reload
    sudo udevadm trigger --subsystem-match=hidraw
fi

# remove an install from before the rename (it was called vxe-atk)
OLD=${XDG_DATA_HOME:-$HOME/.local/share}/vxe-atk
rm -rf "$OLD" "$APPS/vxe-atk.desktop" "$BIN/vxe-atk"

say "Installing the app to $DEST"
mkdir -p "$DEST" "$APPS" "$BIN"
install -m 644 "$SRC/atk.py" "$DEST/"
install -m 755 "$SRC/app.py" "$DEST/"
install -m 644 "$SRC/updater.py" "$DEST/"
rm -f "$DEST/devices.json"    # older versions shipped this; it is now downloaded
ln -sf "$DEST/app.py" "$BIN/$NAME"

say "Downloading the list of ATK mice from ATK HUB"
python3 "$DEST/atk.py" --update-devices ||
    echo "Couldn't download it now. The app will try again when it starts."

cat > "$APPS/$NAME.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=DPIdeck
Comment=DPI, polling rate and buttons for ATK / VXE mice
Exec=python3 $DEST/app.py
Icon=input-mouse
Categories=Settings;HardwareSettings;
Keywords=mouse;dpi;vxe;atk;
EOF

# Optional bar widget for the Noctalia shell. Without Noctalia this is skipped.
if command -v noctalia >/dev/null; then
    say "Adding the DPIdeck bar widget to Noctalia"
    rm -rf "$DEST/noctalia"
    cp -r "$SRC/noctalia" "$DEST/noctalia"
    # "add" fails if the source is already there from an earlier install
    if noctalia msg plugins source add $NAME path "$DEST/noctalia" >/dev/null 2>&1 ||
       noctalia msg plugins source list 2>/dev/null | grep -q "^$NAME "; then
        noctalia msg plugins enable soulmanyes/dpideck >/dev/null 2>&1 || true
        echo "Put the \"DPIdeck\" widget on your bar in Noctalia's bar settings."
    else
        echo "Noctalia isn't running, so the widget wasn't added. To add it later,"
        echo "start Noctalia and run:"
        echo "  noctalia msg plugins source add $NAME path \"$DEST/noctalia\""
        echo "  noctalia msg plugins enable soulmanyes/dpideck"
    fi
fi

say "Done. Open \"DPIdeck\" from your app menu, or run: $NAME"
echo "If the mouse is not found, unplug it (or its dongle) and plug it back in."
}

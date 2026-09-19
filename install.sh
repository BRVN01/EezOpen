#!/usr/bin/env bash

#
# EezOpen Tools Installer
#
# Installs EezTop and EezNet for the current user.
#
# Author: Bruno Dias da Silva
# Project: EezOpen
# License: MIT
#

set -euo pipefail

VERSION="1.1.0"
SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CURRENT_USER="$(id -un)"
SERIAL_GROUP="dialout"

if (( EUID == 0 )); then
    echo "Run install.sh as your normal desktop user, not as root." >&2
    exit 1
fi

for required in eezopen-daemon.py eezopen-config.py eezopen.service create_bg.py; do
    if [[ ! -f "$SRC_DIR/$required" ]]; then
        echo "Missing required file: $required" >&2
        exit 1
    fi
done

if ! python3 - <<'PY' >/dev/null 2>&1
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk
PY
then
    echo "GTK4/PyGObject was not found."
    echo "On Ubuntu/Debian, install it first:"
    echo "  sudo apt install python3-gi gir1.2-gtk-4.0 xdg-utils udisks2"
    exit 1
fi

if ! python3 - <<'PY' >/dev/null 2>&1
from PIL import Image, ImageOps
PY
then
    echo "Pillow was not found; it is required for background-image conversion."
    echo "On Ubuntu/Debian, install it first:"
    echo "  sudo apt install python3-pil"
    exit 1
fi

if ! command -v udisksctl >/dev/null 2>&1; then
    echo "WARNING: udisksctl was not found."
    echo "Configurator operations that need MacroPad USB Mass Storage will not be able to mount it automatically."
    echo "On Ubuntu/Debian: sudo apt install udisks2"
fi

install -d -m 0755 "$HOME/.local/bin"
install -d -m 0755 "$HOME/.local/lib/eezopen"
install -d -m 0755 "$HOME/.config/systemd/user"
install -d -m 0755 "$HOME/.local/share/applications"
install -d -m 0755 "$HOME/.local/share/icons/hicolor/scalable/apps"
install -d -m 0755 "$HOME/.local/share/EezOpen/assets"

install -m 0755 "$SRC_DIR/eezopen-daemon.py" "$HOME/.local/bin/eezopen-daemon"
install -m 0755 "$SRC_DIR/eezopen-config.py" "$HOME/.local/bin/eezopen-config"
install -m 0644 "$SRC_DIR/create_bg.py" "$HOME/.local/lib/eezopen/create_bg.py"
install -m 0644 "$SRC_DIR/eezopen.service" "$HOME/.config/systemd/user/eezopen.service"

if [[ -f "$SRC_DIR/eezopen.svg" ]]; then
    install -m 0644 "$SRC_DIR/eezopen.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/eezopen.svg"
fi
if compgen -G "$SRC_DIR/assets/*.png" >/dev/null; then
    install -m 0644 "$SRC_DIR"/assets/*.png "$HOME/.local/share/EezOpen/assets/"
fi

rm -f "$HOME/.local/share/applications/eezopen-config.desktop"
cat > "$HOME/.local/share/applications/io.github.eezopen.Config.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=EezOpen
Comment=Configure EezBotFun MacroPad on Linux
Exec=$HOME/.local/bin/eezopen-config
Icon=eezopen
Terminal=false
Categories=Utility;Settings;
StartupNotify=true
DESKTOP

systemctl --user daemon-reload

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
fi
if command -v gtk4-update-icon-cache >/dev/null 2>&1; then
    gtk4-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
elif command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
fi

# Current EezOpen uses the standard Linux serial-access group. Remove only
# obsolete EezOpen-owned rules from older releases. Never touch vendor rules and
# never trigger or re-enumerate the USB device during installation.
LEGACY_RULES=(
    /etc/udev/rules.d/69-eezopen-macropad.rules
    /etc/udev/rules.d/70-eezopen-macropad.rules
    /etc/udev/rules.d/99-eezopen-usb.rules
)
removed_rule=0
for rule in "${LEGACY_RULES[@]}"; do
    if [[ -f "$rule" ]]; then
        sudo rm -f -- "$rule"
        removed_rule=1
    fi
done
if (( removed_rule )); then
    sudo udevadm control --reload-rules
fi

needs_relogin=0
if getent group "$SERIAL_GROUP" >/dev/null 2>&1; then
    if ! id -nG "$CURRENT_USER" | tr ' ' '\n' | grep -qx "$SERIAL_GROUP"; then
        echo "Adding $CURRENT_USER to $SERIAL_GROUP for CDC serial access..."
        sudo usermod -aG "$SERIAL_GROUP" "$CURRENT_USER"
        needs_relogin=1
    fi
else
    echo "WARNING: group '$SERIAL_GROUP' does not exist on this system." >&2
fi

echo
echo "EezOpen v$VERSION installed."
echo
echo "USB policy:"
echo "  - no EezOpen udev permission rule is installed"
echo "  - standard '$SERIAL_GROUP' group is used for CDC serial access"
echo "  - no udevadm trigger"
echo "  - no authorized/unbind/RUN rules"
echo "  - the daemon does not recursively scan MacroPad Mass Storage on connection"
echo
if (( needs_relogin )); then
    echo "IMPORTANT: log out and back in before starting the user service so the session inherits '$SERIAL_GROUP'."
    echo "For a temporary foreground shell only, you may use: newgrp $SERIAL_GROUP"
else
    echo "Serial group membership is already available in this login session."
fi
echo
echo "Start/enable the daemon:"
echo "  systemctl --user enable --now eezopen.service"
echo
echo "Open the configurator:"
echo "  $HOME/.local/bin/eezopen-config"
echo
echo "Daemon logs:"
echo "  journalctl --user -u eezopen.service -f"

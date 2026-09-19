#!/usr/bin/env bash

#
# EezOpen Tools Uninstaller
#
# Removes EezTop and EezNet installed for the current user.
#
# Author: Bruno Dias da Silva
# Project: EezOpen
# License: MIT
#

set -euo pipefail

PURGE=0
if [[ "${1:-}" == "--purge" ]]; then
    PURGE=1
elif [[ $# -gt 0 ]]; then
    echo "Usage: $0 [--purge]" >&2
    exit 2
fi

SERVICE="$HOME/.config/systemd/user/eezopen.service"
DAEMON="$HOME/.local/bin/eezopen-daemon"
CONFIG="$HOME/.local/bin/eezopen-config"
DESKTOP="$HOME/.local/share/applications/io.github.eezopen.Config.desktop"
LEGACY_DESKTOP="$HOME/.local/share/applications/eezopen-config.desktop"
ICON="$HOME/.local/share/icons/hicolor/scalable/apps/eezopen.svg"
DATA_DIR="$HOME/.local/share/EezOpen"
ASSETS_DIR="$DATA_DIR/assets"
HELPER_DIR="$HOME/.local/lib/eezopen"

if systemctl --user list-unit-files eezopen.service >/dev/null 2>&1 || [[ -f "$SERVICE" ]]; then
    systemctl --user disable --now eezopen.service >/dev/null 2>&1 || true
fi

rm -f -- "$SERVICE" "$DAEMON" "$CONFIG" "$DESKTOP" "$LEGACY_DESKTOP" "$ICON"
rm -rf -- "$ASSETS_DIR" "$HELPER_DIR"

systemctl --user daemon-reload >/dev/null 2>&1 || true
systemctl --user reset-failed eezopen.service >/dev/null 2>&1 || true

# Current releases do not install an EezOpen udev rule, but clean up rule files
# left by older EezOpen versions. Never remove the vendor's own rules and never
# trigger/re-enumerate USB here.
removed_rule=0
for rule in \
    /etc/udev/rules.d/69-eezopen-macropad.rules \
    /etc/udev/rules.d/70-eezopen-macropad.rules \
    /etc/udev/rules.d/99-eezopen-usb.rules; do
    if [[ -f "$rule" ]]; then
        sudo rm -f -- "$rule"
        removed_rule=1
    fi
done
if (( removed_rule )); then
    sudo udevadm control --reload-rules
fi

# Do not remove the user from dialout: it is a standard system group and may be
# required by unrelated serial devices/applications.
if (( PURGE )); then
    rm -rf -- "$DATA_DIR"
    echo "Removed EezOpen user data, profiles, cached icons, settings, and legacy EezOpen state."
else
    if [[ -d "$DATA_DIR" ]]; then
        echo "Preserved user data: $DATA_DIR"
        echo "Use '$0 --purge' to remove it too."
    fi
fi

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
fi
if command -v gtk4-update-icon-cache >/dev/null 2>&1; then
    gtk4-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
elif command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" >/dev/null 2>&1 || true
fi

echo "EezOpen uninstalled."

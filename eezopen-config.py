#!/usr/bin/env python3

"""
EezOpen Config v2.2.3 - GTK4 configurator - For the EezBotFun 8-Key MacroPad

Part of the EezOpen project.

Author: Bruno Dias da Silva
Project: EezOpen
Description:
    Displays real-time Linux system information on the MacroPad LCD
    using the EezBotFun Customised Display USB CDC protocol.

Copyright (c) 2026 Bruno Dias da Silva

Licensed under the MIT License.
See LICENSE for details.
"""

from __future__ import annotations

import json
import os
import socket
import shlex
import subprocess
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

APP_ID = "io.github.eezopen.Config"
ACTION_NAMES = ["Open URL", "Launch application", "Open folder", "Open file", "External Script"]
ACTION_IDS = ["1", "2", "3", "f", "p"]
ACTION_SHORT = {"1": "URL", "2": "APP", "3": "FOLDER", "f": "FILE", "p": "SCRIPT"}
DEFAULT_ICON_FILES = {
    "1": "action-url.png",
    "2": "action-app.png",
    "3": "action-folder.png",
    "f": "action-file.png",
}

# Complete special-key vocabulary published by EezBotFun for firmware scripts.
# Ordinary letters/digits are captured as part of Shortcut actions instead.
HID_CONSUMER_TOKENS = {"VOLUP", "VOLDOWN", "MUTE", "PREV", "NEXT", "PP", "STOP"}
HID_MEDIA_TO_SCRIPT = {token: f"MK_{token}" for token in HID_CONSUMER_TOKENS}
HID_SCRIPT_TO_MEDIA = {script: token for token, script in HID_MEDIA_TO_SCRIPT.items()}
HID_KEY_TOKENS = [
    "CONTROL", "SHIFT", "ALT", "WINDOWS", "COMMAND", "OPTION",
    "ESC", "ENTER", "UP", "DOWN", "LEFT", "RIGHT", "SPACE",
    "BACKSPACE", "TAB", "CAPLOCKS", "INSERT", "DELETE", "HOME",
    "END", "PAGEUP", "PAGEDOWN",
    "PRINTSCREEN", "SCROLLLOCK", "PAUSE", "BREAK", "MENU", "POWER",
    *[f"F{i}" for i in range(1, 25)],
    "VOLUP", "VOLDOWN", "MUTE", "PREV", "NEXT", "PP", "STOP",
    "NUMLOCK", "KP_SLASH", "KP_ASTERISK", "KP_MINUS", "KP_PLUS",
    "KP_ENTER", "KP_DOT", "KP_EQUAL",
    *[f"KP_{i}" for i in range(10)],
]
HID_ACTION_NAMES = ["Shortcut", "Key", "Input Text", "Wait"]
HID_ACTION_TYPES = ["shortcut", "key", "text", "wait"]



def default_icon_path(action: str) -> str | None:
    name = DEFAULT_ICON_FILES.get(action)
    if not name:
        return None
    candidates = [
        Path.home() / ".local" / "share" / "EezOpen" / "assets" / name,
        Path(__file__).resolve().parent / "assets" / name,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def ipc_socket_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    return (Path(base) / "eezopen" / "daemon.sock") if base else Path("/tmp") / f"eezopen-{os.getuid()}" / "daemon.sock"


def ipc_request(payload: dict, timeout: float = 3.0) -> dict:
    path = ipc_socket_path()
    if not path.exists():
        raise ConnectionError("EezOpen daemon is not running")
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(data)
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 1024 * 1024:
                raise RuntimeError("IPC response is too large")
    if not buf:
        raise RuntimeError("Daemon closed the connection without replying")
    response = json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "Unknown daemon error"))
    return response


def ipc_async(payload: dict, on_success, on_error, timeout: float = 3.0) -> None:
    """Run blocking IPC off the GTK main loop and marshal the result back."""
    def worker():
        try:
            result = ipc_request(payload, timeout=timeout)
        except Exception as exc:
            GLib.idle_add(on_error, exc)
        else:
            GLib.idle_add(on_success, result)
    threading.Thread(target=worker, name="eezopen-gui-ipc", daemon=True).start()


def clear_box(box: Gtk.Box) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def set_widget_description(widget: Gtk.Widget, text: str) -> None:
    """Attach a native GTK hover tooltip without resetting its hover timer."""
    target = str(text or "").strip() or None
    # set_tooltip_text() already manages GtkWidget:has-tooltip.  Avoid writing
    # the same value repeatedly: update_status() runs every 250 ms and resetting
    # the tooltip while the pointer is hovering prevents GTK's tooltip delay
    # from ever expiring.
    if widget.get_tooltip_text() != target:
        widget.set_tooltip_text(target)

def show_error_dialog(parent: Gtk.Window, title: str, message: object) -> None:
    """Show an unmistakable modal error for a user-initiated operation.

    The status/notice labels remain useful as a history of the last operation,
    but failures that require user action must not be visible only there (or only
    in the daemon journal).
    """
    detail = str(message or "Unknown error").strip() or "Unknown error"
    dialog = Gtk.MessageDialog(
        transient_for=parent,
        modal=True,
        message_type=Gtk.MessageType.ERROR,
        buttons=Gtk.ButtonsType.CLOSE,
        text=f"{title}\n\n{detail}",
    )
    dialog.connect("response", lambda d, _response: d.destroy())
    dialog.present()


def screen_script_storage_error(status: dict) -> str | None:
    """Return a user-facing reason when a Screen Script owns device I/O."""
    if not bool(status.get("screen_script_active")):
        return None
    name = str(status.get("screen_script_name") or "Screen Script").strip() or "Screen Script"
    return (
        f"{name} is currently running. Stop the Screen Script before changing "
        "MacroPad files or settings that require USB Mass Storage."
    )

BACKGROUND_TMP_DIR = Path("/tmp/eezopen")
BACKGROUND_FILENAMES = {"dark": "bg_dark.bin", "light": "bg_light.bin"}


def background_converter_path() -> Path:
    """Locate the create_bg.py helper installed with EezOpen or beside this source file."""
    candidates = [
        Path.home() / ".local" / "lib" / "eezopen" / "create_bg.py",
        Path(__file__).resolve().with_name("create_bg.py"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("create_bg.py was not found; reinstall EezOpen with the background converter helper")


def prepare_background_tmp_dir() -> Path:
    """Create the requested /tmp/eezopen workspace without trusting a foreign path."""
    if BACKGROUND_TMP_DIR.exists():
        if BACKGROUND_TMP_DIR.is_symlink():
            raise RuntimeError(f"Refusing symlink temporary directory: {BACKGROUND_TMP_DIR}")
        st = BACKGROUND_TMP_DIR.stat()
        if st.st_uid != os.getuid():
            raise RuntimeError(f"Temporary directory is not owned by the current user: {BACKGROUND_TMP_DIR}")
    else:
        BACKGROUND_TMP_DIR.mkdir(mode=0o700, parents=False)
    BACKGROUND_TMP_DIR.chmod(0o700)
    return BACKGROUND_TMP_DIR

def _modifier_mask(state, name: str) -> bool:
    value = getattr(Gdk.ModifierType, name, 0)
    try:
        return bool(int(state) & int(value))
    except (TypeError, ValueError):
        return False


def _gdk_key_to_hid_token(keyval: int) -> str | None:
    name = Gdk.keyval_name(keyval) or ""
    mapping = {
        "Escape": "ESC", "Return": "ENTER", "KP_Enter": "KP_ENTER",
        "Up": "UP", "Down": "DOWN", "Left": "LEFT", "Right": "RIGHT",
        "space": "SPACE", "BackSpace": "BACKSPACE", "Tab": "TAB",
        "ISO_Left_Tab": "TAB", "Caps_Lock": "CAPLOCKS", "Insert": "INSERT",
        "Delete": "DELETE", "Home": "HOME", "End": "END",
        "Page_Up": "PAGEUP", "Page_Down": "PAGEDOWN", "Print": "PRINTSCREEN",
        "Scroll_Lock": "SCROLLLOCK", "Pause": "PAUSE", "Menu": "MENU",
        "KP_Divide": "KP_SLASH", "KP_Multiply": "KP_ASTERISK",
        "KP_Subtract": "KP_MINUS", "KP_Add": "KP_PLUS",
        "KP_Decimal": "KP_DOT", "KP_Equal": "KP_EQUAL",
    }
    if name in mapping:
        return mapping[name]
    if name.startswith("F") and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:
        return name.upper()
    if name.startswith("KP_") and name[3:].isdigit() and 0 <= int(name[3:]) <= 9:
        return name.upper()
    codepoint = Gdk.keyval_to_unicode(keyval)
    if codepoint:
        char = chr(codepoint)
        if char.isprintable() and not char.isspace():
            # Official examples/captures use lower-case ordinary letter tokens;
            # SHIFT is represented explicitly as a modifier.
            if "A" <= char <= "Z":
                char = char.lower()
            return char
    return None


class HidActionRow:
    def __init__(self, editor: "KeyEditor", data: dict | None = None):
        self.editor = editor
        self.data = dict(data or {})
        self.capture_active = False
        self.capture_modifiers: set[str] = set()

        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.box.set_hexpand(True)

        # One key controller per row. In CAPTURE phase it sees key events before
        # Gtk.Entry/default shortcuts can consume them. It is inert unless the
        # user explicitly clicked Capture.
        self.capture_controller = Gtk.EventControllerKey()
        self.capture_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self.capture_controller.connect("key-pressed", self.on_capture_key)
        self.capture_controller.connect("key-released", self.on_capture_key_released)
        self.box.add_controller(self.capture_controller)

        self.type_dropdown = Gtk.DropDown.new_from_strings(HID_ACTION_NAMES)
        set_widget_description(self.type_dropdown, "Choose the HID action type for this row")
        initial_type = str(self.data.get("type", "shortcut"))
        self.type_dropdown.set_selected(HID_ACTION_TYPES.index(initial_type) if initial_type in HID_ACTION_TYPES else 0)
        self.box.append(self.type_dropdown)

        self.value_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.value_box.set_hexpand(True)
        self.box.append(self.value_box)

        up = Gtk.Button(label="↑"); set_widget_description(up, "Move up")
        down = Gtk.Button(label="↓"); set_widget_description(down, "Move down")
        remove = Gtk.Button(label="Remove"); set_widget_description(remove, "Remove this HID action")
        up.connect("clicked", lambda *_: self.editor.move_hid_action(self, -1))
        down.connect("clicked", lambda *_: self.editor.move_hid_action(self, 1))
        remove.connect("clicked", lambda *_: self.editor.remove_hid_action(self))
        self.box.append(up); self.box.append(down); self.box.append(remove)

        self.type_dropdown.connect("notify::selected", self.on_type_changed)
        self.build_value_editor()

    def action_type(self) -> str:
        idx = int(self.type_dropdown.get_selected())
        return HID_ACTION_TYPES[idx] if 0 <= idx < len(HID_ACTION_TYPES) else "shortcut"

    def on_type_changed(self, *_args):
        self.data = {}
        self.capture_active = False
        self.build_value_editor()

    def build_value_editor(self):
        clear_box(self.value_box)
        kind = self.action_type()

        if kind == "shortcut":
            self.shortcut_entry = Gtk.Entry()
            self.shortcut_entry.set_hexpand(True)
            self.shortcut_entry.set_placeholder_text("CONTROL ALT t"); set_widget_description(self.shortcut_entry, "Shortcut tokens sent to the firmware, for example CONTROL ALT t")
            self.shortcut_entry.set_text(str(self.data.get("value", "")))
            self.value_box.append(self.shortcut_entry)

            self.capture_button = Gtk.Button(label="Capture")
            set_widget_description(self.capture_button, "Click and press the key combination")
            self.capture_button.connect("clicked", self.start_capture)
            self.value_box.append(self.capture_button)

        elif kind == "key":
            current = str(self.data.get("value", "ENTER")).upper()
            values = list(HID_KEY_TOKENS)
            if current and current not in values:
                values.append(current)
            self.key_dropdown = Gtk.DropDown.new_from_strings(values)
            set_widget_description(self.key_dropdown, "Choose a firmware HID key or media key")
            self.key_dropdown.set_hexpand(True)
            self.key_values = values
            self.key_dropdown.set_selected(values.index(current) if current in values else 0)
            self.value_box.append(self.key_dropdown)

        elif kind == "text":
            self.text_entry = Gtk.Entry()
            self.text_entry.set_hexpand(True)
            self.text_entry.set_placeholder_text("Text to type"); set_widget_description(self.text_entry, "Text that the MacroPad firmware will type")
            self.text_entry.set_text(str(self.data.get("text", "")))
            self.value_box.append(self.text_entry)
            self.enter_check = Gtk.CheckButton(label="Press Enter at the end")
            set_widget_description(self.enter_check, "Append Enter after typing the configured text")
            self.enter_check.set_active(bool(self.data.get("enter", False)))
            self.value_box.append(self.enter_check)

        else:
            self.wait_entry = Gtk.Entry()
            self.wait_entry.set_hexpand(True)
            self.wait_entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
            self.wait_entry.set_text(str(self.data.get("ms", 300)))
            self.wait_entry.set_placeholder_text("300"); set_widget_description(self.wait_entry, "Delay in milliseconds before the next HID action")
            self.value_box.append(self.wait_entry)
            self.value_box.append(Gtk.Label(label="ms"))

    def _set_system_shortcuts_inhibited(self, inhibit: bool) -> None:
        # Best effort: on compositors that implement GDK's shortcut-inhibit
        # protocol, this keeps combinations such as Ctrl+Alt+T in EezOpen while
        # capture is active instead of letting the desktop consume them first.
        try:
            surface = self.editor.get_surface()
            if surface is None:
                return
            if inhibit and hasattr(surface, "inhibit_system_shortcuts"):
                surface.inhibit_system_shortcuts(None)
            elif not inhibit and hasattr(surface, "restore_system_shortcuts"):
                surface.restore_system_shortcuts()
        except Exception:
            # Some GTK/backend combinations do not support shortcut inhibition.
            # Local GTK capture still works; do not break the editor.
            pass

    def start_capture(self, *_args):
        self.capture_active = True
        self.capture_modifiers.clear()
        self._set_system_shortcuts_inhibited(True)
        self.capture_button.set_label("Press keys…")
        self.shortcut_entry.set_placeholder_text("Press the shortcut now")
        self.shortcut_entry.grab_focus()

    @staticmethod
    def _capture_modifier_token(keyval: int) -> str | None:
        name = Gdk.keyval_name(keyval) or ""
        if name in {"Control_L", "Control_R"}:
            return "CONTROL"
        if name in {"Shift_L", "Shift_R"}:
            return "SHIFT"
        if name in {"Alt_L", "Alt_R", "ISO_Level3_Shift"}:
            return "ALT"
        if name in {"Super_L", "Super_R", "Meta_L", "Meta_R", "Hyper_L", "Hyper_R"}:
            return "WINDOWS"
        return None

    def _capture_state_modifiers(self, state) -> set[str]:
        modifiers: set[str] = set()
        if _modifier_mask(state, "CONTROL_MASK"):
            modifiers.add("CONTROL")
        if _modifier_mask(state, "SHIFT_MASK"):
            modifiers.add("SHIFT")
        if _modifier_mask(state, "ALT_MASK") or _modifier_mask(state, "MOD1_MASK"):
            modifiers.add("ALT")
        if (_modifier_mask(state, "SUPER_MASK") or _modifier_mask(state, "META_MASK")
                or _modifier_mask(state, "MOD4_MASK")):
            modifiers.add("WINDOWS")
        return modifiers

    def on_capture_key(self, _controller, keyval, _keycode, state):
        if not self.capture_active:
            return False

        modifier = self._capture_modifier_token(keyval)
        if modifier:
            self.capture_modifiers.add(modifier)
            ordered = [m for m in ("CONTROL", "SHIFT", "ALT", "WINDOWS")
                       if m in self.capture_modifiers]
            self.shortcut_entry.set_text(" ".join(ordered))
            return True

        token = _gdk_key_to_hid_token(keyval)
        if not token:
            name = Gdk.keyval_name(keyval) or ""
            self.editor.status_label.set_text(f"Key not recognized for capture: {name or keyval}")
            return True

        modifiers = self.capture_modifiers | self._capture_state_modifiers(state)
        parts = [m for m in ("CONTROL", "SHIFT", "ALT", "WINDOWS") if m in modifiers]
        if token not in parts:
            parts.append(token)

        self.shortcut_entry.set_text(" ".join(parts))
        self.capture_active = False
        self.capture_modifiers.clear()
        self._set_system_shortcuts_inhibited(False)
        self.capture_button.set_label("Capture")
        return True

    def on_capture_key_released(self, _controller, keyval, _keycode, _state):
        if not self.capture_active:
            return
        modifier = self._capture_modifier_token(keyval)
        if modifier:
            self.capture_modifiers.discard(modifier)

    def payload(self) -> dict:
        kind = self.action_type()
        if kind == "shortcut":
            value = " ".join(self.shortcut_entry.get_text().split())
            if not value:
                raise ValueError("Shortcut cannot be empty")
            return {"type": "shortcut", "value": value}
        if kind == "key":
            idx = int(self.key_dropdown.get_selected())
            value = self.key_values[idx] if 0 <= idx < len(self.key_values) else ""
            if not value:
                raise ValueError("HID key cannot be empty")
            return {"type": "key", "value": value}
        if kind == "text":
            text = self.text_entry.get_text()
            if not text:
                raise ValueError("Input Text cannot be empty")
            return {"type": "text", "text": text, "enter": self.enter_check.get_active()}
        value = self.wait_entry.get_text().strip()
        if not value.isdigit():
            raise ValueError("Wait must be an integer number of milliseconds")
        return {"type": "wait", "ms": int(value)}


class KeyEditor(Gtk.Window):
    def __init__(self, parent: "MainWindow", profile: int, key: int, cfg: dict | None):
        super().__init__(title=f"Profile {profile} · Key {key}", transient_for=parent, modal=True)
        self.parent_window = parent
        self.profile = profile
        self.key = key
        self.cfg = cfg or {}
        self.icon_source = str(self.cfg.get("icon_path") or "")
        self.icon_changed = False
        self.clear_icon = False
        self._chooser = None
        self._icon_chooser = None
        self.hid_rows: list[HidActionRow] = []
        self.unsupported_hid_actions: list[dict] = []
        self.set_default_size(520, 390)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_child(scroll)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(18); outer.set_margin_bottom(18)
        outer.set_margin_start(18); outer.set_margin_end(18)
        scroll.set_child(outer)

        title = Gtk.Label(); title.set_markup(f"<b>Profile {profile} · Key {key}</b>"); title.set_xalign(0); outer.append(title)

        header_grid = Gtk.Grid(column_spacing=10, row_spacing=10); outer.append(header_grid)
        lbl = Gtk.Label(label="Alias:"); lbl.set_xalign(1)
        self.alias_entry = Gtk.Entry(); self.alias_entry.set_hexpand(True); self.alias_entry.set_text(str(self.cfg.get("alias", "")))
        set_widget_description(self.alias_entry, "Optional name shown for this key on the MacroPad")
        header_grid.attach(lbl, 0, 0, 1, 1); header_grid.attach(self.alias_entry, 1, 0, 2, 1)

        mode_label = Gtk.Label(label="Mode:"); mode_label.set_xalign(1)
        self.hid_check = Gtk.CheckButton(label="HID Mode (executed by firmware)")
        set_widget_description(self.hid_check, "Enable firmware-executed keyboard/HID actions instead of host-side actions")
        self.hid_check.set_active(self.cfg.get("hid") is True)
        self.hid_check.connect("toggled", self.on_mode_toggled)
        header_grid.attach(mode_label, 0, 1, 1, 1); header_grid.attach(self.hid_check, 1, 1, 2, 1)

        # Existing Non-HID editor is kept intact and merely hidden while HID Mode is active.
        self.nonhid_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10); outer.append(self.nonhid_box)
        grid = Gtk.Grid(column_spacing=10, row_spacing=10); self.nonhid_box.append(grid)
        lbl = Gtk.Label(label="Action:"); lbl.set_xalign(1)
        self.action_dropdown = Gtk.DropDown.new_from_strings(ACTION_NAMES)
        set_widget_description(self.action_dropdown, "Choose what this Non-HID key does on the Linux host")
        current_act = str(self.cfg.get("act", ""))
        self.action_dropdown.set_selected(ACTION_IDS.index(current_act) if current_act in ACTION_IDS else 0)
        self.action_dropdown.connect("notify::selected", self.on_action_changed)
        grid.attach(lbl, 0, 0, 1, 1); grid.attach(self.action_dropdown, 1, 0, 2, 1)

        self.arg_label = Gtk.Label(label="Target:"); self.arg_label.set_xalign(1)
        self.arg_entry = Gtk.Entry(); self.arg_entry.set_hexpand(True); self.arg_entry.set_text(str(self.cfg.get("arg", "")) if self.cfg.get("hid") is not True else "")
        set_widget_description(self.arg_entry, "Target used by the selected action")
        self.browse_button = Gtk.Button(label="Browse…"); set_widget_description(self.browse_button, "Choose the folder or file for this action"); self.browse_button.connect("clicked", self.on_browse)
        grid.attach(self.arg_label, 0, 1, 1, 1); grid.attach(self.arg_entry, 1, 1, 1, 1); grid.attach(self.browse_button, 2, 1, 1, 1)

        self.external_script_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8); self.nonhid_box.append(self.external_script_box)
        script_title = Gtk.Label(); script_title.set_markup("<b>External Script</b>"); script_title.set_xalign(0); self.external_script_box.append(script_title)
        script_hint = Gtk.Label(label="Runs an external executable or script directly with shell=False. On the MacroPad this is stored as the standard ACT 2 action; EezOpen keeps the External Script distinction only in its HOME cache. Arguments are parsed as argv; quotes may group values with spaces.")
        script_hint.set_xalign(0); script_hint.set_wrap(True); script_hint.add_css_class("dim-label"); self.external_script_box.append(script_hint)
        script_grid = Gtk.Grid(column_spacing=10, row_spacing=10); self.external_script_box.append(script_grid)
        script_label = Gtk.Label(label="Script:"); script_label.set_xalign(1); script_grid.attach(script_label, 0, 0, 1, 1)
        self.external_script_entry = Gtk.Entry(); self.external_script_entry.set_hexpand(True); self.external_script_entry.set_placeholder_text("/home/user/scripts/action.py")
        set_widget_description(self.external_script_entry, "Executable or script to run when the key is pressed")
        self.external_script_entry.set_text(str(self.cfg.get("external_script") or "")); script_grid.attach(self.external_script_entry, 1, 0, 1, 1)
        script_browse = Gtk.Button(label="Browse…"); set_widget_description(script_browse, "Select an executable or script file"); script_browse.connect("clicked", self.on_external_script_browse); script_grid.attach(script_browse, 2, 0, 1, 1)
        args_label = Gtk.Label(label="Arguments:"); args_label.set_xalign(1); script_grid.attach(args_label, 0, 1, 1, 1)
        self.external_script_args_entry = Gtk.Entry(); self.external_script_args_entry.set_hexpand(True); self.external_script_args_entry.set_placeholder_text('--host 192.168.1.20 --scene "Camera Principal"')
        set_widget_description(self.external_script_args_entry, "Optional command-line arguments. Quotes keep values with spaces together")
        raw_script_args = self.cfg.get("external_script_args")
        if isinstance(raw_script_args, list):
            self.external_script_args_entry.set_text(shlex.join([str(x) for x in raw_script_args]))
        script_grid.attach(self.external_script_args_entry, 1, 1, 2, 1)

        self.hid_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8); outer.append(self.hid_box)
        hid_title = Gtk.Label(); hid_title.set_markup("<b>HID Actions</b>"); hid_title.set_xalign(0); self.hid_box.append(hid_title)
        hid_hint = Gtk.Label(label="The actions below are written to scripts/profile_P_key_K.txt and executed directly by the firmware. Multi-key shortcuts must stay on the same line.")
        hid_hint.set_xalign(0); hid_hint.set_wrap(True); hid_hint.add_css_class("dim-label"); self.hid_box.append(hid_hint)
        self.hid_warning = Gtk.Label(); self.hid_warning.set_xalign(0); self.hid_warning.set_wrap(True); self.hid_warning.add_css_class("warning"); self.hid_box.append(self.hid_warning)
        self.action_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8); self.hid_box.append(self.action_box)
        add_action = Gtk.Button(label="+ Add HID action"); set_widget_description(add_action, "Append another firmware HID action to this key"); add_action.set_halign(Gtk.Align.START); add_action.connect("clicked", self.add_hid_action); self.hid_box.append(add_action)

        for data in self.hid_actions_from_config():
            self.add_hid_action(data=data)
        if not self.hid_rows:
            self.add_hid_action(data={"type": "shortcut", "value": ""})

        icon_grid = Gtk.Grid(column_spacing=10, row_spacing=10); outer.append(icon_grid)
        self.icon_label = Gtk.Label(label="Icon:"); self.icon_label.set_xalign(1); icon_grid.attach(self.icon_label, 0, 0, 1, 1)
        icon_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); icon_grid.attach(icon_row, 1, 0, 2, 1)
        self.icon_preview = Gtk.Image(); self.icon_preview.set_pixel_size(56); icon_row.append(self.icon_preview)
        choose_icon = Gtk.Button(label="Choose image…"); set_widget_description(choose_icon, "Choose the single 64×64 icon used by this key"); choose_icon.connect("clicked", self.on_choose_icon); icon_row.append(choose_icon)
        clear_icon_btn = Gtk.Button(label="Remove"); set_widget_description(clear_icon_btn, "Remove the custom icon from this key"); clear_icon_btn.connect("clicked", self.on_clear_icon); icon_row.append(clear_icon_btn)
        self.icon_hint = Gtk.Label()
        self.icon_hint.set_xalign(0); self.icon_hint.set_wrap(True); self.icon_hint.add_css_class("dim-label"); outer.append(self.icon_hint)


        self.status_label = Gtk.Label(); self.status_label.set_xalign(0); self.status_label.set_wrap(True); outer.append(self.status_label)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); buttons.set_halign(Gtk.Align.END); outer.append(buttons)
        self.delete_button = Gtk.Button(label="Remove configuration")
        set_widget_description(self.delete_button, "Delete this key configuration from the local cache and connected MacroPad")
        self.delete_button.add_css_class("destructive-action")
        self.delete_button.connect("clicked", self.on_delete)
        buttons.append(self.delete_button)
        cancel = Gtk.Button(label="Cancel"); set_widget_description(cancel, "Close without saving changes"); cancel.connect("clicked", lambda *_: self.close()); buttons.append(cancel)
        self.test_button = Gtk.Button(label="Test action"); set_widget_description(self.test_button, "Run the selected host action without saving the key"); self.test_button.connect("clicked", self.on_test); buttons.append(self.test_button)
        self.save_button = Gtk.Button(label="Save"); set_widget_description(self.save_button, "Save this key to the persistent cache and to the MacroPad when storage is available"); self.save_button.add_css_class("suggested-action"); self.save_button.connect("clicked", self.on_save); buttons.append(self.save_button)
        self.on_action_changed()
        self.on_mode_toggled()
        self.update_icon_preview()

    def hid_actions_from_config(self) -> list[dict]:
        rows: list[dict] = []
        actions = self.cfg.get("actions")
        if not isinstance(actions, list):
            return rows
        for item in actions:
            if not isinstance(item, dict):
                continue
            act = str(item.get("act", ""))
            arg = str(item.get("arg", ""))
            if act == "5":
                rows.append({"type": "shortcut", "value": arg})
            elif act.lower() == "d":
                rows.append({"type": "key", "value": arg})
            elif act.lower() == "a" and arg.upper() in HID_SCRIPT_TO_MEDIA:
                # Official HidMode=True media format: ACT a MK_* + matching script.
                rows.append({"type": "key", "value": HID_SCRIPT_TO_MEDIA[arg.upper()]})
            elif act.lower() == "e" and arg.upper() in HID_CONSUMER_TOKENS:
                # Compatibility with EezOpen v0.5.14-v0.5.16. Re-saving rewrites
                # this legacy form to the confirmed official ACT a MK_* format.
                rows.append({"type": "key", "value": arg.upper()})
            elif act == "7":
                try:
                    ms = int(arg)
                except ValueError:
                    self.unsupported_hid_actions.append(item)
                else:
                    rows.append({"type": "wait", "ms": ms})
            elif act == "4":
                enter = arg.endswith(" ENTER")
                text = arg[:-6] if enter else arg
                rows.append({"type": "text", "text": text, "enter": enter})
            else:
                self.unsupported_hid_actions.append(item)
        return rows

    def add_hid_action(self, _button=None, data: dict | None = None):
        row = HidActionRow(self, data)
        self.hid_rows.append(row)
        self.action_box.append(row.box)

    def remove_hid_action(self, row: HidActionRow):
        if row not in self.hid_rows:
            return
        self.hid_rows.remove(row)
        self.action_box.remove(row.box)
        if not self.hid_rows:
            self.add_hid_action(data={"type": "shortcut", "value": ""})

    def move_hid_action(self, row: HidActionRow, direction: int):
        if row not in self.hid_rows:
            return
        old = self.hid_rows.index(row)
        new = max(0, min(len(self.hid_rows) - 1, old + direction))
        if new == old:
            return
        self.hid_rows.pop(old); self.hid_rows.insert(new, row)
        for item in list(self.hid_rows):
            self.action_box.remove(item.box)
        for item in self.hid_rows:
            self.action_box.append(item.box)

    def selected_action(self) -> str:
        idx = int(self.action_dropdown.get_selected())
        return ACTION_IDS[idx] if 0 <= idx < len(ACTION_IDS) else "1"

    def on_mode_toggled(self, *_args):
        hid = self.hid_check.get_active()
        external_script = (not hid and self.selected_action() == "p")
        self.set_default_size(720, 620) if hid else self.set_default_size(620, 560 if external_script else 430)
        self.nonhid_box.set_visible(not hid)
        self.hid_box.set_visible(hid)
        self.test_button.set_visible(not hid)
        self.icon_label.set_text("Icon:")
        if hid:
            self.icon_hint.set_text("Optional custom icon. In HID Mode, EezOpen does not inject a default action icon; the file sent to the device remains a 64×64 RGBA PNG.")
            if self.unsupported_hid_actions:
                ids = ", ".join(str(x.get("act")) for x in self.unsupported_hid_actions)
                self.hid_warning.set_text(f"This key contains HID ACT entries that EezOpen cannot edit yet ({ids}). HID saving is blocked to prevent configuration loss.")
            else:
                self.hid_warning.set_text("")
        elif external_script:
            self.icon_hint.set_text("Optional custom icon. External Script uses the same single-icon model as other HID and Non-HID actions; no runtime image swapping is performed.")
        else:
            self.icon_hint.set_text("Without a custom image, EezOpen uses a default icon for the action when one is available. The file sent to the device is a 64×64 RGBA PNG.")
        self.update_icon_preview()

    def on_action_changed(self, *_args):
        act = self.selected_action()
        external_script = act == "p"
        self.external_script_box.set_visible(external_script)
        self.arg_label.set_visible(not external_script)
        self.arg_entry.set_visible(not external_script)
        self.browse_button.set_visible(False if external_script else act in {"3", "f"})
        if act == "1":
            self.arg_label.set_text("URL:"); self.arg_entry.set_placeholder_text("example.com")
        elif act == "2":
            self.arg_label.set_text("Command:"); self.arg_entry.set_placeholder_text("obs or program --argument")
        elif act == "3":
            self.arg_label.set_text("Folder:"); self.arg_entry.set_placeholder_text("/home/user/projects")
        elif act == "f":
            self.arg_label.set_text("File:"); self.arg_entry.set_placeholder_text("/home/user/file.txt")
        self.icon_label.set_text("Icon:")
        if not self.hid_check.get_active():
            self.set_default_size(620, 560 if external_script else 430)
            if external_script:
                self.icon_hint.set_text("Optional custom icon. External Script uses the same single-icon model as other HID and Non-HID actions; no runtime image swapping is performed.")
            else:
                self.icon_hint.set_text("Without a custom image, EezOpen uses a default icon for the action when one is available. The file sent to the device is a 64×64 RGBA PNG.")
        if not self.hid_check.get_active() and not self.icon_source and not self.clear_icon:
            self.update_icon_preview()

    def on_browse(self, *_args):
        action = Gtk.FileChooserAction.SELECT_FOLDER if self.selected_action() == "3" else Gtk.FileChooserAction.OPEN
        chooser = Gtk.FileChooserNative(title="Select folder" if action == Gtk.FileChooserAction.SELECT_FOLDER else "Select file",
                                        transient_for=self, action=action, accept_label="Select", cancel_label="Cancel")
        self._chooser = chooser
        chooser.connect("response", self.on_chooser_response); chooser.show()

    def on_chooser_response(self, chooser, response):
        if response == Gtk.ResponseType.ACCEPT:
            selected = chooser.get_file()
            if selected and selected.get_path(): self.arg_entry.set_text(selected.get_path())
        chooser.destroy(); self._chooser = None

    def on_external_script_browse(self, *_args):
        chooser = Gtk.FileChooserNative(title="Select external script executable", transient_for=self,
                                        action=Gtk.FileChooserAction.OPEN, accept_label="Select", cancel_label="Cancel")
        self._chooser = chooser
        chooser.connect("response", self.on_external_script_chooser_response); chooser.show()

    def on_external_script_chooser_response(self, chooser, response):
        if response == Gtk.ResponseType.ACCEPT:
            selected = chooser.get_file()
            if selected and selected.get_path():
                self.external_script_entry.set_text(selected.get_path())
        chooser.destroy(); self._chooser = None

    def on_choose_icon(self, *_args):
        chooser = Gtk.FileChooserNative(title="Select icon", transient_for=self,
                                        action=Gtk.FileChooserAction.OPEN,
                                        accept_label="Select", cancel_label="Cancel")
        self._icon_chooser = chooser
        filt = Gtk.FileFilter(); filt.set_name("Images"); filt.add_pixbuf_formats(); chooser.add_filter(filt)
        chooser.connect("response", self.on_icon_chooser_response); chooser.show()

    def on_icon_chooser_response(self, chooser, response):
        if response == Gtk.ResponseType.ACCEPT:
            selected = chooser.get_file()
            if selected and selected.get_path():
                self.icon_source = selected.get_path(); self.icon_changed = True; self.clear_icon = False
                self.update_icon_preview()
        chooser.destroy(); self._icon_chooser = None

    def on_clear_icon(self, *_args):
        self.icon_source = ""; self.icon_changed = True; self.clear_icon = True; self.update_icon_preview()

    def effective_icon_source(self) -> str | None:
        if self.clear_icon:
            return None
        if self.icon_source and Path(self.icon_source).is_file():
            return self.icon_source
        if self.hid_check.get_active():
            return None
        return default_icon_path(self.selected_action())

    def update_icon_preview(self):
        source = self.effective_icon_source()
        if source and Path(source).is_file():
            try:
                pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(source, 56, 56, True)
                self.icon_preview.set_from_pixbuf(pix); return
            except Exception:
                pass
        self.icon_preview.set_from_icon_name("image-missing-symbolic")

    def _normalized_image_path(self, source: str | None, suffix: str) -> str | None:
        if not source:
            return None
        pix = GdkPixbuf.Pixbuf.new_from_file(source)
        if pix.get_width() != 64 or pix.get_height() != 64:
            pix = pix.scale_simple(64, 64, GdkPixbuf.InterpType.BILINEAR)
        if not pix.get_has_alpha():
            pix = pix.add_alpha(False, 0, 0, 0)
        base = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "eezopen"
        base.mkdir(parents=True, exist_ok=True)
        out = base / f"gui-icon-{self.profile}-{self.key}-{suffix}.png"
        pix.savev(str(out), "png", [], [])
        return str(out)

    def normalized_icon_path(self) -> str | None:
        if self.clear_icon:
            return None

        # Re-normalize the effective icon on every save. This also upgrades icons
        # cached by older EezOpen builds that were 64x64 PNG but not RGBA. A key
        # with no current icon receives the default for its selected action.
        source = self.effective_icon_source()
        if not source:
            return None

        pix = GdkPixbuf.Pixbuf.new_from_file(source)
        if pix.get_width() != 64 or pix.get_height() != 64:
            pix = pix.scale_simple(64, 64, GdkPixbuf.InterpType.BILINEAR)
        # The official per-key icon captured on the device is a 64x64 RGBA PNG
        # (PNG color type 6). Force alpha even when the source image is RGB.
        if not pix.get_has_alpha():
            pix = pix.add_alpha(False, 0, 0, 0)
        base = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "eezopen"
        base.mkdir(parents=True, exist_ok=True)
        out = base / f"gui-icon-{self.profile}-{self.key}.png"
        pix.savev(str(out), "png", [], [])
        return str(out)

    def on_test(self, *_args):
        if self.selected_action() != "p":
            try:
                ipc_request({"cmd": "test_action", "act": self.selected_action(), "arg": self.arg_entry.get_text()})
                self.status_label.set_text("Action sent for testing.")
            except Exception as exc:
                self.status_label.set_text(f"Error: {exc}")
            return
        script = self.external_script_entry.get_text().strip()
        if not script:
            self.status_label.set_text("Select an External Script first."); return
        try:
            arguments = shlex.split(self.external_script_args_entry.get_text())
        except ValueError as exc:
            self.status_label.set_text(f"Invalid arguments: {exc}"); return
        self.status_label.set_text("Running External Script test…")
        def ok(result):
            rc = result.get("returncode")
            out = str(result.get("stdout") or "").strip()
            err = str(result.get("stderr") or "").strip()
            detail = f"exit={rc}"
            if out: detail += f" · stdout: {out[-500:]}"
            if err: detail += f" · stderr: {err[-500:]}"
            self.status_label.set_text(detail); return False
        def fail(exc): self.status_label.set_text(f"External Script test failed: {exc}"); return False
        ipc_async({"cmd": "test_external_script", "script": script, "arguments": arguments}, ok, fail, timeout=32.0)

    def on_delete(self, *_args):
        screen_reason = screen_script_storage_error(self.parent_window.current_status)
        if screen_reason:
            self.status_label.set_text(screen_reason)
            show_error_dialog(self, "Cannot remove configuration", screen_reason)
            return
        if self.parent_window.current_status.get("storage_guarded"):
            message = "Removing a key from the physical MacroPad requires USB storage, which is unavailable in Safe Mode."
            self.status_label.set_text(message)
            show_error_dialog(self, "Cannot remove configuration", message)
            return
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=(f"Remove the configuration for Profile {self.profile} · Key {self.key}?\n\n"
                  "The config, script, and icon for this key will be deleted from the MacroPad "
                  "and the persistent home cache. You can then create the configuration again from scratch."),
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Remove", Gtk.ResponseType.OK)
        dialog.connect("response", self.on_delete_confirmed)
        dialog.present()

    def on_delete_confirmed(self, dialog, response):
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        self.status_label.set_text("Removing configuration from the MacroPad and persistent cache…")

        def ok(result):
            removed = int(result.get("files_removed", 0))
            self.parent_window.set_notice(
                f"Profile {self.profile} · Key {self.key} removed ({removed} arquivos). "
                "Agora ela pode ser configurada novamente do zero."
            )
            self.parent_window.update_status()
            self.parent_window.refresh_profile()
            self.close()
            return False

        def fail(exc):
            message = f"Failed to remove configuration: {exc}"
            self.status_label.set_text(message)
            show_error_dialog(self, "Cannot remove configuration", exc)
            return False

        ipc_async({"cmd": "delete_key", "profile": self.profile, "key": self.key},
                  ok, fail, timeout=15.0)

    def on_save(self, *_args):
        hid = self.hid_check.get_active()
        status = self.parent_window.current_status
        screen_reason = screen_script_storage_error(status)
        if screen_reason and bool(status.get("connected")):
            self.status_label.set_text(screen_reason)
            show_error_dialog(self, "Cannot save configuration", screen_reason)
            return
        if status.get("storage_quarantined") and bool(status.get("connected")):
            reason = str(status.get("storage_quarantine_reason") or "An incomplete USB Mass Storage operation was detected.")
            message = f"{reason} Reconnect the MacroPad before saving to it."
            self.status_label.set_text(message)
            show_error_dialog(self, "Cannot save configuration", message)
            return
        guarded = bool(status.get("storage_guarded"))
        if hid and guarded:
            self.status_label.set_text("HID script changes require the MacroPad USB volume and are unavailable in Safe Mode. Existing HID keys continue to work.")
            return
        if hid and self.unsupported_hid_actions:
            self.status_label.set_text("HID save blocked: this configuration contains ACT entries that the editor does not support yet, preventing existing actions from being lost.")
            return

        try:
            prepared_icon = self.normalized_icon_path()
        except Exception as exc:
            self.status_label.set_text(f"Failed to prepare icon: {exc}"); return

        if hid:
            try:
                actions = [row.payload() for row in self.hid_rows]
            except Exception as exc:
                self.status_label.set_text(f"Invalid HID action: {exc}"); return
            payload = {"cmd": "save_hid_key", "profile": self.profile, "key": self.key,
                       "alias": self.alias_entry.get_text(), "actions": actions,
                       "icon_path": prepared_icon, "clear_icon": self.clear_icon}
        elif self.selected_action() == "p":
            script = self.external_script_entry.get_text().strip()
            if not script:
                self.status_label.set_text("Select an External Script before saving."); return
            try:
                arguments = shlex.split(self.external_script_args_entry.get_text())
            except ValueError as exc:
                self.status_label.set_text(f"Invalid External Script arguments: {exc}"); return
            payload = {"cmd": "save_external_script_key", "profile": self.profile, "key": self.key,
                       "alias": self.alias_entry.get_text(), "script": script, "arguments": arguments,
                       "icon_path": prepared_icon, "clear_icon": self.clear_icon}
        else:
            arg = self.arg_entry.get_text()
            if not arg.strip(): self.status_label.set_text("Enter a target or command before saving."); return
            payload = {"cmd": "save_key", "profile": self.profile, "key": self.key,
                       "alias": self.alias_entry.get_text(), "act": self.selected_action(), "arg": arg,
                       "icon_path": prepared_icon, "clear_icon": self.clear_icon}

        self.status_label.set_text("Saving configuration…")
        def ok(result):
            notes = ["HID configuration saved." if hid else "Configuration saved."]
            connected = bool(self.parent_window.current_status.get("connected"))
            guarded_now = bool(self.parent_window.current_status.get("storage_guarded"))
            if result.get("device_written"):
                notes.append("TXT/script written to the MacroPad." if hid else "TXT written to the MacroPad.")
            elif connected and guarded_now:
                notes.append("Saved to the persistent local cache; USB storage is unavailable.")
            elif connected:
                notes.append("Saved to the persistent local cache; device storage is unavailable.")
            else:
                notes.append("Saved only to the persistent local cache (MacroPad disconnected).")
            if result.get("icon_changed"):
                if connected and guarded_now:
                    notes.append("Icon saved in the local cache; the physical icon is unchanged while USB storage is unavailable.")
                else:
                    notes.append("Icon removed." if result.get("icon_removed") else "64×64 PNG icon written.")
            notes.append("Mode/alias sent over serial." if result.get("serial_applied") else f"Serial: {result.get('serial_error') or 'not applied'}")
            self.parent_window.set_notice(" ".join(notes)); self.parent_window.update_status(); self.parent_window.refresh_profile(); self.close(); return False
        def fail(exc):
            self.status_label.set_text(f"Failed to save configuration: {exc}")
            show_error_dialog(self, "Cannot save configuration", exc)
            return False
        ipc_async(payload, ok, fail, timeout=20.0)


class RgbWindow(Gtk.Window):
    # Correct order used by the current 1.8.3 Configurator / device mode index.
    MODES = ["Always On", "When Pressing The Key", "Breath", "Flowing", "Always Off"]

    def __init__(self, parent: "MainWindow"):
        super().__init__(title="MacroPad RGB", transient_for=parent, modal=True)
        self.parent_window = parent; self.set_default_size(430, 250)
        daemon_modes = parent.current_status.get("rgb_modes") if isinstance(parent.current_status, dict) else None
        self.modes = list(daemon_modes) if isinstance(daemon_modes, list) and daemon_modes else list(self.MODES)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(18); box.set_margin_bottom(18); box.set_margin_start(18); box.set_margin_end(18); self.set_child(box)
        title = Gtk.Label(); title.set_markup("<b>RGB Backlight</b>"); title.set_xalign(0); box.append(title)
        grid = Gtk.Grid(column_spacing=12, row_spacing=12); box.append(grid)
        # The previous version always reopened with a hard-coded blue/Always On. Load the
        # last values persisted by the daemon instead, matching the official
        # Configurator's local-settings behaviour.
        rgb_state = dict(parent.current_status.get("rgb") or {})
        if not rgb_state:
            try:
                rgb_state = dict(ipc_request({"cmd": "status"}, timeout=1.0)["status"].get("rgb") or {})
            except Exception:
                rgb_state = {}
        r = max(0, min(255, int(rgb_state.get("r", 0))))
        g = max(0, min(255, int(rgb_state.get("g", 160))))
        b = max(0, min(255, int(rgb_state.get("b", 255))))
        a = max(0, min(255, int(rgb_state.get("a", 255))))
        selected_mode = int(rgb_state.get("mode", 0))
        if not (0 <= selected_mode < len(self.modes)):
            selected_mode = 0

        lab = Gtk.Label(label="Color:"); lab.set_xalign(1); grid.attach(lab,0,0,1,1)
        self.color = Gtk.ColorButton(); set_widget_description(self.color, "Choose the RGB backlight color"); rgba = Gdk.RGBA(); rgba.parse(f"#{r:02X}{g:02X}{b:02X}"); rgba.alpha = a / 255.0; self.color.set_rgba(rgba); grid.attach(self.color,1,0,1,1)
        lab = Gtk.Label(label="Mode:"); lab.set_xalign(1); grid.attach(lab,0,1,1,1)
        self.mode = Gtk.DropDown.new_from_strings(self.modes); set_widget_description(self.mode, "Choose the RGB animation mode"); self.mode.set_selected(selected_mode); grid.attach(self.mode,1,1,1,1)
        hint = Gtk.Label(label="EezOpen stores the latest color/mode locally and sends them to the MacroPad in the same order as the official Configurator: mode first, color second.")
        hint.set_xalign(0); hint.set_wrap(True); hint.add_css_class("dim-label"); box.append(hint)
        self.status = Gtk.Label(); self.status.set_xalign(0); self.status.set_wrap(True); box.append(self.status)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); row.set_halign(Gtk.Align.END); box.append(row)
        cancel = Gtk.Button(label="Cancel"); set_widget_description(cancel, "Close without applying RGB changes"); cancel.connect("clicked", lambda *_: self.close()); row.append(cancel)
        apply = Gtk.Button(label="Apply RGB"); set_widget_description(apply, "Send the selected RGB color and mode to the MacroPad"); apply.add_css_class("suggested-action"); apply.connect("clicked", self.on_apply); row.append(apply)

    def on_apply(self, *_args):
        c = self.color.get_rgba()
        vals = [max(0,min(255,round(x*255))) for x in (c.red,c.green,c.blue,c.alpha)]
        mode = int(self.mode.get_selected())
        try:
            r = ipc_request({"cmd":"set_rgb", "r":vals[0], "g":vals[1], "b":vals[2], "a":vals[3], "mode":mode})
            self.parent_window.current_status["rgb"] = {k: r.get(k) for k in ("r", "g", "b", "a", "mode")}
            self.parent_window.set_notice(f"RGB applied and saved: {r.get('mode_name')} · #{vals[0]:02X}{vals[1]:02X}{vals[2]:02X}.")
            self.close()
        except Exception as exc: self.status.set_text(f"Failed to apply RGB: {exc}")


class FirmwareWindow(Gtk.Window):
    def __init__(self, parent: "MainWindow"):
        super().__init__(title="Update firmware", transient_for=parent, modal=True)
        self.parent_window = parent
        self.bin_path = ""; self.json_path = ""
        self.file_chooser: Gtk.FileChooserNative | None = None
        self.confirm_dialog: Gtk.MessageDialog | None = None
        self.set_default_size(590, 330)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(18); box.set_margin_bottom(18); box.set_margin_start(18); box.set_margin_end(18)
        self.set_child(box)
        title = Gtk.Label(); title.set_markup("<b>USB Firmware Update</b>"); title.set_xalign(0); box.append(title)
        warning = Gtk.Label(label="Select the matching BIN and JSON files. EezOpen validates the model and size, copies mc-08.bin and mc-08.json to the MacroPad root, and sends the update command. Do not disconnect USB during the process.")
        warning.set_xalign(0); warning.set_wrap(True); box.append(warning)

        grid = Gtk.Grid(column_spacing=8, row_spacing=10); box.append(grid)
        grid.attach(Gtk.Label(label="BIN:"), 0, 0, 1, 1)
        self.bin_label = Gtk.Entry(); self.bin_label.set_editable(False); self.bin_label.set_hexpand(True); grid.attach(self.bin_label, 1, 0, 1, 1)
        b = Gtk.Button(label="Select…"); set_widget_description(b, "Select the firmware BIN file"); b.connect("clicked", self.choose_file, "bin"); grid.attach(b, 2, 0, 1, 1)
        grid.attach(Gtk.Label(label="JSON:"), 0, 1, 1, 1)
        self.json_label = Gtk.Entry(); self.json_label.set_editable(False); self.json_label.set_hexpand(True); grid.attach(self.json_label, 1, 1, 1, 1)
        b = Gtk.Button(label="Select…"); set_widget_description(b, "Select the matching firmware JSON metadata file"); b.connect("clicked", self.choose_file, "json"); grid.attach(b, 2, 1, 1, 1)
        self.status = Gtk.Label(); self.status.set_xalign(0); self.status.set_wrap(True); box.append(self.status)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); buttons.set_halign(Gtk.Align.END); box.append(buttons)
        close = Gtk.Button(label="Cancel"); set_widget_description(close, "Close the firmware update window"); close.connect("clicked", lambda *_: self.close()); buttons.append(close)
        update = Gtk.Button(label="Update firmware"); set_widget_description(update, "Validate, copy and trigger the selected firmware update"); update.add_css_class("destructive-action"); update.connect("clicked", self.confirm_update); buttons.append(update)

    def choose_file(self, _button, kind: str):
        # Keep a strong reference while the native chooser is open. Without it,
        # PyGObject can release the wrapper as soon as this callback returns and
        # the native dialog may disappear together with the firmware workflow.
        try:
            if self.file_chooser is not None:
                self.file_chooser.show()
                return
            chooser = Gtk.FileChooserNative(
                title="Select firmware" if kind == "bin" else "Select JSON metadata",
                transient_for=self,
                action=Gtk.FileChooserAction.OPEN,
                accept_label="Select",
                cancel_label="Cancel",
            )
            self.file_chooser = chooser
            chooser.connect("response", self.file_chosen, kind)
            chooser.show()
        except Exception as exc:
            self.file_chooser = None
            self.status.set_text(f"Failed to open file selector: {exc}")

    def file_chosen(self, chooser, response, kind: str):
        try:
            if response == Gtk.ResponseType.ACCEPT:
                f = chooser.get_file()
                path = f.get_path() if f else None
                if path:
                    if kind == "bin":
                        self.bin_path = path
                        self.bin_label.set_text(path)
                    else:
                        self.json_path = path
                        self.json_label.set_text(path)
        except Exception as exc:
            self.status.set_text(f"Failed to read selected file: {exc}")
        finally:
            chooser.hide()
            if self.file_chooser is chooser:
                self.file_chooser = None

    def confirm_update(self, *_args):
        if not self.bin_path or not self.json_path:
            self.status.set_text("Select both files."); return
        if self.confirm_dialog is not None:
            self.confirm_dialog.present()
            return
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text="Confirm firmware update?\n\nThe files will be copied to the MacroPad and command 'n' will be sent. Do not disconnect USB.",
        )
        self.confirm_dialog = dialog
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL); dialog.add_button("Atualizar", Gtk.ResponseType.OK)
        dialog.connect("response", self.on_confirm_response); dialog.present()

    def on_confirm_response(self, dialog, response):
        if self.confirm_dialog is dialog:
            self.confirm_dialog = None
        dialog.destroy()
        if response != Gtk.ResponseType.OK: return
        self.status.set_text("Validating, copying, syncing USB, and sending the command at 115200…")
        def ok(result):
            self.status.set_text(f"Command sent at {result.get('trigger_baud')} baud. Version {result.get('version')} · {result.get('size')} bytes. Wait for completion on the MacroPad screen.")
            self.parent_window.set_notice("Firmware update triggered; the daemon will redetect the MacroPad after reboot.")
            return False
        def fail(exc):
            self.status.set_text(f"Failed: {exc}")
            show_error_dialog(self, "Firmware update failed", exc)
            return False
        ipc_async({"cmd": "firmware_update", "bin_path": self.bin_path, "json_path": self.json_path}, ok, fail, timeout=30.0)


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title="EezOpen Config")
        self.set_default_size(760, 500)
        self.current_status: dict = {}
        self.current_profile = 1
        self.key_buttons: dict[int, Gtk.Button] = {}
        self.profile_buttons: dict[int, Gtk.Button] = {}
        # Keep a strong reference to the firmware window.  A temporary
        # `FirmwareWindow(self).present()` object can be garbage-collected
        # immediately by Python, which makes the GTK window disappear as soon
        # as it is opened.
        self.firmware_window: FirmwareWindow | None = None
        self.background_image_path = ""
        self.background_file_chooser: Gtk.FileChooserNative | None = None
        self.screen_script_path = ""
        self.screen_scripts: list[dict[str, str]] = []
        self.screen_script_selected = 0
        self.screen_script_dirty = False
        self.screen_script_updating = False
        self.screen_script_file_chooser: Gtk.FileChooserNative | None = None
        self.screen_script_file_chooser_mode = "add"
        self.screen_script_request_pending = False
        # Coalesce rapid profile clicks. Only one physical profile-switch IPC
        # may be in flight; while storage is busy, keep only the newest target.
        self.profile_switch_pending = False
        self.profile_switch_target: int | None = None
        self.profile_switch_retry_scheduled = False
        self.background_apply_pending = False

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0); self.set_child(root)

        # Use the HeaderBar as the real window titlebar instead of placing it
        # inside the content area.  Keeping an embedded HeaderBar while the
        # window manager also draws decorations can produce two sets of
        # minimize/maximize/close controls.
        header = Gtk.HeaderBar()
        header.set_show_title_buttons(True)
        self.set_titlebar(header)
        title = Gtk.Label(); title.set_markup("<b>EezOpen Config</b>"); header.set_title_widget(title)

        theme_menu = Gtk.MenuButton(icon_name="weather-clear-night-symbolic"); set_widget_description(theme_menu, "MacroPad theme")
        pop = Gtk.Popover(); theme_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4); theme_box.set_margin_top(8); theme_box.set_margin_bottom(8); theme_box.set_margin_start(8); theme_box.set_margin_end(8)
        dark = Gtk.Button(label="Dark theme"); set_widget_description(dark, "Apply the MacroPad dark theme"); dark.connect("clicked", self.on_theme, "dark", pop); theme_box.append(dark)
        light = Gtk.Button(label="Light theme"); set_widget_description(light, "Apply the MacroPad light theme"); light.connect("clicked", self.on_theme, "light", pop); theme_box.append(light)
        pop.set_child(theme_box); theme_menu.set_popover(pop); header.pack_end(theme_menu)
        rgb_btn = Gtk.Button(label="RGB"); set_widget_description(rgb_btn, "Configure the MacroPad RGB backlight"); rgb_btn.connect("clicked", self.on_rgb); header.pack_end(rgb_btn)
        self.firmware_button = Gtk.Button(label="Firmware"); set_widget_description(self.firmware_button, "Update the MacroPad firmware using BIN and JSON files"); self.firmware_button.connect("clicked", self.on_firmware); header.pack_end(self.firmware_button)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(14); content.set_margin_bottom(14); content.set_margin_start(18); content.set_margin_end(18)
        root.append(content)

        # Connection/device controls are one visual section, matching the framed
        # key and Screen Script areas below.
        connection_frame = Gtk.Frame(); content.append(connection_frame)
        connection_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        connection_box.set_margin_top(12); connection_box.set_margin_bottom(12)
        connection_box.set_margin_start(12); connection_box.set_margin_end(12)
        connection_frame.set_child(connection_box)

        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); connection_box.append(status_row)
        self.status_dot = Gtk.Label(label="○"); status_row.append(self.status_dot)
        self.device_label = Gtk.Label(label="Looking for daemon…"); self.device_label.set_xalign(0); self.device_label.set_hexpand(True); status_row.append(self.device_label)

        sync_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); connection_box.append(sync_row)
        self.pull_button = Gtk.Button(label="Sync MacroPad"); set_widget_description(self.pull_button, "Read the current configs, scripts, and icons from the connected MacroPad into EezOpen's temporary device view. This does not overwrite the settings saved in HOME."); self.pull_button.connect("clicked", self.on_pull); sync_row.append(self.pull_button)
        self.import_button = Gtk.Button(label="Save MacroPad settings to HOME"); set_widget_description(self.import_button, "Copy configs, scripts, and icons from the connected MacroPad to EezOpen's persistent settings in HOME. Useful when moving to a new computer or restoring the local EezOpen configuration."); self.import_button.connect("clicked", self.on_import_from_device); sync_row.append(self.import_button)
        self.push_button = Gtk.Button(label="Save HOME settings to MacroPad"); set_widget_description(self.push_button, "Copy the settings saved in EezOpen's HOME cache to the connected MacroPad. The MacroPad profile count is also adjusted automatically to match the local profiles."); self.push_button.connect("clicked", self.on_push); sync_row.append(self.push_button)
        query = Gtk.Button(label="Refresh Profiles"); set_widget_description(query, "Query the MacroPad over serial and refresh the number of profiles shown by EezOpen. This does not read or write the USB Mass Storage."); query.connect("clicked", self.on_query_profiles); sync_row.append(query)

        # Background controls get their own frame as well.
        background_frame = Gtk.Frame(); content.append(background_frame)
        background_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        background_row.set_margin_top(12); background_row.set_margin_bottom(12)
        background_row.set_margin_start(12); background_row.set_margin_end(12)
        background_frame.set_child(background_row)
        background_row.append(Gtk.Label(label="Background:"))
        self.background_theme = Gtk.DropDown.new_from_strings(["Dark", "Light"])
        set_widget_description(self.background_theme, "Choose which background file will be replaced on the MacroPad. Dark writes app_icons/bg_dark.bin. Light writes app_icons/bg_light.bin; the Light filename has not yet been confirmed on the physical device.")
        background_row.append(self.background_theme)
        self.background_path_entry = Gtk.Entry(); self.background_path_entry.set_editable(False); self.background_path_entry.set_hexpand(True); self.background_path_entry.set_placeholder_text("Select an image…")
        set_widget_description(self.background_path_entry, "Source image that will be resized to 622×345, reduced to 16 colors and converted to the LVGL background BIN format")
        background_row.append(self.background_path_entry)
        background_select = Gtk.Button(label="Select…"); set_widget_description(background_select, "Select the source image for the MacroPad background"); background_select.connect("clicked", self.on_background_select); background_row.append(background_select)
        self.background_apply_button = Gtk.Button(label="Apply Background"); set_widget_description(self.background_apply_button, "Convert the selected image in /tmp/eezopen and replace the selected background BIN in the MacroPad app_icons directory. After applying it, rotate the MacroPad wheel to change layout so the firmware reloads and displays the new background."); self.background_apply_button.connect("clicked", self.on_background_apply); background_row.append(self.background_apply_button)
        self.background_remove_button = Gtk.Button(label="Remove Background"); set_widget_description(self.background_remove_button, "Remove bg_dark.bin and bg_light.bin from the MacroPad app_icons directory so no custom background image is used. After removing them, rotate the MacroPad wheel to change layout and refresh the screen."); self.background_remove_button.connect("clicked", self.on_background_remove); background_row.append(self.background_remove_button)

        # Keep Screen Script controls visually grouped, like the MacroPad key grid.
        screen_frame = Gtk.Frame(); content.append(screen_frame)
        screen_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        screen_box.set_margin_top(12); screen_box.set_margin_bottom(12)
        screen_box.set_margin_start(12); screen_box.set_margin_end(12)
        screen_frame.set_child(screen_box)

        screen_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); screen_box.append(screen_row)
        screen_label = Gtk.Label(label="Screen Script:")
        set_widget_description(screen_label, "Long-running display scripts. Multiple scripts may be saved, but only one can run at a time. EezOpen keeps serial command handling active while the selected script controls the LCD.")
        screen_row.append(screen_label)
        self.screen_script_model = Gtk.StringList.new([])
        self.screen_script_dropdown = Gtk.DropDown(model=self.screen_script_model)
        self.screen_script_dropdown.set_hexpand(True)
        set_widget_description(self.screen_script_dropdown, "Choose one of the saved Screen Scripts")
        self.screen_script_dropdown.connect("notify::selected", self.on_screen_script_selected)
        screen_row.append(self.screen_script_dropdown)
        self.screen_script_add_button = Gtk.Button(label="Add…")
        set_widget_description(self.screen_script_add_button, "Add another Screen Script")
        self.screen_script_add_button.connect("clicked", self.on_screen_script_add)
        screen_row.append(self.screen_script_add_button)
        self.screen_script_remove_button = Gtk.Button(label="Remove")
        set_widget_description(self.screen_script_remove_button, "Remove the selected Screen Script from EezOpen. The script file itself is not deleted.")
        self.screen_script_remove_button.connect("clicked", self.on_screen_script_remove)
        screen_row.append(self.screen_script_remove_button)
        self.screen_script_run_button = Gtk.Button(label="Start Screen Script")
        set_widget_description(self.screen_script_run_button, "Start the selected Screen Script. EezOpen keeps the MacroPad serial connection and key actions active; only PCS/PC Monitor display telemetry is paused while the script runs.")
        self.screen_script_run_button.connect("clicked", self.on_screen_script_run)
        screen_row.append(self.screen_script_run_button)

        screen_edit_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); screen_box.append(screen_edit_row)
        screen_edit_row.append(Gtk.Label(label="Name:"))
        self.screen_script_name_entry = Gtk.Entry(); self.screen_script_name_entry.set_width_chars(14)
        self.screen_script_name_entry.set_placeholder_text("OBS Monitor")
        self.screen_script_name_entry.connect("changed", self.on_screen_script_field_changed)
        screen_edit_row.append(self.screen_script_name_entry)
        screen_edit_row.append(Gtk.Label(label="Script:"))
        self.screen_script_entry = Gtk.Entry(); self.screen_script_entry.set_editable(False); self.screen_script_entry.set_hexpand(True); self.screen_script_entry.set_placeholder_text("Select or add a script…")
        set_widget_description(self.screen_script_entry, "Python .py files are launched with the current Python interpreter; other files must be executable.")
        screen_edit_row.append(self.screen_script_entry)
        self.screen_script_select_button = Gtk.Button(label="Change…")
        set_widget_description(self.screen_script_select_button, "Change the file used by the selected Screen Script")
        self.screen_script_select_button.connect("clicked", self.on_screen_script_select)
        screen_edit_row.append(self.screen_script_select_button)
        screen_edit_row.append(Gtk.Label(label="Arguments:"))
        self.screen_script_args_entry = Gtk.Entry(); self.screen_script_args_entry.set_width_chars(24); self.screen_script_args_entry.set_placeholder_text('--host 192.168.1.20 --port 4444')
        set_widget_description(self.screen_script_args_entry, "Command-line arguments for this Screen Script. Quoting is parsed with shlex; no shell is used.")
        self.screen_script_args_entry.connect("changed", self.on_screen_script_field_changed)
        screen_edit_row.append(self.screen_script_args_entry)
        self.screen_script_save_button = Gtk.Button(label="Save")
        set_widget_description(self.screen_script_save_button, "Save name, path and arguments for the selected Screen Script")
        self.screen_script_save_button.connect("clicked", self.on_screen_script_save)
        screen_edit_row.append(self.screen_script_save_button)

        profile_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8); content.append(profile_row)
        profile_row.append(Gtk.Label(label="Profiles:"))
        scroller = Gtk.ScrolledWindow(); scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER); scroller.set_hexpand(True); scroller.set_min_content_height(42)
        self.profile_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4); scroller.set_child(self.profile_box); profile_row.append(scroller)
        minus = Gtk.Button(label="−"); set_widget_description(minus, "Remove the last profile"); minus.connect("clicked", self.on_profile_minus); profile_row.append(minus)
        plus = Gtk.Button(label="+"); set_widget_description(plus, "Add profile"); plus.connect("clicked", self.on_profile_plus); profile_row.append(plus)

        grid_frame = Gtk.Frame(); content.append(grid_frame)
        grid = Gtk.Grid(column_spacing=8, row_spacing=8); grid.set_margin_top(12); grid.set_margin_bottom(12); grid.set_margin_start(12); grid.set_margin_end(12); grid.set_column_homogeneous(True); grid.set_row_homogeneous(True); grid_frame.set_child(grid)
        for key in range(1, 9):
            button = Gtk.Button(); set_widget_description(button, f"Edit Profile {self.current_profile} · Key {key}"); button.set_size_request(112, 84); button.connect("clicked", self.on_key_clicked, key)
            self.key_buttons[key] = button; grid.attach(button, (key - 1) % 4, (key - 1) // 4, 1, 1); self.set_key_button(key, None)

        self.notice_label = Gtk.Label(); self.notice_label.set_xalign(0); self.notice_label.set_wrap(True); content.append(self.notice_label)
        self.footer = Gtk.Label(label="Save serializes Mass Storage and CDC access: the USB volume is mounted only for the file operation, flushed/unmounted, then mode/alias is applied over serial. Icons are normalized to 64×64 PNG.")
        self.footer.set_xalign(0); self.footer.set_wrap(True); self.footer.add_css_class("dim-label"); content.append(self.footer)

        GLib.timeout_add(250, self.poll_status); GLib.idle_add(self.initial_load)

    def set_notice(self, text: str): self.notice_label.set_text(text)

    def profile_count(self) -> int:
        try: return max(1, int(self.current_status.get("profile_count") or 1))
        except Exception: return 1

    def set_key_button(self, key: int, cfg: dict | None):
        button = self.key_buttons[key]
        set_widget_description(button, f"Edit Profile {self.current_profile} · Key {key}")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3); box.set_valign(Gtk.Align.CENTER)
        icon_path = str((cfg or {}).get("icon_path") or "")
        if icon_path and Path(icon_path).is_file():
            try:
                pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(icon_path, 34, 34, True)
                image = Gtk.Image.new_from_pixbuf(pix); box.append(image)
            except Exception:
                pass
        if not cfg:
            box.append(Gtk.Label(label=f"Key {key}")); box.append(Gtk.Label(label="Not configured")); button.set_child(box); return
        alias = str(cfg.get("alias", "")).strip() or "(no alias)"
        if len(alias) > 16: alias = alias[:15] + "…"
        kind = "HID · firmware" if cfg.get("hid") is True else ACTION_SHORT.get(str(cfg.get("act", "")), f"ACT {cfg.get('act', '')}")
        box.append(Gtk.Label(label=f"{key} · {alias}")); kind_label = Gtk.Label(label=kind); kind_label.add_css_class("dim-label"); box.append(kind_label)
        button.set_child(box)

    def refresh_profile_buttons(self):
        clear_box(self.profile_box); self.profile_buttons.clear()
        count = self.profile_count()
        if self.current_profile > count: self.current_profile = count
        for n in range(1, count + 1):
            b = Gtk.Button(label=str(n)); set_widget_description(b, f"Open profile {n} and switch the connected MacroPad to it"); b.connect("clicked", self.on_profile_selected, n)
            if n == self.current_profile: b.add_css_class("suggested-action")
            self.profile_box.append(b); self.profile_buttons[n] = b

    def initial_load(self):
        self.update_status()
        self.refresh_profile_buttons()
        self.refresh_profile()
        # Deliberately do not touch MacroPad Mass Storage on window open.
        # "Sync MacroPad" is an explicit operation so USB/SCSI startup cannot
        # race background CDC telemetry or an automatic recursive FAT read.
        return False

    def poll_status(self):
        old = (self.current_status.get("id"), self.current_status.get("connected"), self.current_status.get("profile_count"))
        self.update_status(); new = (self.current_status.get("id"), self.current_status.get("connected"), self.current_status.get("profile_count"))
        if old != new: self.refresh_profile_buttons(); self.refresh_profile()
        return True

    def update_status(self):
        try: self.current_status = ipc_request({"cmd": "status"}, timeout=0.8)["status"]
        except Exception as exc:
            self.current_status = {}; self.status_dot.set_text("○"); self.device_label.set_text(f"Daemon unavailable — {exc}"); return
        s = self.current_status
        guarded = bool(s.get("storage_guarded"))
        safe_mode = bool(s.get("safe_mode"))
        storage_quarantined = bool(s.get("storage_quarantined"))
        storage_busy = bool(s.get("storage_busy"))
        if guarded:
            tip = "USB Mass Storage is unavailable because the MacroPad is in Safe Mode. CDC/HID runtime features remain available." if safe_mode else "USB Mass Storage is currently unavailable."
            set_widget_description(self.pull_button, tip)
            set_widget_description(self.import_button, tip)
            set_widget_description(self.push_button, tip)
            set_widget_description(self.background_apply_button, tip)
            set_widget_description(self.background_remove_button, tip)
            set_widget_description(self.firmware_button, tip)
            self.footer.set_text("Safe Mode: the firmware is not exposing USB Mass Storage. CDC/HID, profiles, RGB, Non-HID actions and PC Monitor remain available." if safe_mode else "USB Mass Storage is currently unavailable.")
        elif storage_quarantined:
            reason = str(s.get("storage_quarantine_reason") or "A Mass Storage request did not complete safely")
            tip = reason + ". Reconnect the MacroPad before further USB/CDC writes."
            set_widget_description(self.pull_button, tip)
            set_widget_description(self.import_button, tip)
            set_widget_description(self.push_button, tip)
            set_widget_description(self.background_apply_button, tip)
            set_widget_description(self.background_remove_button, tip)
            set_widget_description(self.firmware_button, tip)
            self.footer.set_text("MacroPad USB storage is quarantined after an incomplete operation. Reconnect the MacroPad before continuing.")
        else:
            set_widget_description(self.pull_button, "Reload the connected MacroPad into the temporary view without changing the persistent cache")
            set_widget_description(self.import_button, "Replace the persistent EezOpen cache with configs, scripts, and icons from the connected MacroPad")
            set_widget_description(self.push_button, "Apply the persistent EezOpen cache and local profile count to the connected MacroPad")
            set_widget_description(self.background_apply_button, "Convert the selected image in /tmp/eezopen and replace the selected background BIN in the MacroPad app_icons directory. After applying it, rotate the MacroPad wheel to change layout so the firmware reloads and displays the new background.")
            set_widget_description(self.background_remove_button, "Remove bg_dark.bin and bg_light.bin from the MacroPad app_icons directory so no custom background image is used. After removing them, rotate the MacroPad wheel to change layout and refresh the screen.")
            set_widget_description(self.firmware_button, "Update MacroPad firmware")
            self.footer.set_text("Save writes the configuration to the persistent cache and, when storage is available, to the MacroPad USB volume. Mode/alias are applied over serial. Icons are normalized to 64×64 PNG.")
        remote_script = str(s.get("screen_script_path") or "")
        screen_active = bool(s.get("screen_script_active"))
        remote_scripts = s.get("screen_scripts") or []
        try:
            remote_selected = int(s.get("screen_script_selected", 0))
        except (TypeError, ValueError):
            remote_selected = 0
        if not self.screen_scripts and isinstance(remote_scripts, list):
            self.screen_scripts = [dict(item) for item in remote_scripts if isinstance(item, dict)]
            self.screen_script_selected = max(0, min(remote_selected, len(self.screen_scripts) - 1)) if self.screen_scripts else 0
            self.refresh_screen_script_widgets()
        if not self.screen_script_request_pending:
            self.screen_script_run_button.set_label("Stop Screen Script" if screen_active else "Start Screen Script")
            self.screen_script_run_button.set_sensitive(bool(screen_active or self.screen_scripts))
        controls_enabled = not screen_active and not self.screen_script_request_pending
        storage_controls_enabled = bool(s.get("connected")) and not guarded and not storage_quarantined and not storage_busy and not screen_active
        self.pull_button.set_sensitive(storage_controls_enabled)
        self.import_button.set_sensitive(storage_controls_enabled)
        self.push_button.set_sensitive(storage_controls_enabled)
        self.background_apply_button.set_sensitive(storage_controls_enabled and not self.background_apply_pending)
        self.background_remove_button.set_sensitive(storage_controls_enabled)
        self.firmware_button.set_sensitive(storage_controls_enabled)
        self.screen_script_dropdown.set_sensitive(controls_enabled and bool(self.screen_scripts))
        self.screen_script_add_button.set_sensitive(controls_enabled)
        self.screen_script_remove_button.set_sensitive(controls_enabled and bool(self.screen_scripts))
        self.screen_script_select_button.set_sensitive(controls_enabled and bool(self.screen_scripts))
        self.screen_script_name_entry.set_sensitive(controls_enabled and bool(self.screen_scripts))
        self.screen_script_args_entry.set_sensitive(controls_enabled and bool(self.screen_scripts))
        self.screen_script_save_button.set_sensitive(controls_enabled and bool(self.screen_scripts))

        if screen_active:
            self.status_dot.set_text("●")
            name = str(s.get("screen_script_name") or (Path(remote_script).name if remote_script else "screen script"))
            self.device_label.set_text(
                f"Screen Script running · {name} · PID {s.get('screen_script_pid') or '?'} · "
                f"{s.get('screen_script_tty') or '?'} · daemon CDC TX paused · PC Monitor paused"
            )
        elif s.get("connected"):
            self.status_dot.set_text("●")
            if guarded:
                storage = "USB storage disabled (Safe Mode)" if safe_mode else "USB storage unavailable"
            elif storage_quarantined:
                storage = "USB storage fault · reconnect MacroPad"
            elif storage_busy:
                storage = "USB storage active · operation in progress"
            elif s.get("storage_ready"):
                storage = "USB storage mounted"
            else:
                storage = "USB storage idle/unmounted"
            auth = "auth OK" if s.get("authenticated") else "auth not confirmed"
            self.device_label.set_text(f"Connected · type {s.get('type') or '?'} · {s.get('id') or '?'} · {s.get('device') or '?'} · {s.get('profile_count', 1)} profiles ({s.get('profile_count_source')}) · {auth} · {storage}")
        elif s.get("startup_phase") not in {None, "waiting"}:
            self.status_dot.set_text("◌")
            detail = str(s.get("startup_detail") or s.get("startup_phase") or "USB startup")
            self.device_label.set_text(f"MacroPad starting safely · {detail}")
        elif s.get("id"):
            self.status_dot.set_text("○"); self.device_label.set_text(f"MacroPad disconnected · cache {s.get('id')}")
        else:
            self.status_dot.set_text("○"); self.device_label.set_text("Daemon running · waiting for MacroPad")

    def refresh_profile(self):
        try:
            response = ipc_request({"cmd": "get_profile", "profile": self.current_profile})
            for item in response.get("keys", []): self.set_key_button(int(item["key"]), item.get("config"))
        except Exception as exc:
            for k in range(1, 9): self.set_key_button(k, None)
            self.set_notice(f"Could not load profile: {exc}")

    def _schedule_profile_switch_retry(self) -> None:
        if self.profile_switch_retry_scheduled:
            return
        self.profile_switch_retry_scheduled = True
        GLib.timeout_add(250, self._retry_profile_switch)

    def _retry_profile_switch(self):
        self.profile_switch_retry_scheduled = False
        self._start_profile_switch()
        return False

    def _start_profile_switch(self) -> None:
        if self.profile_switch_pending or self.profile_switch_target is None:
            return
        if not self.current_status.get("connected"):
            self.profile_switch_target = None
            return
        if self.current_status.get("storage_quarantined"):
            self.set_notice("MacroPad storage is quarantined after an incomplete USB operation. Reconnect the MacroPad before switching its physical profile.")
            self.profile_switch_target = None
            return
        if self.current_status.get("storage_busy"):
            # Keep only the newest requested profile and wait until the explicit
            # MSC transaction is complete. Do not spawn a queue of IPC threads.
            self._schedule_profile_switch_retry()
            return

        target = int(self.profile_switch_target)
        self.profile_switch_target = None
        self.profile_switch_pending = True

        def finish_and_continue():
            self.profile_switch_pending = False
            self.update_status()
            if self.profile_switch_target is not None:
                self._start_profile_switch()

        def ok(_result):
            self.set_notice(f"MacroPad switched to profile {target}.")
            finish_and_continue()
            return False

        def fail(exc):
            self.set_notice(f"Profile {target} open in EezOpen, but the MacroPad could not be switched: {exc}")
            finish_and_continue()
            return False

        ipc_async({"cmd": "activate_profile", "profile": target}, ok, fail, timeout=3.0)

    def on_profile_selected(self, _button, n):
        self.current_profile = n
        self.refresh_profile_buttons()
        self.refresh_profile()
        if self.current_status.get("connected"):
            # Rapid clicks/scrolling only keep the newest physical target. This
            # prevents dozens of concurrent activate_profile IPC workers when
            # storage is slow or temporarily unavailable.
            self.profile_switch_target = int(n)
            self._start_profile_switch()

    def change_profile_count(self, delta: int):
        old = self.profile_count(); new = old + delta
        if new < 1: self.set_notice("At least one profile is required."); return
        try:
            result = ipc_request({"cmd": "set_profile_count", "count": new}, timeout=5.0)
            self.update_status(); self.refresh_profile_buttons(); self.refresh_profile()
            if result.get("confirmed"):
                self.set_notice(f"MacroPad now has {new} profiles.")
            else:
                self.set_notice(f"EezOpen now shows {new} profiles locally only; connect the MacroPad to apply the profile count to the device.")
        except Exception as exc: self.set_notice(f"Failed to change profile count: {exc}")

    def on_profile_plus(self, *_): self.change_profile_count(1)

    def on_profile_minus(self, *_):
        if self.profile_count() <= 1: self.set_notice("At least one profile is required."); return
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=f"Remove profile {self.profile_count()}?\n\nThe persistent cache will not be deleted.",
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL); dialog.add_button("Remove", Gtk.ResponseType.OK)
        dialog.connect("response", lambda d, r: (d.destroy(), self.change_profile_count(-1) if r == Gtk.ResponseType.OK else None)); dialog.present()

    def on_pull(self, *_):
        if self.current_status.get("storage_guarded"):
            self.set_notice("USB storage is unavailable in Safe Mode; EezOpen is using the persistent local cache for runtime.")
            return
        self.set_notice("Refreshing MacroPad device view…")
        def ok(r):
            self.update_status(); self.refresh_profile_buttons(); self.refresh_profile()
            self.set_notice(f"Loaded into temporary device view: {r.get('configs')} configs; persistent home cache preserved" + (" + icons." if r.get("icons_synced") else "."))
            return False
        def fail(exc):
            self.set_notice(f"Refresh failed: {exc}")
            show_error_dialog(self, "Sync MacroPad failed", exc)
            return False
        ipc_async({"cmd": "pull_from_device"}, ok, fail, timeout=15.0)

    def on_import_from_device(self, *_):
        if self.current_status.get("storage_guarded"):
            self.set_notice("Import is unavailable in Safe Mode because USB Mass Storage is disabled.")
            return
        if not self.current_status.get("connected"):
            self.set_notice("Connect the MacroPad before importing its configuration.")
            return
        device_id = str(self.current_status.get("id") or "this device")
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text=(
                "Save MacroPad configuration to EezOpen?\n\n"
                f"This will copy the configs, scripts, and icons currently stored on {device_id} "
                "to your EezOpen configuration directory in your home folder."
            ),
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Import", Gtk.ResponseType.OK)

        def response(dlg, response_id):
            dlg.destroy()
            if response_id != Gtk.ResponseType.OK:
                return
            self.set_notice("Importing configs, scripts, and icons from MacroPad…")

            def ok(result):
                self.update_status(); self.refresh_profile_buttons(); self.refresh_profile()
                self.set_notice(
                    "Imported from MacroPad: "
                    f"{result.get('configs', 0)} configs, "
                    f"{result.get('scripts', 0)} scripts, "
                    f"{result.get('icons', 0)} icon files. "
                    "The persistent EezOpen cache is now up to date."
                )
                return False

            def fail(exc):
                self.set_notice(f"Import failed: {exc}")
                show_error_dialog(self, "Import from MacroPad failed", exc)
                return False

            ipc_async({"cmd": "import_from_device"}, ok, fail, timeout=20.0)

        dialog.connect("response", response)
        dialog.present()

    def on_push(self, *_):
        if self.current_status.get("storage_guarded"):
            self.set_notice("USB-volume apply is unavailable in Safe Mode. Serial-applied runtime settings still work normally.")
            return
        self.set_notice("Applying persistent cache to MacroPad…")
        def ok(r):
            self.refresh_profile()
            msg = f"Applied: {r.get('configs_written')} configs, {r.get('serial_applied')} serial updates, {r.get('profile_count', self.profile_count())} profiles"
            if r.get("icons_written"): msg += f", {r.get('icons_written')} icon files"
            if r.get("serial_errors"): msg += f". Errors: {len(r['serial_errors'])}"
            self.set_notice(msg + "."); return False
        def fail(exc):
            self.set_notice(f"Apply failed: {exc}")
            show_error_dialog(self, "Save HOME settings to MacroPad failed", exc)
            return False
        ipc_async({"cmd": "push_to_device"}, ok, fail, timeout=20.0)

    def on_query_profiles(self, *_):
        self.set_notice("Querying profile count from MacroPad…")
        def ok(_r):
            self.update_status(); self.refresh_profile_buttons(); self.refresh_profile()
            self.set_notice(f"Profiles found: {self.profile_count()} ({self.current_status.get('profile_count_source')}).")
            return False
        def fail(exc): self.set_notice(f"Failed to refresh profiles: {exc}"); return False
        ipc_async({"cmd": "query_profile_count"}, ok, fail, timeout=15.0)

    def on_background_select(self, *_args):
        try:
            if self.background_file_chooser is not None:
                self.background_file_chooser.show()
                return
            chooser = Gtk.FileChooserNative(
                title="Select background image",
                transient_for=self,
                action=Gtk.FileChooserAction.OPEN,
                accept_label="Select",
                cancel_label="Cancel",
            )
            self.background_file_chooser = chooser
            chooser.connect("response", self.on_background_file_chosen)
            chooser.show()
        except Exception as exc:
            self.background_file_chooser = None
            self.set_notice(f"Failed to open background image selector: {exc}")

    def on_background_file_chosen(self, chooser, response):
        try:
            if response == Gtk.ResponseType.ACCEPT:
                selected = chooser.get_file()
                path = selected.get_path() if selected else None
                if path:
                    self.background_image_path = path
                    self.background_path_entry.set_text(path)
        except Exception as exc:
            self.set_notice(f"Failed to read selected background image: {exc}")
        finally:
            chooser.hide()
            if self.background_file_chooser is chooser:
                self.background_file_chooser = None

    def on_background_apply(self, *_args):
        if self.background_apply_pending:
            self.set_notice("A background update is already in progress.")
            return
        if self.current_status.get("storage_quarantined"):
            self.set_notice("MacroPad USB storage is quarantined. Reconnect the MacroPad before applying a background image.")
            return
        if self.current_status.get("storage_guarded"):
            self.set_notice("Background update requires USB Mass Storage and is unavailable in Safe Mode.")
            return
        if not self.current_status.get("connected"):
            self.set_notice("Connect the MacroPad before applying a background image.")
            return
        source = Path(self.background_image_path) if self.background_image_path else None
        if source is None or not source.is_file():
            self.set_notice("Select a valid background image first.")
            return
        theme = "dark" if int(self.background_theme.get_selected()) == 0 else "light"
        filename = BACKGROUND_FILENAMES[theme]
        self.background_apply_pending = True
        self.background_apply_button.set_sensitive(False)
        self.set_notice(f"Converting background to /tmp/eezopen/{filename}…")

        def worker():
            try:
                converter = background_converter_path()
                temp_dir = prepare_background_tmp_dir()
                output = temp_dir / filename
                proc = subprocess.run(
                    [sys.executable, str(converter), str(source), str(output)],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, timeout=30, check=False,
                )
                if proc.returncode != 0:
                    raise RuntimeError(proc.stdout.strip() or f"create_bg.py exited with code {proc.returncode}")
                result = ipc_request({"cmd": "install_background", "theme": theme, "bin_path": str(output)}, timeout=20.0)
            except Exception as exc:
                GLib.idle_add(self._background_apply_failed, exc)
            else:
                GLib.idle_add(self._background_apply_done, result)

        threading.Thread(target=worker, name="eezopen-background", daemon=True).start()

    def _background_apply_done(self, result):
        self.background_apply_pending = False
        filename = result.get("filename", "background.bin")
        note = f"Background written to MacroPad app_icons/{filename}. Converted file kept at {result.get('source', '/tmp/eezopen')}. Rotate the MacroPad wheel to change layout so the new background is reloaded."
        if result.get("theme") == "light":
            note += " The bg_light.bin filename is experimental and still needs confirmation on the physical device."
        self.set_notice(note)
        self.update_status()
        return False

    def _background_apply_failed(self, exc):
        self.background_apply_pending = False
        self.set_notice(f"Background update failed: {exc}")
        show_error_dialog(self, "Background update failed", exc)
        self.update_status()
        return False

    def on_background_remove(self, *_args):
        if self.current_status.get("storage_guarded"):
            self.set_notice("Removing the background requires USB Mass Storage and is unavailable in Safe Mode.")
            return
        if not self.current_status.get("connected"):
            self.set_notice("Connect the MacroPad before removing the background image.")
            return
        self.set_notice("Removing custom background files from the MacroPad…")

        def ok(result):
            removed = result.get("removed") or []
            if removed:
                detail = ", ".join(removed)
                self.set_notice(f"Background removed ({detail}). Rotate the MacroPad wheel to change layout and refresh the screen.")
            else:
                self.set_notice("No custom background files were present. Rotate the MacroPad wheel to change layout and refresh the screen.")
            self.update_status()
            return False

        def fail(exc):
            self.set_notice(f"Background removal failed: {exc}")
            show_error_dialog(self, "Background removal failed", exc)
            return False

        ipc_async({"cmd": "remove_background"}, ok, fail, timeout=20.0)

    def refresh_screen_script_fields(self):
        """Refresh only the editor fields for the selected Screen Script.

        This intentionally does not touch Gtk.StringList/Gtk.DropDown. Rebuilding
        the DropDown model from inside notify::selected can wedge GTK while its
        popover is still processing the selection change.
        """
        self.screen_script_updating = True
        try:
            if self.screen_scripts:
                self.screen_script_selected = max(0, min(self.screen_script_selected, len(self.screen_scripts) - 1))
                item = self.screen_scripts[self.screen_script_selected]
                self.screen_script_path = str(item.get("path") or "")
                self.screen_script_name_entry.set_text(str(item.get("name") or ""))
                self.screen_script_entry.set_text(self.screen_script_path)
                self.screen_script_args_entry.set_text(str(item.get("args") or ""))
            else:
                self.screen_script_selected = 0
                self.screen_script_path = ""
                self.screen_script_name_entry.set_text("")
                self.screen_script_entry.set_text("")
                self.screen_script_args_entry.set_text("")
            self.screen_script_dirty = False
        finally:
            self.screen_script_updating = False

    def refresh_screen_script_widgets(self):
        """Rebuild the saved-script list after structural changes only."""
        self.screen_script_updating = True
        try:
            while self.screen_script_model.get_n_items() > 0:
                self.screen_script_model.remove(0)
            for item in self.screen_scripts:
                self.screen_script_model.append(str(item.get("name") or Path(str(item.get("path") or "script")).stem))
            if self.screen_scripts:
                self.screen_script_selected = max(0, min(self.screen_script_selected, len(self.screen_scripts) - 1))
                self.screen_script_dropdown.set_selected(self.screen_script_selected)
            else:
                self.screen_script_selected = 0
        finally:
            self.screen_script_updating = False
        self.refresh_screen_script_fields()

    def on_screen_script_field_changed(self, *_args):
        if not self.screen_script_updating:
            self.screen_script_dirty = True

    def on_screen_script_selected(self, dropdown, _pspec):
        if self.screen_script_updating or not self.screen_scripts:
            return
        selected = int(dropdown.get_selected())
        if selected < 0 or selected >= len(self.screen_scripts):
            return

        # Do not rebuild the DropDown model here. The selection callback runs
        # while GTK is still handling the DropDown popover; mutating its model
        # at that point was causing the Configurator to freeze.
        self.screen_script_selected = selected
        self.refresh_screen_script_fields()

        requested_selected = selected

        def ok(result):
            # Keep the current GUI selection if the user has already selected
            # another entry while this IPC request was in flight.
            if self.screen_script_selected == requested_selected:
                self.set_notice("Screen Script selected.")
            return False

        def fail(exc):
            self.set_notice(f"Could not save Screen Script selection: {exc}")
            return False

        ipc_async(
            {"cmd": "set_screen_script_selected", "selected": requested_selected},
            ok,
            fail,
            timeout=4.0,
        )

    def persist_screen_scripts(self, notice: str | None = None):
        payload = {
            "cmd": "set_screen_scripts",
            "scripts": self.screen_scripts,
            "selected": self.screen_script_selected,
        }

        def ok(result):
            self.screen_scripts = [dict(item) for item in result.get("scripts", self.screen_scripts)]
            self.screen_script_selected = int(result.get("selected", self.screen_script_selected))
            self.screen_script_dirty = False
            self.refresh_screen_script_widgets()
            if notice:
                self.set_notice(notice)
            return False

        def fail(exc):
            self.set_notice(f"Could not save Screen Scripts: {exc}")
            return False

        ipc_async(payload, ok, fail, timeout=4.0)

    def _show_screen_script_chooser(self, mode: str):
        try:
            if self.screen_script_file_chooser is not None:
                self.screen_script_file_chooser.show()
                return
            self.screen_script_file_chooser_mode = mode
            chooser = Gtk.FileChooserNative(
                title="Add Screen Script" if mode == "add" else "Change Screen Script",
                transient_for=self,
                action=Gtk.FileChooserAction.OPEN,
                accept_label="Add" if mode == "add" else "Select",
                cancel_label="Cancel",
            )
            self.screen_script_file_chooser = chooser
            chooser.connect("response", self.on_screen_script_file_chosen)
            chooser.show()
        except Exception as exc:
            self.screen_script_file_chooser = None
            self.set_notice(f"Failed to open Screen Script selector: {exc}")

    def on_screen_script_add(self, *_args):
        self._show_screen_script_chooser("add")

    def on_screen_script_select(self, *_args):
        if not self.screen_scripts:
            self._show_screen_script_chooser("add")
        else:
            self._show_screen_script_chooser("change")

    def on_screen_script_file_chosen(self, chooser, response):
        try:
            if response == Gtk.ResponseType.ACCEPT:
                selected = chooser.get_file()
                path = selected.get_path() if selected else None
                if path:
                    if self.screen_script_file_chooser_mode == "add":
                        self.screen_scripts.append({"name": Path(path).stem, "path": path, "args": ""})
                        self.screen_script_selected = len(self.screen_scripts) - 1
                        self.refresh_screen_script_widgets()
                        self.persist_screen_scripts(f"Screen Script added: {Path(path).name}")
                    elif self.screen_scripts:
                        self.screen_scripts[self.screen_script_selected]["path"] = path
                        if not str(self.screen_scripts[self.screen_script_selected].get("name") or "").strip():
                            self.screen_scripts[self.screen_script_selected]["name"] = Path(path).stem
                        self.refresh_screen_script_widgets()
                        self.persist_screen_scripts(f"Screen Script changed: {Path(path).name}")
        except Exception as exc:
            self.set_notice(f"Failed to read selected Screen Script: {exc}")
        finally:
            chooser.hide()
            if self.screen_script_file_chooser is chooser:
                self.screen_script_file_chooser = None

    def on_screen_script_save(self, *_args):
        if not self.screen_scripts:
            self.set_notice("Add a Screen Script first.")
            return
        item = self.screen_scripts[self.screen_script_selected]
        name = self.screen_script_name_entry.get_text().strip()
        path = self.screen_script_entry.get_text().strip()
        arguments = self.screen_script_args_entry.get_text().strip()
        if not name:
            name = Path(path).stem if path else "Screen Script"
        if not path:
            self.set_notice("Select a valid Screen Script file.")
            return
        try:
            shlex.split(arguments)
        except ValueError as exc:
            self.set_notice(f"Invalid Screen Script arguments: {exc}")
            return
        item.update({"name": name, "path": path, "args": arguments})
        self.refresh_screen_script_widgets()
        self.persist_screen_scripts(f"Screen Script saved: {name}")

    def on_screen_script_remove(self, *_args):
        if not self.screen_scripts:
            return
        removed = self.screen_scripts.pop(self.screen_script_selected)
        if self.screen_scripts:
            self.screen_script_selected = min(self.screen_script_selected, len(self.screen_scripts) - 1)
        else:
            self.screen_script_selected = 0
        self.refresh_screen_script_widgets()
        self.persist_screen_scripts(f"Screen Script removed: {removed.get('name') or Path(str(removed.get('path') or '')).name}")

    def on_screen_script_run(self, _button):
        if self.screen_script_request_pending:
            return

        active = bool(self.current_status.get("screen_script_active"))
        if not active:
            if not self.screen_scripts:
                self.set_notice("Add a Screen Script first.")
                return
            item = self.screen_scripts[self.screen_script_selected]
            source = Path(str(item.get("path") or ""))
            if not source.is_file():
                self.set_notice("Select a valid Screen Script first.")
                return
            if not self.current_status.get("connected"):
                self.set_notice("Connect the MacroPad before starting the Screen Script.")
                return
            arguments = str(item.get("args") or "")
            try:
                shlex.split(arguments)
            except ValueError as exc:
                self.set_notice(f"Invalid Screen Script arguments: {exc}")
                return

            self.screen_script_request_pending = True
            self.screen_script_run_button.set_sensitive(False)
            self.screen_script_run_button.set_label("Starting…")
            self.set_notice("Starting Screen Script. Host-side key actions remain active; daemon CDC transmission, PC Monitor telemetry, and USB Mass Storage changes pause until the Screen Script stops.")

            def ok(result):
                self.screen_script_request_pending = False
                self.update_status()
                self.set_notice(
                    f"Screen Script running (PID {result.get('pid')}). "
                    "Host-side key actions remain available; daemon CDC TX, PC Monitor, and USB Mass Storage changes are paused."
                )
                return False

            def fail(exc):
                self.screen_script_request_pending = False
                self.update_status()
                self.set_notice(f"Failed to start Screen Script: {exc}")
                show_error_dialog(self, "Failed to start Screen Script", exc)
                return False

            ipc_async({
                "cmd": "start_screen_script",
                "script": str(source),
                "name": str(item.get("name") or source.stem),
                "args": arguments,
            }, ok, fail, timeout=8.0)
            return

        self.screen_script_request_pending = True
        self.screen_script_run_button.set_sensitive(False)
        self.screen_script_run_button.set_label("Stopping…")
        self.set_notice("Stopping Screen Script. EezOpen serial connection remains active.")

        def ok(_result):
            self.screen_script_request_pending = False
            self.update_status()
            self.set_notice("Screen Script stopped. PC Monitor display telemetry resumed.")
            return False

        def fail(exc):
            self.screen_script_request_pending = False
            self.update_status()
            self.set_notice(f"Failed to stop Screen Script: {exc}")
            show_error_dialog(self, "Failed to stop Screen Script", exc)
            return False

        ipc_async({"cmd": "stop_screen_script"}, ok, fail, timeout=8.0)

    def on_theme(self, _button, theme: str, popover):
        popover.popdown()
        try:
            ipc_request({"cmd": "set_theme", "theme": theme}); self.set_notice(f"Theme {'dark' if theme == 'dark' else 'light'} sent. The MacroPad should reboot and reconnect.")
        except Exception as exc:
            self.set_notice(f"Failed to change theme: {exc}")
            show_error_dialog(self, "Failed to change MacroPad theme", exc)

    def on_rgb(self, *_): RgbWindow(self).present()

    def _firmware_window_closed(self, _window):
        self.firmware_window = None
        return False

    def on_firmware(self, *_):
        if self.current_status.get("storage_guarded"):
            self.set_notice("Firmware update requires USB Mass Storage and is unavailable in Safe Mode.")
            return
        if self.firmware_window is not None:
            self.firmware_window.present()
            return
        self.firmware_window = FirmwareWindow(self)
        self.firmware_window.connect("close-request", self._firmware_window_closed)
        self.firmware_window.present()

    def on_key_clicked(self, _button, key: int):
        try:
            response = ipc_request({"cmd": "get_key", "profile": self.current_profile, "key": key})
            cfg = response.get("config") or {}
            cfg["icon_path"] = response.get("icon_path")
        except Exception as exc:
            self.set_notice(f"Failed to read key: {exc}")
            show_error_dialog(self, "Failed to open key configuration", exc)
            return
        KeyEditor(self, self.current_profile, key, cfg).present()


class EezOpenConfigApp(Gtk.Application):
    def __init__(self): super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
    def do_activate(self):
        win = self.props.active_window or MainWindow(self); win.present()


def main() -> int: return EezOpenConfigApp().run(sys.argv)
if __name__ == "__main__": raise SystemExit(main())
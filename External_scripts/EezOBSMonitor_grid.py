#!/usr/bin/env python3
"""
EezOBS - OBS Studio status monitor for the EezBotFun 8-Key MacroPad

Part of the EezOpen project.

Author: Bruno Dias da Silva

Project: EezOpen

Description:

    Displays OBS Studio audio sources, current scene and recording status
    on the MacroPad LCD using the EezBotFun Customised Display USB CDC
    Grid layout validated with firmware v39.

    OBS Studio status is read through the OBS WebSocket v5 API.

Copyright (c) 2026 Bruno Dias da Silva

Licensed under the MIT License.

See LICENSE for details.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import time
from typing import Optional

try:
    import obsws_python as obs
except ImportError:
    raise SystemExit(
        "obsws-python not found.\n"
        "Install it with:\n"
        "  pip install obsws-python"
    )

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    raise SystemExit(
        "pyserial not found.\n"
        "On Ubuntu/Debian install it with:\n"
        "  sudo apt install python3-serial"
    )


EEZ_VID = 0x303A
EEZ_PID = 0x4007
MAX_PAYLOAD = 2043

BG = "#080C12"
WHITE = "#FFFFFF"
DIM = "#A9B4C0"
CYAN = "#00D8FF"
GREEN = "#00FF66"
YELLOW = "#FFD000"
RED = "#FF4040"

SCENE_BG = "#08243A"
LIVE_BG = "#082A1A"
WARN_BG = "#3A2C00"
REC_BG = "#3A0D0D"
STOP_BG = "#171C24"
BORDER = "#294154"

MAX_AUDIO_SOURCES = 6
AUDIO_DISCOVERY_INTERVAL = 10.0

DEFAULT_OBS_HOST = "192.168.1.20"
DEFAULT_OBS_PORT = 4444


def load_config(path: str) -> dict:
    """
    Load a simple EezOBS configuration file.

    Supported keys:

        obs-host=192.168.1.20
        obs-port=4455
        obs-password=secret
        audio-source=Mic/Aux
        audio-source=Desktop Audio

    ``audio-source`` may be repeated. Blank lines and lines beginning with
    ``#`` or ``;`` are ignored.
    """

    config_path = os.path.abspath(os.path.expanduser(path))
    config: dict[str, object] = {
        "audio-source": [],
    }

    allowed = {
        "obs-host",
        "obs-port",
        "obs-password",
        "audio-source",
    }

    try:
        with open(config_path, "r", encoding="utf-8") as handle:
            for lineno, raw_line in enumerate(handle, 1):
                line = raw_line.strip()

                if not line or line.startswith(("#", ";")):
                    continue

                if "=" not in line:
                    raise ValueError(
                        f"{config_path}:{lineno}: expected key=value"
                    )

                key, value = line.split("=", 1)
                key = key.strip().lower()
                value = value.strip()

                if key not in allowed:
                    raise ValueError(
                        f"{config_path}:{lineno}: unsupported option {key!r}"
                    )

                if key == "audio-source":
                    if not value:
                        raise ValueError(
                            f"{config_path}:{lineno}: audio-source cannot be empty"
                        )
                    audio_sources = config["audio-source"]
                    assert isinstance(audio_sources, list)
                    audio_sources.append(value)
                    continue

                if key == "obs-port":
                    try:
                        port = int(value)
                    except ValueError as exc:
                        raise ValueError(
                            f"{config_path}:{lineno}: obs-port must be an integer"
                        ) from exc

                    if not 1 <= port <= 65535:
                        raise ValueError(
                            f"{config_path}:{lineno}: obs-port must be between 1 and 65535"
                        )

                    config[key] = port
                    continue

                config[key] = value

    except OSError as exc:
        raise ValueError(f"Unable to read config file {config_path}: {exc}") from exc

    return config


def resolve_obs_settings(args: argparse.Namespace) -> None:
    """
    Resolve OBS settings while keeping command-line arguments available.

    Precedence, from highest to lowest:

        command line > config file > environment > built-in defaults

    If at least one ``--audio-source`` is supplied on the command line, the
    command-line list replaces the list from the config file. This makes it
    easy to test individual sources without editing the config.
    """

    config: dict[str, object] = {}

    if args.config:
        config = load_config(args.config)

    if args.obs_host is None:
        args.obs_host = str(
            config.get(
                "obs-host",
                os.environ.get("OBS_HOST", DEFAULT_OBS_HOST),
            )
        )

    if args.obs_port is None:
        raw_port = config.get(
            "obs-port",
            os.environ.get("OBS_PORT", str(DEFAULT_OBS_PORT)),
        )
        try:
            args.obs_port = int(raw_port)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid OBS port: {raw_port!r}") from exc

    if not 1 <= args.obs_port <= 65535:
        raise ValueError("OBS port must be between 1 and 65535")

    if args.obs_password is None:
        args.obs_password = str(
            config.get(
                "obs-password",
                os.environ.get("OBS_PASSWORD", ""),
            )
        )

    if args.audio_source is None:
        configured_sources = config.get("audio-source", [])
        if isinstance(configured_sources, list):
            args.audio_source = [str(name) for name in configured_sources]
        else:
            args.audio_source = []

    args.audio_source = args.audio_source[:MAX_AUDIO_SOURCES]


def find_macropad_port() -> str:
    for port in list_ports.comports():
        if port.vid == EEZ_VID and port.pid == EEZ_PID:
            return port.device

    raise RuntimeError(
        "EezBotFun MacroPad not found automatically. "
        "Use --serial-port /dev/ttyACM0"
    )


def send_cus(ser: serial.Serial, obj: dict) -> None:
    payload = json.dumps(
        obj,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    if b"\x00" in payload:
        raise ValueError("NUL found in JSON payload")

    if not 1 <= len(payload) <= MAX_PAYLOAD:
        raise ValueError(f"Invalid payload size: {len(payload)} bytes")

    frame = b"cus" + struct.pack(">H", len(payload)) + payload

    written = ser.write(frame)
    ser.flush()

    if written != len(frame):
        raise IOError(f"Short write: {written}/{len(frame)}")


def panel(
    x: int,
    y: int,
    w: int,
    h: int,
    text: str,
    *,
    fg: str = WHITE,
    bg: str = BG,
    align: str = "CENTER",
    cmd: str = "update",
    border: int = 0,
    border_color: str = BORDER,
    clear_canvas: bool = False,
) -> dict:
    obj = {
        "cmd": cmd,
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "text": text,
        "fg": fg,
        "bg": bg,
        "align": align,
        "long_mode": "CLIP",
    }

    if border:
        obj["border"] = border
        obj["border-color"] = border_color
        obj["border-radius"] = 4

    if clear_canvas:
        obj["clear_canvas"] = True

    return obj


def compact_timecode(value: Optional[str]) -> str:
    if not value:
        return ""

    value = str(value)

    if "." in value:
        value = value.split(".", 1)[0]

    return value


def shorten(value: str, max_len: int) -> str:
    value = str(value).strip()

    if len(value) <= max_len:
        return value

    if max_len <= 3:
        return value[:max_len]

    return value[: max_len - 3] + "..."


class ObsState:
    def __init__(self) -> None:
        self.connected = False
        self.scene = "NO CONNECTION"
        self.recording = "UNKNOWN"
        self.record_time = ""
        self.audio_sources: list[tuple[str, Optional[bool]]] = []
        self.error = ""


class ObsMonitor:
    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        timeout: float,
        audio_sources: Optional[list[str]] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.client = None

        self.explicit_audio_sources = list(audio_sources or [])
        self.discovered_audio_sources: list[str] = []
        self.last_audio_discovery = 0.0

    def connect(self) -> None:
        self.client = obs.ReqClient(
            host=self.host,
            port=self.port,
            password=self.password,
            timeout=self.timeout,
        )

    def disconnect(self) -> None:
        client = self.client
        self.client = None

        if client is None:
            return

        try:
            if hasattr(client, "disconnect"):
                client.disconnect()
        except Exception:
            pass

    def _get_input_names(self) -> list[str]:
        assert self.client is not None

        names: list[str] = []

        try:
            response = self.client.get_input_list()
            items = getattr(response, "inputs", None)

            if items is None:
                raise AttributeError("GetInputList response has no 'inputs'")
        except Exception:
            raw = self.client.send("GetInputList", raw=True)

            if isinstance(raw, dict):
                items = raw.get("inputs", [])
            else:
                items = []

        for item in items:
            if not isinstance(item, dict):
                continue

            name = item.get("inputName") or item.get("input_name")

            if name and name not in names:
                names.append(str(name))

        return names

    def discover_audio_sources(self) -> list[str]:
        assert self.client is not None

        audio_sources: list[str] = []

        for name in self._get_input_names():
            try:
                self.client.get_input_mute(name)
            except Exception:
                # Inputs without audio normally do not provide a mute state.
                continue

            audio_sources.append(name)

            if len(audio_sources) >= MAX_AUDIO_SOURCES:
                break

        self.discovered_audio_sources = audio_sources
        self.last_audio_discovery = time.monotonic()

        return audio_sources

    def current_audio_sources(self) -> list[str]:
        if self.explicit_audio_sources:
            return self.explicit_audio_sources[:MAX_AUDIO_SOURCES]

        now = time.monotonic()

        if (
            not self.discovered_audio_sources
            or now - self.last_audio_discovery >= AUDIO_DISCOVERY_INTERVAL
        ):
            return self.discover_audio_sources()

        return self.discovered_audio_sources[:MAX_AUDIO_SOURCES]

    def list_audio_sources(self) -> list[str]:
        if self.client is None:
            self.connect()

        return self.discover_audio_sources()

    def read(self) -> ObsState:
        state = ObsState()

        if self.client is None:
            self.connect()

        assert self.client is not None

        try:
            scene_resp = self.client.get_current_program_scene()

            scene = getattr(scene_resp, "scene_name", None)
            if not scene:
                scene = getattr(
                    scene_resp,
                    "current_program_scene_name",
                    "UNKNOWN",
                )

            record_resp = self.client.get_record_status()

            output_active = bool(
                getattr(record_resp, "output_active", False)
            )
            output_paused = bool(
                getattr(record_resp, "output_paused", False)
            )

            if not output_active:
                recording = "STOPPED"
            elif output_paused:
                recording = "PAUSED"
            else:
                recording = "RECORDING"

            record_time = compact_timecode(
                getattr(record_resp, "output_timecode", "")
            )

            audio_states: list[tuple[str, Optional[bool]]] = []

            for name in self.current_audio_sources():
                try:
                    mute_resp = self.client.get_input_mute(name)
                    muted = bool(
                        getattr(mute_resp, "input_muted", False)
                    )
                except Exception:
                    muted = None

                audio_states.append((name, muted))

            state.connected = True
            state.scene = str(scene)
            state.recording = recording
            state.record_time = record_time
            state.audio_sources = audio_states

            return state

        except Exception as exc:
            self.disconnect()
            state.error = str(exc)
            return state


def audio_card_style(muted: Optional[bool]) -> tuple[str, str, str]:
    if muted is True:
        return RED, REC_BG, "MUTED"

    if muted is False:
        return GREEN, LIVE_BG, "LIVE"

    return YELLOW, WARN_BG, "UNKNOWN"


def build_grid_widgets(
    state: ObsState,
    *,
    obs_host: str,
    obs_port: int,
) -> list[dict]:
    """
    Build the initial 480x200 Grid layout.

    fullscreen=False preserves the firmware-owned lower key area.
    Grid behavior was validated on firmware v39.
    """

    header_fg = GREEN if state.connected else RED
    header_text = f"EEZOBS {obs_host}:{obs_port} {time.strftime('%H:%M')}"

    widgets: list[dict] = [
        {
            "id": 0,
            "type": "text",
            "row": 0,
            "col": 0,
            "col_span": 2,
            "text": header_text,
            "fg": header_fg,
            "bg": BG,
            "align": "CENTER",
            "long_mode": "CLIP",
        }
    ]

    padded: list[tuple[str, Optional[bool]] | None] = list(
        state.audio_sources[:MAX_AUDIO_SOURCES] if state.connected else []
    )

    while len(padded) < MAX_AUDIO_SOURCES:
        padded.append(None)

    # Six audio cards: rows 1..3, two columns.
    for idx, slot in enumerate(padded):
        row = 1 + (idx // 2)
        col = idx % 2

        if slot is None:
            card_text = ""
            fg = DIM
            bg = BG
            border = 0
            border_color = BORDER
        else:
            name, muted = slot
            fg, bg, status = audio_card_style(muted)
            name = shorten(name, 17)
            card_text = f"{name}  {status}"
            border = 1
            border_color = fg

        widgets.append(
            {
                "id": idx + 1,
                "type": "text",
                "row": row,
                "col": col,
                "text": card_text,
                "fg": fg,
                "bg": bg,
                "align": "CENTER",
                "long_mode": "CLIP",
                "border": border,
                "border-color": border_color,
                "border-radius": 4,
            }
        )

    if state.recording == "RECORDING":
        rec_fg = RED
        rec_bg = REC_BG
        rec_status = "RECORDING"
    elif state.recording == "PAUSED":
        rec_fg = YELLOW
        rec_bg = WARN_BG
        rec_status = "PAUSED"
    elif state.recording == "STOPPED":
        rec_fg = DIM
        rec_bg = STOP_BG
        rec_status = "STOPPED"
    else:
        rec_fg = RED
        rec_bg = STOP_BG
        rec_status = "UNKNOWN"

    if state.record_time and state.recording in ("RECORDING", "PAUSED"):
        rec_line = f"{rec_status} {state.record_time}"
    else:
        rec_line = rec_status

    scene_name = shorten(state.scene.strip() or "UNKNOWN", 23)

    widgets.extend(
        [
            {
                "id": 7,
                "type": "text",
                "row": 4,
                "col": 0,
                "text": f"REC\n{rec_line}",
                "fg": rec_fg,
                "bg": rec_bg,
                "align": "CENTER",
                "long_mode": "CLIP",
                "border": 1,
                "border-color": rec_fg,
                "border-radius": 4,
            },
            {
                "id": 8,
                "type": "text",
                "row": 4,
                "col": 1,
                "text": f"SCENE\n{scene_name}",
                "fg": CYAN if state.connected else RED,
                "bg": SCENE_BG if state.connected else STOP_BG,
                "align": "CENTER",
                "long_mode": "CLIP",
                "border": 1,
                "border-color": CYAN if state.connected else RED,
                "border-radius": 4,
            },
        ]
    )

    return widgets


def start_grid(
    ser: serial.Serial,
    state: ObsState,
    *,
    obs_host: str,
    obs_port: int,
) -> None:
    """
    Create the Grid once.

    row 0    -> header
    rows 1-3 -> six audio cards
    row 4    -> REC / SCENE
    """

    send_cus(
        ser,
        {
            "cmd": "start",
            "layout": "grid",
            "fullscreen": False,
            "grid": {
                "cols": ["*", "*"],
                "rows": ["24", "30", "30", "30", "*"],
                "gap": 3,
                "pad": 0,
            },
            "widgets": build_grid_widgets(
                state,
                obs_host=obs_host,
                obs_port=obs_port,
            ),
        },
    )


def build_grid_updates(
    state: ObsState,
    *,
    obs_host: str,
    obs_port: int,
) -> list[dict]:
    """
    Build one consolidated update for all dynamic widgets.

    This replaces the previous series of independent Absolute-mode writes.
    """

    header_fg = GREEN if state.connected else RED
    header_text = f"EEZOBS {obs_host}:{obs_port} {time.strftime('%H:%M')}"

    updates: list[dict] = [
        {
            "id": 0,
            "text": header_text,
            "fg": header_fg,
            "bg": BG,
        }
    ]

    padded: list[tuple[str, Optional[bool]] | None] = list(
        state.audio_sources[:MAX_AUDIO_SOURCES] if state.connected else []
    )

    while len(padded) < MAX_AUDIO_SOURCES:
        padded.append(None)

    for idx, slot in enumerate(padded):
        if slot is None:
            updates.append(
                {
                    "id": idx + 1,
                    "text": "",
                    "fg": DIM,
                    "bg": BG,
                    "border": 0,
                    "border-color": BORDER,
                }
            )
        else:
            name, muted = slot
            fg, bg, status = audio_card_style(muted)
            name = shorten(name, 17)

            updates.append(
                {
                    "id": idx + 1,
                    "text": f"{name}  {status}",
                    "fg": fg,
                    "bg": bg,
                    "border": 1,
                    "border-color": fg,
                }
            )

    if state.recording == "RECORDING":
        rec_fg = RED
        rec_bg = REC_BG
        rec_status = "RECORDING"
    elif state.recording == "PAUSED":
        rec_fg = YELLOW
        rec_bg = WARN_BG
        rec_status = "PAUSED"
    elif state.recording == "STOPPED":
        rec_fg = DIM
        rec_bg = STOP_BG
        rec_status = "STOPPED"
    else:
        rec_fg = RED
        rec_bg = STOP_BG
        rec_status = "UNKNOWN"

    if state.record_time and state.recording in ("RECORDING", "PAUSED"):
        rec_line = f"{rec_status} {state.record_time}"
    else:
        rec_line = rec_status

    updates.append(
        {
            "id": 7,
            "text": f"REC\n{rec_line}",
            "fg": rec_fg,
            "bg": rec_bg,
            "border": 1,
            "border-color": rec_fg,
        }
    )

    scene_name = shorten(state.scene.strip() or "UNKNOWN", 23)

    updates.append(
        {
            "id": 8,
            "text": f"SCENE\n{scene_name}",
            "fg": CYAN if state.connected else RED,
            "bg": SCENE_BG if state.connected else STOP_BG,
            "border": 1,
            "border-color": CYAN if state.connected else RED,
        }
    )

    return updates


def update_grid(
    ser: serial.Serial,
    state: ObsState,
    *,
    obs_host: str,
    obs_port: int,
) -> None:
    send_cus(
        ser,
        {
            "cmd": "update",
            "layout": "grid",
            "set": build_grid_updates(
                state,
                obs_host=obs_host,
                obs_port=obs_port,
            ),
        },
    )


def draw(
    ser: serial.Serial,
    state: ObsState,
    *,
    first: bool,
    obs_host: str,
    obs_port: int,
) -> None:
    """
    Firmware-v39 Grid renderer.

    The Grid is created only once. Later refreshes update widgets by ID in a
    single serial frame, instead of sending many Absolute-mode updates.
    """

    if first:
        start_grid(
            ser,
            state,
            obs_host=obs_host,
            obs_port=obs_port,
        )
    else:
        update_grid(
            ser,
            state,
            obs_host=obs_host,
            obs_port=obs_port,
        )

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Display OBS Studio audio sources, scene and recording status "
            "on an EezBotFun 8-Key MacroPad."
        )
    )

    parser.add_argument(
        "--config",
        metavar="FILE",
        help=(
            "Load OBS connection/audio settings from a config file. "
            "Command-line options override values from the file."
        ),
    )

    parser.add_argument(
        "--obs-host",
        default=None,
        help=(
            "OBS WebSocket host. Overrides config file/OBS_HOST environment."
        ),
    )
    parser.add_argument(
        "--obs-port",
        type=int,
        default=None,
        help=(
            "OBS WebSocket port. Overrides config file/OBS_PORT environment."
        ),
    )
    parser.add_argument(
        "--obs-password",
        default=None,
        help=(
            "OBS WebSocket password. Overrides config file/OBS_PASSWORD "
            "environment."
        ),
    )
    parser.add_argument(
        "--audio-source",
        action="append",
        default=None,
        metavar="NAME",
        help=(
            "Audio source to display. Repeat the option to choose multiple "
            "sources and their order. If supplied, this list overrides audio "
            "sources from the config file. If omitted everywhere, sources "
            "are auto-discovered."
        ),
    )
    parser.add_argument(
        "--list-audio",
        action="store_true",
        help="List auto-detected OBS audio sources and exit",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Refresh interval in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3.0,
        help="OBS WebSocket request timeout (default: %(default)s)",
    )
    parser.add_argument(
        "--serial-port",
        help="MacroPad serial port, e.g. /dev/ttyACM0",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=115200,
        help="MacroPad serial baud rate (default: %(default)s)",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    try:
        resolve_obs_settings(args)
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 2

    monitor = ObsMonitor(
        host=args.obs_host,
        port=args.obs_port,
        password=args.obs_password,
        timeout=args.timeout,
        audio_sources=args.audio_source,
    )

    if args.list_audio:
        try:
            names = monitor.list_audio_sources()
        except Exception as exc:
            print(f"Unable to query OBS: {exc}")
            return 1
        finally:
            monitor.disconnect()

        if not names:
            print("No audio sources detected.")
            return 0

        print("OBS audio sources:")
        for name in names:
            print(f"  {name}")

        return 0

    try:
        serial_port = args.serial_port or find_macropad_port()
    except RuntimeError as exc:
        print(exc)
        return 1

    try:
        ser = serial.Serial(
            serial_port,
            args.baud,
            timeout=1,
            write_timeout=2,
        )
    except (serial.SerialException, OSError) as exc:
        print(f"Unable to open {serial_port}: {exc}")
        return 1

    print("EezOBS")
    print(f"MacroPad: {serial_port}")
    print(f"OBS:      {args.obs_host}:{args.obs_port}")

    if args.audio_source:
        print("Audio sources:")
        for name in args.audio_source[:MAX_AUDIO_SOURCES]:
            print(f"  {name}")
    else:
        print("Audio sources: auto-discovery")

    print("Ctrl+C to exit")

    first = True
    time.sleep(0.25)

    try:
        while True:
            state = monitor.read()

            draw(
                ser,
                state,
                first=first,
                obs_host=args.obs_host,
                obs_port=args.obs_port,
            )

            first = False
            time.sleep(max(0.25, args.interval))

    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        monitor.disconnect()

        try:
            send_cus(ser, {"cmd": "stop"})
        except Exception:
            pass

        ser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
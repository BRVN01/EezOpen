#!/usr/bin/env python3
"""
EezOBS Monitor - OBS Studio status monitor for the EezBotFun 8-Key MacroPad

Part of the EezOpen project.

Author: Bruno Dias da Silva

Project: EezOpen

Description:

    Displays OBS Studio audio sources, current scene and recording status
    on the MacroPad LCD using the EezBotFun Customised Display USB CDC
    protocol.

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


def draw_audio_grid(
    ser: serial.Serial,
    audio_sources: list[tuple[str, Optional[bool]]],
) -> None:
    positions = [
        (0, 27),
        (242, 27),
        (0, 60),
        (242, 60),
        (0, 93),
        (242, 93),
    ]

    padded: list[tuple[str, Optional[bool]] | None] = list(
        audio_sources[:MAX_AUDIO_SOURCES]
    )

    while len(padded) < MAX_AUDIO_SOURCES:
        padded.append(None)

    for slot, pos in zip(padded, positions):
        x, y = pos

        if slot is None:
            text = ""
            fg = DIM
            bg = BG
            border_color = BORDER
        else:
            name, muted = slot
            fg, bg, status = audio_card_style(muted)

            # Keep the card on one line. The display uses a proportional font,
            # so source names are truncated instead of aligned with spaces.
            name = shorten(name, 17)
            text = f"{name}  {status}"
            border_color = fg

        send_cus(
            ser,
            panel(
                x,
                y,
                238,
                30,
                text,
                fg=fg,
                bg=bg,
                border=1 if slot is not None else 0,
                border_color=border_color,
            ),
        )

        time.sleep(0.008)


def draw(
    ser: serial.Serial,
    state: ObsState,
    *,
    first: bool,
    obs_host: str,
    obs_port: int,
) -> None:
    """
    Uses fixed opaque Absolute-mode regions without clearing every refresh.

    This keeps the display stable while several OBS values are updated.
    """

    if state.connected:
        header_fg = GREEN
    else:
        header_fg = RED

    # IP/port moved to the top to free the 480x200 body.
    header_text = (
        f"EEZOBS {obs_host}:{obs_port} {time.strftime('%H:%M')}"
    )

    send_cus(
        ser,
        panel(
            0,
            0,
            480,
            24,
            header_text,
            fg=header_fg,
            bg=BG,
            cmd="start" if first else "update",
            clear_canvas=first,
        ),
    )
    time.sleep(0.010)

    if state.connected:
        draw_audio_grid(ser, state.audio_sources)
    else:
        # Clear all six audio slots when OBS is offline.
        draw_audio_grid(ser, [])

    # Recording card: bottom-left.
    if state.recording == "RECORDING":
        rec_fg = GREEN
        rec_bg = LIVE_BG
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

    send_cus(
        ser,
        panel(
            0,
            127,
            238,
            73,
            f"REC\n{rec_line}",
            fg=rec_fg,
            bg=rec_bg,
            border=1,
            border_color=rec_fg,
        ),
    )
    time.sleep(0.010)

    # Scene moved to the old microphone card position.
    scene_name = state.scene.strip() or "UNKNOWN"
    scene_name = shorten(scene_name, 23)

    send_cus(
        ser,
        panel(
            242,
            127,
            238,
            73,
            f"SCENE\n{scene_name}",
            fg=CYAN if state.connected else RED,
            bg=SCENE_BG if state.connected else STOP_BG,
            border=1,
            border_color=CYAN if state.connected else RED,
        ),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Display OBS Studio audio sources, scene and recording status "
            "on an EezBotFun 8-Key MacroPad."
        )
    )

    parser.add_argument(
        "--obs-host",
        default=os.environ.get("OBS_HOST", "192.168.1.20"),
        help="OBS WebSocket host (default: %(default)s)",
    )
    parser.add_argument(
        "--obs-port",
        type=int,
        default=int(os.environ.get("OBS_PORT", "4444")),
        help="OBS WebSocket port (default: %(default)s)",
    )
    parser.add_argument(
        "--obs-password",
        default=os.environ.get("OBS_PASSWORD", ""),
        help="OBS WebSocket password; OBS_PASSWORD env var is also supported",
    )
    parser.add_argument(
        "--audio-source",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Audio source to display. Repeat the option to choose multiple "
            "sources and their order. If omitted, sources are auto-discovered."
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

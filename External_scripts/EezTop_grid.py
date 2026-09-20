#!/usr/bin/env python3

"""
EezTop - Linux system monitor for the EezBotFun 8-Key MacroPad

Part of the EezOpen project.

Author: Bruno Dias da Silva
Project: EezOpen
Description:
    Displays real-time Linux system information on the MacroPad LCD
    using the EezBotFun Customised Display USB CDC Grid layout
    validated with firmware v39.

Copyright (c) 2026 Bruno Dias da Silva

Licensed under the MIT License.
See LICENSE for details.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import time
from pathlib import Path
from typing import Optional

import serial
from serial.tools import list_ports

EEZ_VID = 0x303A
EEZ_PID = 0x4007
MAX_PAYLOAD = 2043
SCREEN_W = 480
SCREEN_H = 200

# Stronger, high-contrast palette for the MacroPad LCD.
BG = "#080C12"
WHITE = "#FFFFFF"
DIM = "#A9B4C0"
CYAN = "#00D8FF"
GREEN = "#00FF66"
YELLOW = "#FFD000"
RED = "#FF4040"

CPU_BG = "#082A1A"
RAM_BG = "#08243A"
WARN_BG = "#3A2C00"
CRIT_BG = "#3A0D0D"
BORDER = "#294154"

CLK_TCK = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


def find_port() -> str:
    for p in list_ports.comports():
        if p.vid == EEZ_VID and p.pid == EEZ_PID:
            return p.device
    raise RuntimeError("MacroPad nao encontrado; use --port /dev/ttyACM0")


def send_cus(ser: serial.Serial, obj: dict) -> None:
    payload = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if b"\x00" in payload:
        raise ValueError("NUL no JSON")
    if not 1 <= len(payload) <= MAX_PAYLOAD:
        raise ValueError(f"payload invalido: {len(payload)} bytes")

    frame = b"cus" + struct.pack(">H", len(payload)) + payload
    n = ser.write(frame)
    ser.flush()
    if n != len(frame):
        raise IOError(f"short write: {n}/{len(frame)}")


def panel(
    x: int,
    y: int,
    w: int,
    h: int,
    text: str,
    *,
    fg: str = WHITE,
    bg: str = BG,
    align: str = "LEFT",
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
        obj["border-radius"] = 3

    if clear_canvas:
        obj["clear_canvas"] = True

    return obj


def cpu_stat() -> tuple[int, int]:
    with open("/proc/stat", encoding="utf-8") as f:
        values = [int(x) for x in f.readline().split()[1:]]
    total = sum(values)
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return total - idle, total


def cpu_pct(old: tuple[int, int], new: tuple[int, int]) -> float:
    dbusy = new[0] - old[0]
    dtotal = new[1] - old[1]
    if dtotal <= 0:
        return 0.0
    return max(0.0, min(100.0, dbusy * 100.0 / dtotal))


def memory() -> tuple[float, float, float, int]:
    data: dict[str, int] = {}
    with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
            key, value = line.split(":", 1)
            data[key] = int(value.strip().split()[0])

    total_kib = data["MemTotal"]
    avail_kib = data.get("MemAvailable", data.get("MemFree", 0))
    used_kib = total_kib - avail_kib
    used_gib = used_kib / 1024 / 1024
    total_gib = total_kib / 1024 / 1024
    pct = used_kib * 100.0 / total_kib if total_kib else 0.0
    return used_gib, total_gib, pct, total_kib * 1024


def cpu_temp() -> Optional[float]:
    candidates: list[tuple[int, float]] = []

    for hwmon in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            chip = (hwmon / "name").read_text().strip().lower()
        except OSError:
            chip = ""

        for inp in hwmon.glob("temp*_input"):
            try:
                value = int(inp.read_text().strip()) / 1000.0
            except (OSError, ValueError):
                continue
            if not 0 <= value <= 125:
                continue

            label_path = inp.with_name(inp.name.replace("_input", "_label"))
            try:
                label = label_path.read_text().strip().lower()
            except OSError:
                label = ""

            score = 100
            if chip == "k10temp":
                score -= 50
            if "coretemp" in chip:
                score -= 40
            if label == "tctl":
                score -= 30
            elif label == "tdie":
                score -= 25
            elif "package" in label:
                score -= 20
            elif "cpu" in label:
                score -= 15

            candidates.append((score, value))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def uptime_short() -> str:
    try:
        seconds = int(float(Path("/proc/uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        return "?"

    days, seconds = divmod(seconds, 86400)
    hours, _ = divmod(seconds, 3600)
    return f"{days}d{hours}h" if days else f"{hours}h"


def read_proc_snapshot() -> dict[int, tuple[int, int, str]]:
    snapshot: dict[int, tuple[int, int, str]] = {}

    try:
        entries = os.scandir("/proc")
    except OSError:
        return snapshot

    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue

            try:
                pid = int(entry.name)
                raw = Path(entry.path, "stat").read_text(encoding="utf-8")
                left = raw.find("(")
                right = raw.rfind(")")
                if left < 0 or right <= left:
                    continue

                comm = raw[left + 1:right]
                fields = raw[right + 2:].split()
                ticks = int(fields[11]) + int(fields[12])
                rss_pages = int(fields[21])
                snapshot[pid] = (ticks, rss_pages, comm)
            except (OSError, ValueError, IndexError):
                continue

    return snapshot


def process_rows(
    previous: dict[int, tuple[int, int, str]],
    current: dict[int, tuple[int, int, str]],
    elapsed: float,
    mem_total_bytes: int,
    limit: int = 5,
) -> list[tuple[int, float, float, str]]:
    """
    Return process rows as:
        (pid, cpu_percent, resident_memory_mib, command)

    ``mem_total_bytes`` is kept in the signature for compatibility with the
    existing caller, but per-process memory is now displayed as actual RSS
    consumption instead of percentage of total RAM.
    """
    rows: list[tuple[int, float, float, str]] = []
    elapsed = max(elapsed, 0.001)

    for pid, (ticks, rss_pages, comm) in current.items():
        old = previous.get(pid)
        if old is None:
            continue

        delta_ticks = ticks - old[0]
        if delta_ticks < 0:
            continue

        cpu = (delta_ticks / CLK_TCK) * 100.0 / elapsed
        mem_mib = rss_pages * PAGE_SIZE / 1024.0 / 1024.0
        rows.append((pid, cpu, mem_mib, comm))

    rows.sort(key=lambda row: (row[1], row[2]), reverse=True)
    return rows[:limit]


def severity(value: float, warn: float = 70.0, crit: float = 90.0) -> tuple[str, str]:
    if value >= crit:
        return RED, CRIT_BG
    if value >= warn:
        return YELLOW, WARN_BG
    return GREEN, CPU_BG


def ram_severity(value: float) -> tuple[str, str]:
    if value >= 90:
        return RED, CRIT_BG
    if value >= 75:
        return YELLOW, WARN_BG
    return CYAN, RAM_BG


def fmt_process_mem(mem_mib: float) -> str:
    """Format process RSS using MiB/GiB rather than RAM percentage."""
    if mem_mib >= 1024.0:
        return f"{mem_mib / 1024.0:.1f}G"
    if mem_mib >= 100.0:
        return f"{mem_mib:.0f}M"
    if mem_mib >= 10.0:
        return f"{mem_mib:.1f}M"
    return f"{mem_mib:.2f}M"


def temp_color(temp: Optional[float]) -> str:
    if temp is None:
        return DIM
    if temp >= 85:
        return RED
    if temp >= 70:
        return YELLOW
    return GREEN


def build_grid_widgets(
    *,
    hostname: str,
    cpu: float,
    temp: Optional[float],
    mem_used: float,
    mem_total: float,
    mem_pct: float,
    loads: tuple[float, float, float],
    procs: list[tuple[int, float, float, str]],
    fullscreen: bool,
) -> list[dict]:
    """
    Build the initial 480x200 Grid layout.

    fullscreen=False preserves the firmware-owned lower key area.
    fullscreen=True uses the full 480x320 LCD.
    The process table uses four multiline widgets instead of one widget per
    cell, keeping the widget count small and updates compact.
    """
    cpu_fg, cpu_bg = severity(cpu)
    ram_fg, ram_bg = ram_severity(mem_pct)
    temp_s = f"{temp:.0f}C" if temp is not None else "N/A"
    load1, load5, load15 = loads

    header = (
        f"{hostname[:10]}  {temp_s}  UP {uptime_short()}  "
        f"{time.strftime('%H:%M')}  L {load1:.2f}/{load5:.2f}/{load15:.2f}"
    )

    process_limit = 10 if fullscreen else 5

    pid_lines = ["PID"]
    cpu_lines = ["CPU"]
    mem_lines = ["RSS"]
    cmd_lines = ["COMMAND"]

    for pid, pcpu, mem_mib, comm in procs[:process_limit]:
        pid_lines.append(str(pid))
        cpu_lines.append(f"{pcpu:.1f}%")
        mem_lines.append(fmt_process_mem(mem_mib))
        cmd_lines.append(comm[:18])

    while len(pid_lines) < process_limit + 1:
        pid_lines.append("")
        cpu_lines.append("")
        mem_lines.append("")
        cmd_lines.append("")

    ram_text = (
        f"RAM  {mem_used:.1f}/{mem_total:.0f}G"
        if fullscreen
        else f"RAM  {mem_pct:4.1f}%  {mem_used:.1f}/{mem_total:.0f}G"
    )

    return [
        {
            "id": 0,
            "type": "text",
            "row": 0,
            "col": 0,
            "col_span": 4,
            "text": header,
            "fg": CYAN,
            "bg": BG,
            "align": "CENTER",
            "long_mode": "CLIP",
        },
        {
            "id": 1,
            "type": "text",
            "row": 1,
            "col": 0,
            "col_span": 2,
            "text": f"CPU  {cpu:4.1f}%",
            "fg": cpu_fg,
            "bg": cpu_bg,
            "align": "CENTER",
            "long_mode": "CLIP",
            "border": 1,
            "border-color": cpu_fg,
            "border-radius": 4,
        },
        {
            "id": 2,
            "type": "text",
            "row": 1,
            "col": 2,
            "col_span": 2,
            "text": ram_text,
            "fg": ram_fg,
            "bg": ram_bg,
            "align": "CENTER",
            "long_mode": "CLIP",
            "border": 1,
            "border-color": ram_fg,
            "border-radius": 4,
        },
        {
            "id": 3,
            "type": "text",
            "row": 2,
            "col": 0,
            "text": "\n".join(pid_lines),
            "fg": DIM,
            "bg": BG,
            "align": "RIGHT",
            "long_mode": "CLIP",
        },
        {
            "id": 4,
            "type": "text",
            "row": 2,
            "col": 1,
            "text": "\n".join(cpu_lines),
            "fg": GREEN,
            "bg": BG,
            "align": "RIGHT",
            "long_mode": "CLIP",
        },
        {
            "id": 5,
            "type": "text",
            "row": 2,
            "col": 2,
            "text": "\n".join(mem_lines),
            "fg": CYAN,
            "bg": BG,
            "align": "RIGHT",
            "long_mode": "CLIP",
        },
        {
            "id": 6,
            "type": "text",
            "row": 2,
            "col": 3,
            "text": "\n".join(cmd_lines),
            "fg": WHITE,
            "bg": BG,
            "align": "LEFT",
            "long_mode": "CLIP",
        },
    ]


def start_grid(
    ser: serial.Serial,
    *,
    hostname: str,
    cpu: float,
    temp: Optional[float],
    mem_used: float,
    mem_total: float,
    mem_pct: float,
    loads: tuple[float, float, float],
    procs: list[tuple[int, float, float, str]],
    fullscreen: bool,
) -> None:
    send_cus(
        ser,
        {
            "cmd": "start",
            "layout": "grid",
            "fullscreen": fullscreen,
            "grid": {
                "cols": ["96", "84", "84", "*"],
                "rows": ["25", "31", "*"],
                "gap": 3,
                "pad": 0,
            },
            "widgets": build_grid_widgets(
                hostname=hostname,
                cpu=cpu,
                temp=temp,
                mem_used=mem_used,
                mem_total=mem_total,
                mem_pct=mem_pct,
                loads=loads,
                procs=procs,
                fullscreen=fullscreen,
            ),
        },
    )


def update_grid(
    ser: serial.Serial,
    *,
    hostname: str,
    cpu: float,
    temp: Optional[float],
    mem_used: float,
    mem_total: float,
    mem_pct: float,
    loads: tuple[float, float, float],
    procs: list[tuple[int, float, float, str]],
    fullscreen: bool,
) -> None:
    cpu_fg, cpu_bg = severity(cpu)
    ram_fg, ram_bg = ram_severity(mem_pct)
    temp_s = f"{temp:.0f}C" if temp is not None else "N/A"
    load1, load5, load15 = loads

    header = (
        f"{hostname[:10]}  {temp_s}  UP {uptime_short()}  "
        f"{time.strftime('%H:%M')}  L {load1:.2f}/{load5:.2f}/{load15:.2f}"
    )

    process_limit = 10 if fullscreen else 5

    pid_lines = ["PID"]
    cpu_lines = ["CPU"]
    mem_lines = ["RSS"]
    cmd_lines = ["COMMAND"]

    for pid, pcpu, mem_mib, comm in procs[:process_limit]:
        pid_lines.append(str(pid))
        cpu_lines.append(f"{pcpu:.1f}%")
        mem_lines.append(fmt_process_mem(mem_mib))
        cmd_lines.append(comm[:18])

    while len(pid_lines) < process_limit + 1:
        pid_lines.append("")
        cpu_lines.append("")
        mem_lines.append("")
        cmd_lines.append("")

    ram_text = (
        f"RAM  {mem_used:.1f}/{mem_total:.0f}G"
        if fullscreen
        else f"RAM  {mem_pct:4.1f}%  {mem_used:.1f}/{mem_total:.0f}G"
    )

    send_cus(
        ser,
        {
            "cmd": "update",
            "layout": "grid",
            "set": [
                {"id": 0, "text": header, "fg": CYAN, "bg": BG},
                {
                    "id": 1,
                    "text": f"CPU  {cpu:4.1f}%",
                    "fg": cpu_fg,
                    "bg": cpu_bg,
                    "border": 1,
                    "border-color": cpu_fg,
                },
                {
                    "id": 2,
                    "text": ram_text,
                    "fg": ram_fg,
                    "bg": ram_bg,
                    "border": 1,
                    "border-color": ram_fg,
                },
                {"id": 3, "text": "\n".join(pid_lines)},
                {"id": 4, "text": "\n".join(cpu_lines)},
                {"id": 5, "text": "\n".join(mem_lines)},
                {"id": 6, "text": "\n".join(cmd_lines)},
            ],
        },
    )


def draw(
    ser: serial.Serial,
    *,
    first: bool,
    hostname: str,
    cpu: float,
    temp: Optional[float],
    mem_used: float,
    mem_total: float,
    mem_pct: float,
    loads: tuple[float, float, float],
    procs: list[tuple[int, float, float, str]],
    fullscreen: bool,
) -> None:
    """
    Firmware-v39 Grid renderer.

    The Grid is created once. Later refreshes update all widgets by ID in one
    serial frame, avoiding the multiple Absolute-mode writes used previously.
    """
    kwargs = dict(
        hostname=hostname,
        cpu=cpu,
        temp=temp,
        mem_used=mem_used,
        mem_total=mem_total,
        mem_pct=mem_pct,
        loads=loads,
        procs=procs,
        fullscreen=fullscreen,
    )

    if first:
        start_grid(ser, **kwargs)
    else:
        update_grid(ser, **kwargs)

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Mini htop para EezBotFun - Grid firmware v39"
    )
    p.add_argument("--port", help="ex.: /dev/ttyACM0")
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--baud", type=int, default=115200)
    p.add_argument(
        "--full",
        "--fullscreen",
        dest="fullscreen",
        action="store_true",
        help="usa o Grid em fullscreen (480x320) em vez da area normal 480x200",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    try:
        port = args.port or find_port()
    except RuntimeError as exc:
        print(exc)
        return 1

    hostname = socket.gethostname().split(".")[0]
    print(f"EezTop Grid v39 em {port}")
    if args.fullscreen:
        print("480x320; Grid fullscreen; 10 processos; memoria de processo em RSS real")
    else:
        print("480x200; Grid; area inferior das teclas preservada")
    print("Ctrl+C para sair")

    try:
        ser = serial.Serial(port, args.baud, timeout=1, write_timeout=2)
    except (serial.SerialException, OSError) as exc:
        print(f"Erro abrindo {port}: {exc}")
        return 1

    old_cpu = cpu_stat()
    old_procs = read_proc_snapshot()
    old_time = time.monotonic()
    first = True
    time.sleep(0.30)

    try:
        while True:
            now = time.monotonic()
            elapsed = max(0.001, now - old_time)

            new_cpu = cpu_stat()
            cpu = cpu_pct(old_cpu, new_cpu)
            old_cpu = new_cpu

            mem_used, mem_total, mem_pct, mem_total_bytes = memory()
            temp = cpu_temp()
            loads = os.getloadavg()

            new_procs = read_proc_snapshot()
            procs = process_rows(
                old_procs,
                new_procs,
                elapsed,
                mem_total_bytes,
                limit=10 if args.fullscreen else 5,
            )
            old_procs = new_procs
            old_time = now

            draw(
                ser,
                first=first,
                hostname=hostname,
                cpu=cpu,
                temp=temp,
                mem_used=mem_used,
                mem_total=mem_total,
                mem_pct=mem_pct,
                loads=loads,
                procs=procs,
                fullscreen=args.fullscreen,
            )
            first = False
            time.sleep(max(0.5, args.interval))

    except KeyboardInterrupt:
        print("\nSaindo...")
        try:
            send_cus(ser, {"cmd": "stop"})
        except Exception:
            pass
    finally:
        ser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


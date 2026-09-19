#!/usr/bin/env python3

"""
EezTop - Linux system monitor for the EezBotFun 8-Key MacroPad

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
        mem = rss_pages * PAGE_SIZE * 100.0 / mem_total_bytes if mem_total_bytes else 0.0
        rows.append((pid, cpu, mem, comm))

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


def temp_color(temp: Optional[float]) -> str:
    if temp is None:
        return DIM
    if temp >= 85:
        return RED
    if temp >= 70:
        return YELLOW
    return GREEN


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
) -> None:
    """Draw one complete frame without clearing between refreshes.

    Absolute mode has no widget IDs.  Clearing before every refresh causes the
    LCD to visibly pass through partially-redrawn states.  Instead every region
    below is an opaque rectangle at fixed coordinates.  New rectangles are
    simply stacked on top of the old ones; the firmware automatically discards
    the oldest absolute overlays when it reaches its overlay cap.

    Result: while a refresh is in progress, columns not yet updated still show
    the previous frame instead of disappearing.
    """
    cpu_fg, cpu_bg = severity(cpu)
    ram_fg, ram_bg = ram_severity(mem_pct)
    temp_s = f"{temp:.0f}C" if temp is not None else "N/A"
    load1, load5, load15 = loads

    # One header panel instead of four independent panels.  Proportional text is
    # fine here because it is a single status sentence, not a table.
    header = (
        f"{hostname[:10]}  {temp_s}  UP {uptime_short()}  "
        f"{time.strftime('%H:%M')}  L {load1:.2f}/{load5:.2f}/{load15:.2f}"
    )
    send_cus(
        ser,
        panel(
            0, 0, 480, 25, header,
            fg=CYAN,
            bg=BG,
            align="CENTER",
            cmd="start" if first else "update",
            clear_canvas=first,
        ),
    )
    time.sleep(0.010)

    # Two large opaque status cards.  Previous cards remain visible until the new
    # one reaches the device, so there is no blank flash during normal refreshes.
    send_cus(
        ser,
        panel(
            0, 27, 238, 31,
            f"CPU  {cpu:4.1f}%",
            fg=cpu_fg, bg=cpu_bg, align="CENTER",
            border=1, border_color=cpu_fg,
        ),
    )
    time.sleep(0.010)

    send_cus(
        ser,
        panel(
            242, 27, 238, 31,
            f"RAM  {mem_pct:4.1f}%  {mem_used:.1f}/{mem_total:.0f}G",
            fg=ram_fg, bg=ram_bg, align="CENTER",
            border=1, border_color=ram_fg,
        ),
    )
    time.sleep(0.010)

    # Four fixed columns.  The rectangles touch each other and are fully opaque,
    # so every refresh paints the whole table area without tabs/spaces and without
    # exposing an empty canvas.
    pid_lines = ["PID"]
    cpu_lines = ["CPU"]
    mem_lines = ["MEM"]
    cmd_lines = ["COMMAND"]

    for pid, pcpu, pmem, comm in procs[:5]:
        pid_lines.append(str(pid))
        cpu_lines.append(f"{pcpu:.1f}%")
        mem_lines.append(f"{pmem:.1f}%")
        cmd_lines.append(comm[:18])

    while len(pid_lines) < 6:
        pid_lines.append("")
        cpu_lines.append("")
        mem_lines.append("")
        cmd_lines.append("")

    table_y = 62
    table_h = 138
    columns = (
        panel(0,   table_y, 96,  table_h, "\n".join(pid_lines), fg=DIM,         bg=BG, align="RIGHT"),
        panel(96,  table_y, 84,  table_h, "\n".join(cpu_lines), fg=GREEN,       bg=BG, align="RIGHT"),
        panel(180, table_y, 84,  table_h, "\n".join(mem_lines), fg=CYAN,        bg=BG, align="RIGHT"),
        panel(264, table_y, 216, table_h, "\n".join(cmd_lines), fg=WHITE,       bg=BG, align="LEFT"),
    )

    for i, obj in enumerate(columns):
        send_cus(ser, obj)
        if i != len(columns) - 1:
            time.sleep(0.010)

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Mini htop para EezBotFun - absolute sem clear por frame"
    )
    p.add_argument("--port", help="ex.: /dev/ttyACM0")
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--baud", type=int, default=115200)
    return p.parse_args()


def main() -> int:
    args = parse_args()

    try:
        port = args.port or find_port()
    except RuntimeError as exc:
        print(exc)
        return 1

    hostname = socket.gethostname().split(".")[0]
    print(f"EezTop v7 stable overlays em {port}")
    print("480x200; sem tabs; sem clear_canvas durante refresh")
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
                limit=5,
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


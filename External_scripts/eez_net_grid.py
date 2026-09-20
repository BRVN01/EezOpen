#!/usr/bin/env python3

"""
EezNet - Linux network monitor for the EezBotFun 8-Key MacroPad

Part of the EezOpen project.

Author: Bruno Dias da Silva
Project: EezOpen
Description:
    Displays real-time Linux network information on the MacroPad LCD
    using the EezBotFun Customised Display USB CDC Grid layout
    validated with firmware v39.

Copyright (c) 2026 Bruno Dias da Silva

Licensed under the MIT License.
See LICENSE for details.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import ipaddress
import socket
import struct
import subprocess
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

# High-contrast palette that worked well on the MacroPad LCD.
BG = "#080C12"
WHITE = "#FFFFFF"
DIM = "#A9B4C0"
CYAN = "#00D8FF"
GREEN = "#00FF66"
YELLOW = "#FFD000"
RED = "#FF4040"

RX_BG = "#082A1A"
TX_BG = "#08243A"
WARN_BG = "#3A2C00"
CRIT_BG = "#3A0D0D"
BORDER = "#294154"


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


def default_interface() -> Optional[str]:
    try:
        with open("/proc/net/route", encoding="utf-8") as f:
            next(f, None)
            for line in f:
                fields = line.split()
                if len(fields) < 4:
                    continue
                iface, dest, _, flags = fields[:4]
                if dest == "00000000" and (int(flags, 16) & 0x2):
                    return iface
    except (OSError, ValueError):
        pass
    return None


def get_ipv4(iface: str) -> Optional[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        req = struct.pack("256s", iface[:15].encode())
        res = fcntl.ioctl(sock.fileno(), 0x8915, req)  # SIOCGIFADDR
        return socket.inet_ntoa(res[20:24])
    except OSError:
        return None
    finally:
        sock.close()


def local_ipv4_map() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}

    # iproute2 also sees secondary IPv4 addresses, unlike SIOCGIFADDR.
    try:
        proc = subprocess.run(
            ["ip", "-o", "-4", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=True,
        )
        for line in proc.stdout.splitlines():
            fields = line.split()
            if len(fields) < 4 or fields[2] != "inet":
                continue
            iface = fields[1].split("@", 1)[0]
            addr = fields[3].split("/", 1)[0]
            result.setdefault(iface, []).append(addr)
    except (FileNotFoundError, subprocess.SubprocessError):
        pass

    # Fallback, and also include interfaces without IPv4.
    for path in sorted(Path("/sys/class/net").iterdir(), key=lambda p: p.name):
        result.setdefault(path.name, [])
        if not result[path.name]:
            addr = get_ipv4(path.name)
            if addr:
                result[path.name].append(addr)

    return result


def local_interfaces() -> list[tuple[str, Optional[str]]]:
    result: list[tuple[str, Optional[str]]] = []
    for iface, addrs in sorted(local_ipv4_map().items()):
        result.append((iface, addrs[0] if addrs else None))
    return result


def interface_from_ip(ip: str) -> Optional[str]:
    try:
        wanted = str(ipaddress.IPv4Address(ip))
    except ipaddress.AddressValueError:
        return None

    for iface, addrs in local_ipv4_map().items():
        if wanted in addrs:
            return iface
    return None


def resolve_interface(target: Optional[str], explicit_iface: Optional[str], explicit_ip: Optional[str]) -> str:
    if explicit_iface:
        if not Path(f"/sys/class/net/{explicit_iface}").exists():
            raise RuntimeError(f"Interface {explicit_iface!r} nao existe")
        return explicit_iface

    if explicit_ip:
        iface = interface_from_ip(explicit_ip)
        if not iface:
            raise RuntimeError(f"Nenhuma interface local possui o IPv4 {explicit_ip}")
        return iface

    if target:
        # Primeiro tenta como nome de interface.
        if Path(f"/sys/class/net/{target}").exists():
            return target

        # Depois tenta como IPv4 local.
        try:
            ipaddress.IPv4Address(target)
        except ipaddress.AddressValueError:
            raise RuntimeError(
                f"{target!r} nao e uma interface existente nem um IPv4 valido"
            )

        iface = interface_from_ip(target)
        if not iface:
            raise RuntimeError(f"Nenhuma interface local possui o IPv4 {target}")
        return iface

    iface = default_interface()
    if not iface:
        raise RuntimeError(
            "Nao consegui descobrir a interface default. "
            "Informe a interface ou IPv4 local."
        )
    return iface


def read_operstate(iface: str) -> str:
    try:
        return Path(f"/sys/class/net/{iface}/operstate").read_text().strip().lower()
    except OSError:
        return "unknown"


def read_link_speed(iface: str) -> Optional[int]:
    try:
        speed = int(Path(f"/sys/class/net/{iface}/speed").read_text().strip())
        return speed if speed > 0 else None
    except (OSError, ValueError):
        return None


def read_wifi_dbm(iface: str) -> Optional[int]:
    try:
        with open("/proc/net/wireless", encoding="utf-8") as f:
            for line in f:
                if ":" not in line:
                    continue
                name, rest = line.split(":", 1)
                if name.strip() != iface:
                    continue
                fields = rest.split()
                if len(fields) < 3:
                    return None
                # /proc/net/wireless usually exposes level in dBm here.
                level = float(fields[2].rstrip("."))
                if -150.0 <= level <= 0.0:
                    return int(round(level))
    except (OSError, ValueError):
        pass
    return None


def net_snapshot(iface: str) -> tuple[int, int, int, int, int, int, int, int]:
    """Return RX bytes/packets/errors/drop and TX bytes/packets/errors/drop."""
    with open("/proc/net/dev", encoding="utf-8") as f:
        for line in f:
            if ":" not in line:
                continue
            name, raw = line.split(":", 1)
            if name.strip() != iface:
                continue

            fields = raw.split()
            if len(fields) < 16:
                break
            return (
                int(fields[0]),   # rx bytes
                int(fields[1]),   # rx packets
                int(fields[2]),   # rx errors
                int(fields[3]),   # rx dropped
                int(fields[8]),   # tx bytes
                int(fields[9]),   # tx packets
                int(fields[10]),  # tx errors
                int(fields[11]),  # tx dropped
            )

    raise RuntimeError(f"Interface {iface!r} nao encontrada em /proc/net/dev")


def tcp_counts() -> tuple[int, int]:
    # Linux TCP states: 01 ESTABLISHED, 0A LISTEN.
    established = 0
    listen = 0

    for filename in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(filename, encoding="utf-8") as f:
                next(f, None)
                for line in f:
                    fields = line.split()
                    if len(fields) < 4:
                        continue
                    state = fields[3]
                    if state == "01":
                        established += 1
                    elif state == "0A":
                        listen += 1
        except OSError:
            continue

    return established, listen


def fmt_rate(value: float) -> str:
    value = max(0.0, value)
    units = ("B/s", "K/s", "M/s", "G/s")
    for unit in units:
        if value < 1024.0 or unit == "G/s":
            if value >= 100:
                return f"{value:.0f}{unit}"
            if value >= 10:
                return f"{value:.1f}{unit}"
            return f"{value:.2f}{unit}"
        value /= 1024.0
    return f"{value:.1f}G/s"


def fmt_bytes(value: int) -> str:
    amount = float(max(0, value))
    units = ("B", "K", "M", "G", "T")
    for unit in units:
        if amount < 1024.0 or unit == "T":
            if amount >= 100:
                return f"{amount:.0f}{unit}"
            if amount >= 10:
                return f"{amount:.1f}{unit}"
            return f"{amount:.2f}{unit}"
        amount /= 1024.0
    return f"{amount:.1f}T"


def fmt_count(value: int) -> str:
    amount = float(max(0, value))
    units = ("", "K", "M", "G")
    for unit in units:
        if amount < 1000.0 or unit == "G":
            if unit == "":
                return str(int(amount))
            if amount >= 100:
                return f"{amount:.0f}{unit}"
            if amount >= 10:
                return f"{amount:.1f}{unit}"
            return f"{amount:.2f}{unit}"
        amount /= 1000.0
    return f"{amount:.1f}G"


def fmt_link(speed_mbps: Optional[int], wifi_dbm: Optional[int]) -> str:
    if wifi_dbm is not None:
        return f"{wifi_dbm}dBm"
    if speed_mbps is None:
        return ""
    if speed_mbps >= 1000 and speed_mbps % 1000 == 0:
        return f"{speed_mbps // 1000}G"
    return f"{speed_mbps}M"


def rate_color(rate_bps: float, speed_mbps: Optional[int], default_fg: str, default_bg: str) -> tuple[str, str]:
    if not speed_mbps or speed_mbps <= 0:
        return default_fg, default_bg

    capacity_bps = speed_mbps * 1_000_000 / 8.0
    pct = rate_bps * 100.0 / capacity_bps if capacity_bps else 0.0
    if pct >= 90:
        return RED, CRIT_BG
    if pct >= 70:
        return YELLOW, WARN_BG
    return default_fg, default_bg


def build_grid_widgets(
    *,
    iface: str,
    ip: Optional[str],
    operstate: str,
    speed_mbps: Optional[int],
    wifi_dbm: Optional[int],
    rx_rate: float,
    tx_rate: float,
    rx_pps: float,
    tx_pps: float,
    snap: tuple[int, int, int, int, int, int, int, int],
    established: int,
    listen: int,
) -> list[dict]:
    """
    Build the initial 480x200 Grid layout.

    Six equal columns make it easy to use 3+3 spans for RX/TX and 2+2+2 spans
    for the metrics table while keeping the total widget count small.
    """
    rx_bytes, rx_packets, rx_errs, rx_drop, tx_bytes, tx_packets, tx_errs, tx_drop = snap

    rx_fg, rx_bg = rate_color(rx_rate, speed_mbps, GREEN, RX_BG)
    tx_fg, tx_bg = rate_color(tx_rate, speed_mbps, CYAN, TX_BG)

    state = "UP" if operstate == "up" else operstate.upper()[:4]
    link = fmt_link(speed_mbps, wifi_dbm)
    ip_s = ip or "no-ip"

    header_parts = [iface[:10], ip_s]
    if link:
        header_parts.append(link)
    header_parts.extend([state, time.strftime("%H:%M")])
    header = "  ".join(header_parts)

    metric_lines = ["METRIC", "TOTAL", "PACKETS", "DROP", "ERROR"]
    rx_lines = [
        "RX",
        fmt_bytes(rx_bytes),
        fmt_count(rx_packets),
        fmt_count(rx_drop),
        fmt_count(rx_errs),
    ]
    tx_lines = [
        "TX",
        fmt_bytes(tx_bytes),
        fmt_count(tx_packets),
        fmt_count(tx_drop),
        fmt_count(tx_errs),
    ]

    footer = f"TCP  EST {established}   LISTEN {listen}"

    return [
        {
            "id": 0,
            "type": "text",
            "row": 0,
            "col": 0,
            "col_span": 6,
            "text": header,
            "fg": CYAN if operstate == "up" else RED,
            "bg": BG,
            "align": "CENTER",
            "long_mode": "CLIP",
        },
        {
            "id": 1,
            "type": "text",
            "row": 1,
            "col": 0,
            "col_span": 3,
            "text": f"RX  {fmt_rate(rx_rate)}  {fmt_count(int(rx_pps))}pps",
            "fg": rx_fg,
            "bg": rx_bg,
            "align": "CENTER",
            "long_mode": "CLIP",
            "border": 1,
            "border-color": rx_fg,
            "border-radius": 4,
        },
        {
            "id": 2,
            "type": "text",
            "row": 1,
            "col": 3,
            "col_span": 3,
            "text": f"TX  {fmt_rate(tx_rate)}  {fmt_count(int(tx_pps))}pps",
            "fg": tx_fg,
            "bg": tx_bg,
            "align": "CENTER",
            "long_mode": "CLIP",
            "border": 1,
            "border-color": tx_fg,
            "border-radius": 4,
        },
        {
            "id": 3,
            "type": "text",
            "row": 2,
            "col": 0,
            "col_span": 2,
            "text": "\n".join(metric_lines),
            "fg": DIM,
            "bg": BG,
            "align": "RIGHT",
            "long_mode": "CLIP",
        },
        {
            "id": 4,
            "type": "text",
            "row": 2,
            "col": 2,
            "col_span": 2,
            "text": "\n".join(rx_lines),
            "fg": GREEN,
            "bg": BG,
            "align": "RIGHT",
            "long_mode": "CLIP",
        },
        {
            "id": 5,
            "type": "text",
            "row": 2,
            "col": 4,
            "col_span": 2,
            "text": "\n".join(tx_lines),
            "fg": CYAN,
            "bg": BG,
            "align": "RIGHT",
            "long_mode": "CLIP",
        },
        {
            "id": 6,
            "type": "text",
            "row": 3,
            "col": 0,
            "col_span": 6,
            "text": footer,
            "fg": WHITE,
            "bg": BG,
            "align": "CENTER",
            "long_mode": "CLIP",
        },
    ]


def start_grid(
    ser: serial.Serial,
    *,
    iface: str,
    ip: Optional[str],
    operstate: str,
    speed_mbps: Optional[int],
    wifi_dbm: Optional[int],
    rx_rate: float,
    tx_rate: float,
    rx_pps: float,
    tx_pps: float,
    snap: tuple[int, int, int, int, int, int, int, int],
    established: int,
    listen: int,
) -> None:
    send_cus(
        ser,
        {
            "cmd": "start",
            "layout": "grid",
            "fullscreen": False,
            "grid": {
                "cols": ["*", "*", "*", "*", "*", "*"],
                "rows": ["25", "35", "*", "26"],
                "gap": 3,
                "pad": 0,
            },
            "widgets": build_grid_widgets(
                iface=iface,
                ip=ip,
                operstate=operstate,
                speed_mbps=speed_mbps,
                wifi_dbm=wifi_dbm,
                rx_rate=rx_rate,
                tx_rate=tx_rate,
                rx_pps=rx_pps,
                tx_pps=tx_pps,
                snap=snap,
                established=established,
                listen=listen,
            ),
        },
    )


def update_grid(
    ser: serial.Serial,
    *,
    iface: str,
    ip: Optional[str],
    operstate: str,
    speed_mbps: Optional[int],
    wifi_dbm: Optional[int],
    rx_rate: float,
    tx_rate: float,
    rx_pps: float,
    tx_pps: float,
    snap: tuple[int, int, int, int, int, int, int, int],
    established: int,
    listen: int,
) -> None:
    rx_bytes, rx_packets, rx_errs, rx_drop, tx_bytes, tx_packets, tx_errs, tx_drop = snap

    rx_fg, rx_bg = rate_color(rx_rate, speed_mbps, GREEN, RX_BG)
    tx_fg, tx_bg = rate_color(tx_rate, speed_mbps, CYAN, TX_BG)

    state = "UP" if operstate == "up" else operstate.upper()[:4]
    link = fmt_link(speed_mbps, wifi_dbm)
    ip_s = ip or "no-ip"

    header_parts = [iface[:10], ip_s]
    if link:
        header_parts.append(link)
    header_parts.extend([state, time.strftime("%H:%M")])
    header = "  ".join(header_parts)

    metric_lines = ["METRIC", "TOTAL", "PACKETS", "DROP", "ERROR"]
    rx_lines = [
        "RX",
        fmt_bytes(rx_bytes),
        fmt_count(rx_packets),
        fmt_count(rx_drop),
        fmt_count(rx_errs),
    ]
    tx_lines = [
        "TX",
        fmt_bytes(tx_bytes),
        fmt_count(tx_packets),
        fmt_count(tx_drop),
        fmt_count(tx_errs),
    ]

    footer = f"TCP  EST {established}   LISTEN {listen}"

    send_cus(
        ser,
        {
            "cmd": "update",
            "layout": "grid",
            "set": [
                {
                    "id": 0,
                    "text": header,
                    "fg": CYAN if operstate == "up" else RED,
                    "bg": BG,
                },
                {
                    "id": 1,
                    "text": f"RX  {fmt_rate(rx_rate)}  {fmt_count(int(rx_pps))}pps",
                    "fg": rx_fg,
                    "bg": rx_bg,
                    "border": 1,
                    "border-color": rx_fg,
                },
                {
                    "id": 2,
                    "text": f"TX  {fmt_rate(tx_rate)}  {fmt_count(int(tx_pps))}pps",
                    "fg": tx_fg,
                    "bg": tx_bg,
                    "border": 1,
                    "border-color": tx_fg,
                },
                {"id": 3, "text": "\n".join(metric_lines)},
                {"id": 4, "text": "\n".join(rx_lines)},
                {"id": 5, "text": "\n".join(tx_lines)},
                {"id": 6, "text": footer},
            ],
        },
    )


def draw(
    ser: serial.Serial,
    *,
    first: bool,
    iface: str,
    ip: Optional[str],
    operstate: str,
    speed_mbps: Optional[int],
    wifi_dbm: Optional[int],
    rx_rate: float,
    tx_rate: float,
    rx_pps: float,
    tx_pps: float,
    snap: tuple[int, int, int, int, int, int, int, int],
    established: int,
    listen: int,
) -> None:
    """
    Firmware-v39 Grid renderer.

    The Grid is created once. Later refreshes update all widgets by ID in one
    serial frame, avoiding the multiple Absolute-mode writes used previously.
    """
    kwargs = dict(
        iface=iface,
        ip=ip,
        operstate=operstate,
        speed_mbps=speed_mbps,
        wifi_dbm=wifi_dbm,
        rx_rate=rx_rate,
        tx_rate=tx_rate,
        rx_pps=rx_pps,
        tx_pps=tx_pps,
        snap=snap,
        established=established,
        listen=listen,
    )

    if first:
        start_grid(ser, **kwargs)
    else:
        update_grid(ser, **kwargs)

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="EezNet - monitor de rede para EezBotFun",
        epilog=(
            "Exemplos: eez_net_v2.py enp5s0 | "
            "eez_net_v2.py 192.168.1.20 | "
            "eez_net_v2.py -i enp5s0 | "
            "eez_net_v2.py --ip 192.168.1.20"
        ),
    )
    p.add_argument(
        "target",
        nargs="?",
        help="interface ou IPv4 local; se omitido usa a rota default",
    )
    p.add_argument("--port", help="ex.: /dev/ttyACM0")

    select = p.add_mutually_exclusive_group()
    select.add_argument("--interface", "-i", help="interface de rede, ex.: enp5s0")
    select.add_argument("--ip", help="IPv4 LOCAL; o EezNet descobre a interface")

    p.add_argument(
        "--list-interfaces",
        action="store_true",
        help="lista interfaces/IPv4 locais e sai",
    )
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--baud", type=int, default=115200)
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.list_interfaces:
        print("Interfaces locais:")
        ipv4_map = local_ipv4_map()
        for name, addrs in sorted(ipv4_map.items()):
            print(f"  {name:16} {', '.join(addrs) if addrs else '-'}")
        return 0

    try:
        port = args.port or find_port()
    except RuntimeError as exc:
        print(exc)
        return 1

    if args.target and (args.interface or args.ip):
        print("Use somente um seletor: TARGET, --interface ou --ip")
        return 2

    try:
        iface = resolve_interface(args.target, args.interface, args.ip)
    except RuntimeError as exc:
        print(exc)
        print("Interfaces locais:")
        for name, addr in local_interfaces():
            print(f"  {name:16} {addr or '-'}")
        return 1

    try:
        old_snap = net_snapshot(iface)
    except RuntimeError as exc:
        print(exc)
        return 1

    print(f"EezNet em {port}")
    print(f"Interface: {iface}")
    print(f"IPv4: {get_ipv4(iface) or '-'}")
    print("480x200; Grid firmware v39; updates por widget ID; Ctrl+C para sair")

    try:
        ser = serial.Serial(port, args.baud, timeout=1, write_timeout=2)
    except (serial.SerialException, OSError) as exc:
        print(f"Erro abrindo {port}: {exc}")
        return 1

    old_time = time.monotonic()
    first = True
    time.sleep(0.30)

    try:
        while True:
            now = time.monotonic()
            elapsed = max(0.001, now - old_time)

            try:
                snap = net_snapshot(iface)
            except RuntimeError as exc:
                print(exc)
                break

            rx_rate = max(0, snap[0] - old_snap[0]) / elapsed
            tx_rate = max(0, snap[4] - old_snap[4]) / elapsed
            rx_pps = max(0, snap[1] - old_snap[1]) / elapsed
            tx_pps = max(0, snap[5] - old_snap[5]) / elapsed

            ip = get_ipv4(iface)
            operstate = read_operstate(iface)
            speed_mbps = read_link_speed(iface)
            wifi_dbm = read_wifi_dbm(iface)
            established, listen = tcp_counts()

            draw(
                ser,
                first=first,
                iface=iface,
                ip=ip,
                operstate=operstate,
                speed_mbps=speed_mbps,
                wifi_dbm=wifi_dbm,
                rx_rate=rx_rate,
                tx_rate=tx_rate,
                rx_pps=rx_pps,
                tx_pps=tx_pps,
                snap=snap,
                established=established,
                listen=listen,
            )

            first = False
            old_snap = snap
            old_time = now
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
#!/usr/bin/env python3
"""EezOpen daemon v1.4.3.

Owns the EezBotFun serial port, keeps a local cache, executes the small
Non-HID action set used by EezOpen and exposes a local JSON/Unix-socket API
for the GTK configurator.
"""
from __future__ import annotations

from contextlib import contextmanager
import glob
import hashlib
import hmac
import json
import logging
import os
import select
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path
from typing import Any, Optional

APP_NAME = "EezOpen"
EEZOPEN_VERSION = "1.4.3"
BAUD = termios.B460800
POLL_INTERVAL = 2.0
MAX_SERIAL_BUFFER = 64 * 1024
MAX_IPC_REQUEST = 1024 * 1024
MAX_PROFILE_UI = 99
SUPPORTED_ACTIONS = {"1", "2", "3", "f", "p"}
MAX_HID_ACTIONS = 512
BACKGROUND_WIDTH = 622
BACKGROUND_HEIGHT = 345
BACKGROUND_BIN_SIZE = 4 + (16 * 4) + ((BACKGROUND_WIDTH * BACKGROUND_HEIGHT + 1) // 2)
BACKGROUND_FILENAMES = {"dark": "bg_dark.bin", "light": "bg_light.bin"}
HID_MEDIA_SCRIPTS = {
    # Confirmed by native Configurator capture with HidMode=True.  Media keys
    # are firmware script tokens, not direct binary Consumer KeyConfig writes.
    "VOLUP": "MK_VOLUP",
    "VOLDOWN": "MK_VOLDOWN",
    "MUTE": "MK_MUTE",
    "PREV": "MK_PREV",
    "NEXT": "MK_NEXT",
    "PP": "MK_PP",
    "STOP": "MK_STOP",
}
HID_CONSUMER_TOKENS = set(HID_MEDIA_SCRIPTS)
HID_KEY_TOKENS = {
    "CONTROL", "SHIFT", "ALT", "WINDOWS", "COMMAND", "OPTION",
    "ESC", "ENTER", "UP", "DOWN", "LEFT", "RIGHT", "SPACE",
    "BACKSPACE", "TAB", "CAPLOCKS", "INSERT", "DELETE", "HOME",
    "END", "PAGEUP", "PAGEDOWN", "PRINTSCREEN", "SCROLLLOCK",
    "PAUSE", "BREAK", "MENU", "POWER",
    *(f"F{i}" for i in range(1, 25)),
    *HID_CONSUMER_TOKENS,
    "NUMLOCK", "KP_SLASH", "KP_ASTERISK", "KP_MINUS", "KP_PLUS",
    "KP_ENTER", "KP_DOT", "KP_EQUAL",
    *(f"KP_{i}" for i in range(10)),
}
AUTH_KEY = b"ezfnb42u9a5bvinb42u9ezfnb-209qaw"

LOG = logging.getLogger("eezopen")
RUNNING = True
STATE_LOCK = threading.RLock()
SERIAL_WRITE_LOCK = threading.Lock()
SETTINGS_LOCK = threading.RLock()
OPERATION_LOCK = threading.RLock()
# Serial and Mass Storage are two interfaces of the same physical firmware.
# This lock serializes *all* host->device I/O so CDC telemetry/commands can never
# overlap an explicit MSC filesystem operation.
DEVICE_IO_LOCK = threading.RLock()
PROFILE_COND = threading.Condition(STATE_LOCK)
SCREEN_SCRIPT_LOCK = threading.RLock()
SCREEN_SCRIPT_PROCESS: Optional[subprocess.Popen] = None

# PC Monitor integration.  The tested MacroPad firmware renders a raw Unix
# timestamp as UTC+8; on this UTC-3 host the proven correction is -11 hours.
# Keep this behavior identical to the standalone v4 test that was validated
# on the physical device.
PC_MONITOR_ENABLED = True
PC_MONITOR_INTERVAL = 1.0
# Experimental guard for the firmware's profile transition animation.
# PCS telemetry is paused briefly so it cannot redraw the LCD mid-transition.
PC_MONITOR_PROFILE_TRANSITION_DELAY = 0.80
# The runtime CDC endpoint appears before Linux has necessarily finished the USB
# Mass Storage/SCSI bring-up. Never probe or transmit to the firmware until the
# composite device has remained stable and the MSC queue is idle.
USB_RUNTIME_SETTLE_TIMEOUT = 15.0
USB_MSC_SETTLE_SECONDS = 3.0
USB_MSC_IDLE_STABLE_SECONDS = 1.0
USB_SAFE_MODE_SETTLE_SECONDS = 5.0
USB_NO_MSC_SETTLE_SECONDS = 2.0
PC_MONITOR_STARTUP_DELAY = 2.0
MSC_POST_UNMOUNT_DELAY = 0.50
# Give the firmware a real direction-change gap before asking the MSC function
# to mount. DEVICE_IO_LOCK prevents any new daemon CDC TX while this delay runs.
MSC_PRE_ACCESS_CDC_QUIET = 0.75
MSC_PRE_ACCESS_IDLE_TIMEOUT = 1.50
MSC_PRE_ACCESS_IDLE_STABLE = 0.25
# Foreground serial commands should fail quickly rather than queueing for seconds
# behind a wedged SCSI request. The Configurator can retry after storage is idle.
MSC_FOREGROUND_IDLE_TIMEOUT = 0.35
MSC_FOREGROUND_IDLE_STABLE = 0.12
# Host-side Mass Storage blocking is intentionally disabled. Safe Mode (PID 4005)
# is handled separately because the firmware itself does not expose MSC there.
HOST_MSC_STABILITY_GUARD = False
PC_MONITOR_CMD = 1230
PC_MONITOR_TIME_SHIFT_SECONDS = -(11 * 3600)
PC_CPU_PREV: Optional[tuple[int, int]] = None
PC_CPU_TEMP_MAX = 0.0
PC_GPU_TEMP_MAX = 0.0
PC_TICK = 0
PC_CPU_ENERGY_PREV: Optional[tuple[float, float]] = None
STATE: dict[str, Any] = {
    "connected": False,
    "fd": None,
    "dev": None,
    "type": None,
    "id": None,
    "config_dir": None,
    "icon_dir": None,
    "script_dir": None,
    # Persistent cache above lives in ~/.local/share/EezOpen.  The view dirs
    # below are ephemeral mirrors of the currently connected MacroPad and live
    # under XDG_RUNTIME_DIR.  Automatic device discovery must never overwrite
    # the persistent cache.
    "view_config_dir": None,
    "view_icon_dir": None,
    "view_script_dir": None,
    "source_dir": None,
    "storage_root": None,
    "profile_count": 1,
    "profile_count_source": "fallback",
    "profile_generation": 0,
    "active_profile": None,
    "usb_sysfs": None,
    "usb_vid": None,
    "usb_pid": None,
    "usb_serial": None,
    "storage_ready": False,
    "storage_error": None,
    "safe_mode": False,
    "storage_guarded": HOST_MSC_STABILITY_GUARD,
    "authenticated": False,
    "connection_generation": 0,
    # Connection lifecycle. "connected" becomes True only after USB settle,
    # serial probe and authentication have completed. Background telemetry also
    # waits for pc_monitor_ready_at.
    "operational": False,
    "startup_phase": "waiting",
    "startup_detail": None,
    "pc_monitor_ready_at": 0.0,
    "storage_busy": False,
    # If a mount request times out, udisksctl exiting does not prove that the
    # remote udisksd/kernel mount operation was cancelled. Quarantine all CDC TX
    # until physical reconnect instead of racing an unresolved SCSI request.
    "storage_quarantined": False,
    "storage_quarantine_reason": None,
    "last_cdc_tx_at": 0.0,
    # Firmware update reconnect guard. While active, the daemon must not probe,
    # authenticate, mount or otherwise touch a transient recovery/update USB
    # personality. It resumes only after the normal pre-update VID:PID has
    # disappeared and then reappeared.
    "firmware_waiting_for_normal": False,
    "firmware_expected_vid": None,
    "firmware_expected_pid": None,
    "firmware_seen_transition": False,
    "firmware_wait_started": None,
    "pc_monitor_enabled": PC_MONITOR_ENABLED,
    "pc_monitor_interval": PC_MONITOR_INTERVAL,
    "pc_monitor_last_sent": None,
    "pc_monitor_last_error": None,
    "pc_monitor_pause_until": 0.0,
    # A managed long-running Screen Script may draw on the LCD while the daemon
    # remains connected. Only PCS telemetry is paused; normal EBF commands and
    # Non-HID key handling stay available. Exactly one Screen Script may run.
    "screen_script_active": False,
    "screen_script_name": None,
    "screen_script_path": None,
    "screen_script_args": "",
    "screen_script_pid": None,
    "screen_script_tty": None,
    "screen_script_last_error": None,
}


def stop_handler(_signum, _frame_obj):
    global RUNNING
    RUNNING = False


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    path = Path(base) / "eezopen" if base else Path("/tmp") / f"eezopen-{os.getuid()}"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def socket_path() -> Path:
    return runtime_dir() / "daemon.sock"


def frame(payload: str) -> bytes:
    data = payload.encode("ascii")
    if len(data) > 255:
        raise ValueError("Payload is too large for the EBF frame")
    return b"ebf" + bytes([len(data)]) + data


def configure_serial(fd: int) -> None:
    attr = termios.tcgetattr(fd)
    attr[0] = 0
    attr[1] = 0
    clear_flags = termios.PARENB | termios.CSTOPB | termios.CSIZE | termios.HUPCL
    if hasattr(termios, "CRTSCTS"):
        clear_flags |= termios.CRTSCTS
    attr[2] &= ~clear_flags
    attr[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
    attr[3] = 0
    attr[4] = BAUD
    attr[5] = BAUD
    termios.tcsetattr(fd, termios.TCSANOW, attr)
    termios.tcflush(fd, termios.TCIFLUSH)


def configure_serial_baud(fd: int, baud: int) -> None:
    """Raw 8N1/no-flow-control serial configuration without HUPCL.

    Firmware USB-copy triggering is proven on this device at 115200 baud,
    while normal EezOpen runtime uses 460800 baud.
    """
    attr = termios.tcgetattr(fd)
    attr[0] = 0
    attr[1] = 0
    clear_flags = termios.PARENB | termios.CSTOPB | termios.CSIZE | termios.HUPCL
    if hasattr(termios, "CRTSCTS"):
        clear_flags |= termios.CRTSCTS
    attr[2] &= ~clear_flags
    attr[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
    attr[3] = 0
    attr[4] = baud
    attr[5] = baud
    termios.tcsetattr(fd, termios.TCSANOW, attr)
    termios.tcflush(fd, termios.TCIFLUSH)


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def usb_device_info_from_tty(dev: str) -> dict[str, Optional[str]]:
    """Return the physical USB-device ancestor for a ttyACM node.

    Matching the mass-storage interface by the shared physical USB parent is
    more robust than pinning the product ID: firmware updates/recovery can
    re-enumerate the same MacroPad with a different PID.
    """
    node = Path("/sys/class/tty") / Path(dev).name / "device"
    try:
        node = node.resolve()
    except OSError:
        return {"sysfs": None, "vid": None, "pid": None, "serial": None, "product": None}
    for parent in (node, *node.parents):
        vid = _safe_read(parent / "idVendor").lower()
        pid = _safe_read(parent / "idProduct").lower()
        if vid and pid:
            return {
                "sysfs": str(parent),
                "vid": vid,
                "pid": pid,
                "serial": _safe_read(parent / "serial") or None,
                "product": _safe_read(parent / "product") or None,
            }
    return {"sysfs": None, "vid": None, "pid": None, "serial": None, "product": None}


def usb_serial_from_tty(dev: str) -> Optional[str]:
    return usb_device_info_from_tty(dev).get("serial")


def is_eezbotfun_runtime_tty(dev: str) -> bool:
    """Return True only for normal EezBotFun runtime USB personalities.

    PID 0012 is the known updater/recovery personality and must not be probed
    by the normal runtime detector. 4005 (Safe Mode) and 4007 (MSC enabled)
    are the observed runtime PIDs; the product descriptor is kept as a
    forward-compatible fallback for future normal runtime PIDs.
    """
    info = usb_device_info_from_tty(dev)
    vid = str(info.get("vid") or "").lower()
    pid = str(info.get("pid") or "").lower()
    product = str(info.get("product") or "").strip()
    if vid != "303a" or pid == "0012":
        return False
    return pid in {"4005", "4007"} or product == "EezBotFun MicroPad"


def is_safe_mode_usb(vid: Any, pid: Any) -> bool:
    """Known Safe Mode USB identity: CDC + HID, with Mass Storage disabled."""
    return str(vid or "").lower() == "303a" and str(pid or "").lower() == "4005"



def _set_startup_phase(phase: str, detail: Optional[str] = None) -> None:
    with STATE_LOCK:
        STATE["startup_phase"] = phase
        STATE["startup_detail"] = detail
        if phase != "operational":
            STATE["operational"] = False


def _usb_identity_token(info: dict[str, Optional[str]]) -> tuple[str, str, str, str]:
    sysfs = str(info.get("sysfs") or "")
    devnum = _safe_read(Path(sysfs) / "devnum") if sysfs else ""
    return (
        sysfs,
        str(info.get("vid") or "").lower(),
        str(info.get("pid") or "").lower(),
        devnum,
    )


def _usb_identity_unchanged(dev: str, token: tuple[str, str, str, str]) -> bool:
    if not os.path.exists(dev):
        return False
    info = usb_device_info_from_tty(dev)
    return _usb_identity_token(info) == token


def _usb_has_mass_storage_interface(usb_sysfs: str) -> bool:
    """Inspect USB interface class in sysfs without touching the block device."""
    if not usb_sysfs:
        return False
    base = Path(usb_sysfs)
    try:
        for iface in base.glob(f"{base.name}:*"):
            if _safe_read(iface / "bInterfaceClass").lower() == "08":
                return True
    except OSError:
        return False
    return False


def _usb_block_devices_sysfs(usb_sysfs: str, *, include_partitions: bool = False) -> list[str]:
    """Return block names belonging to this physical USB ancestor using sysfs only."""
    if not usb_sysfs:
        return []
    try:
        usb_root = Path(usb_sysfs).resolve()
    except OSError:
        return []
    result: list[str] = []
    block_root = Path("/sys/class/block")
    try:
        entries = list(block_root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not include_partitions and (entry / "partition").exists():
            continue
        try:
            target = entry.resolve()
        except OSError:
            continue
        if target == usb_root or usb_root in target.parents:
            result.append(entry.name)
    return sorted(set(result))


def _block_inflight_total(name: str) -> Optional[int]:
    raw = _safe_read(Path("/sys/class/block") / name / "inflight")
    if not raw:
        return None
    try:
        return sum(int(part) for part in raw.split())
    except ValueError:
        return None


def _usb_block_inflight_total(usb_sysfs: str) -> Optional[int]:
    """Return current in-flight block I/O for this USB device using sysfs only.

    ``None`` means the block device exists but Linux did not expose a readable
    inflight counter.  Zero means no request is currently queued/in service.
    This never opens the block device and therefore cannot itself trigger FAT I/O.
    """
    names = _usb_block_devices_sysfs(usb_sysfs)
    if not names:
        return 0
    values = [_block_inflight_total(name) for name in names]
    if any(value is None for value in values):
        return None
    return sum(int(value or 0) for value in values)


def _wait_for_usb_block_idle(usb_sysfs: str, *, timeout: float = 1.5, stable: float = 0.15) -> bool:
    """Wait for MSC block I/O to be continuously idle without touching the FAT."""
    if not usb_sysfs:
        return True
    deadline = time.monotonic() + max(0.0, timeout)
    idle_since: Optional[float] = None
    while time.monotonic() < deadline:
        inflight = _usb_block_inflight_total(usb_sysfs)
        now = time.monotonic()
        if inflight == 0:
            if idle_since is None:
                idle_since = now
            if now - idle_since >= stable:
                return True
        else:
            idle_since = None
        time.sleep(0.025)
    return False


def _set_storage_quarantine(reason: str) -> None:
    reason = str(reason or "MacroPad Mass Storage entered an unknown state")
    with STATE_LOCK:
        STATE["storage_quarantined"] = True
        STATE["storage_quarantine_reason"] = reason
        STATE["storage_error"] = reason
        STATE["storage_ready"] = False
        STATE["storage_root"] = None
        STATE["source_dir"] = None
        STATE["pc_monitor_last_error"] = reason
    LOG.error("MacroPad MSC quarantined until USB reconnect: %s", reason)


def _storage_quarantine_reason() -> Optional[str]:
    with STATE_LOCK:
        if not STATE.get("storage_quarantined"):
            return None
        return str(STATE.get("storage_quarantine_reason") or "MacroPad Mass Storage is quarantined")


def _wait_cdc_quiet_before_msc() -> None:
    """Hold the device gate until the last daemon CDC TX is safely behind us."""
    with STATE_LOCK:
        last_tx = float(STATE.get("last_cdc_tx_at") or 0.0)
    if last_tx <= 0:
        return
    remaining = MSC_PRE_ACCESS_CDC_QUIET - (time.monotonic() - last_tx)
    if remaining > 0:
        LOG.debug("MSC pre-access quiet period: waiting %.0f ms after last CDC TX", remaining * 1000.0)
        time.sleep(remaining)


def _usb_block_mounts(usb_sysfs: str) -> list[tuple[str, str]]:
    """Return (/dev/node, mountpoint) for this USB device without reading its FS."""
    names = set(_usb_block_devices_sysfs(usb_sysfs, include_partitions=True))
    if not names:
        return []
    mounts: list[tuple[str, str]] = []
    try:
        lines = Path("/proc/self/mounts").read_text(errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        source, mountpoint = parts[0], parts[1]
        if source.startswith("/dev/") and Path(source).name in names:
            mounts.append((source, mountpoint.replace("\\040", " ")))
    return mounts


def _runtime_storage_mounted() -> bool:
    with STATE_LOCK:
        sysfs = str(STATE.get("usb_sysfs") or "")
    return bool(_usb_block_mounts(sysfs)) if sysfs else False


def _unmount_usb_mounts_sysfs(usb_sysfs: str, *, only_automounts: bool = False) -> bool:
    """Unmount known mounts for this MacroPad without discovering files on the FAT volume."""
    mounts = _usb_block_mounts(usb_sysfs)
    if only_automounts:
        user = os.environ.get("USER") or ""
        prefixes = tuple(x for x in (f"/media/{user}/", f"/run/media/{user}/") if user)
        mounts = [(dev, mp) for dev, mp in mounts if prefixes and mp.startswith(prefixes)]
    ok = True
    seen: set[str] = set()
    for dev, mountpoint in mounts:
        if dev in seen:
            continue
        seen.add(dev)
        try:
            if shutil.which("udisksctl"):
                cmd = ["udisksctl", "unmount", "-b", dev]
            else:
                cmd = ["umount", mountpoint]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, timeout=12, check=False)
            if proc.returncode != 0:
                LOG.warning("Could not unmount MacroPad storage %s (%s): %s",
                            dev, mountpoint, proc.stdout.strip())
                ok = False
            else:
                LOG.info("MacroPad storage unmounted: %s (%s)", dev, mountpoint)
        except Exception as exc:
            LOG.warning("Could not unmount MacroPad storage %s: %s", dev, exc)
            ok = False
    return ok


def wait_for_runtime_ready(dev: str) -> bool:
    """Wait for one USB enumeration to be safe before opening the CDC tty.

    This function intentionally uses only sysfs/procfs. It never opens the tty,
    mounts the volume, runs blkid, or reads the FAT filesystem. On MSC-enabled
    runtime it waits until the block device exists, the initial SCSI activity has
    settled, and the queue has remained idle for a continuous interval.
    """
    info = usb_device_info_from_tty(dev)
    token = _usb_identity_token(info)
    usb_sysfs = str(info.get("sysfs") or "")
    pid = str(info.get("pid") or "").lower()
    if not usb_sysfs:
        return False

    if pid == "4005":
        _set_startup_phase("settling-safe-mode", "waiting to distinguish transient 4005 from stable Safe Mode")
        LOG.info("MacroPad PID 4005 detected; leaving CDC untouched for %.1fs",
                 USB_SAFE_MODE_SETTLE_SECONDS)
        deadline = time.monotonic() + USB_SAFE_MODE_SETTLE_SECONDS
        while RUNNING and time.monotonic() < deadline:
            if not _usb_identity_unchanged(dev, token):
                LOG.info("MacroPad USB identity changed while PID 4005 was settling; redetecting")
                return False
            time.sleep(0.10)
        if not RUNNING or not _usb_identity_unchanged(dev, token):
            return False
        _set_startup_phase("usb-ready", "stable Safe Mode; CDC probe allowed")
        LOG.info("MacroPad Safe Mode remained stable; CDC probe allowed")
        return True

    has_msc = _usb_has_mass_storage_interface(usb_sysfs)
    if not has_msc:
        _set_startup_phase("settling", "runtime has no MSC interface; waiting for USB identity stability")
        deadline = time.monotonic() + USB_NO_MSC_SETTLE_SECONDS
        while RUNNING and time.monotonic() < deadline:
            if not _usb_identity_unchanged(dev, token):
                return False
            time.sleep(0.10)
        if not RUNNING or not _usb_identity_unchanged(dev, token):
            return False
        _set_startup_phase("usb-ready", "USB identity stable; CDC probe allowed")
        return True

    _set_startup_phase("settling-msc", "waiting for Mass Storage/SCSI initialization to become idle")
    LOG.info("MacroPad MSC runtime detected; CDC will remain untouched until block I/O settles")
    deadline = time.monotonic() + USB_RUNTIME_SETTLE_TIMEOUT
    block_seen_at: Optional[float] = None
    idle_since: Optional[float] = None
    last_inflight: Optional[int] = None

    while RUNNING and time.monotonic() < deadline:
        if not _usb_identity_unchanged(dev, token):
            LOG.info("MacroPad USB identity changed during MSC settle; redetecting")
            return False
        blocks = _usb_block_devices_sysfs(usb_sysfs)
        now = time.monotonic()
        if not blocks:
            block_seen_at = None
            idle_since = None
            time.sleep(0.10)
            continue
        if block_seen_at is None:
            block_seen_at = now
            LOG.info("MacroPad block device appeared: %s; holding CDC for %.1fs minimum settle",
                     ", ".join("/dev/" + x for x in blocks), USB_MSC_SETTLE_SECONDS)

        totals = [_block_inflight_total(name) for name in blocks]
        known = all(value is not None for value in totals)
        inflight = sum(int(value or 0) for value in totals)
        if inflight != last_inflight:
            LOG.debug("MacroPad MSC inflight=%s blocks=%s", inflight if known else "unknown", blocks)
            last_inflight = inflight

        minimum_elapsed = (now - block_seen_at) >= USB_MSC_SETTLE_SECONDS
        if minimum_elapsed and known and inflight == 0:
            if idle_since is None:
                idle_since = now
            if now - idle_since >= USB_MSC_IDLE_STABLE_SECONDS:
                # Desktop automounters may have mounted the tiny FAT volume while
                # it was settling. Release only normal user-session automounts now,
                # before any CDC traffic starts. Explicit/manual mounts elsewhere
                # are left alone and background telemetry will stay suppressed.
                _unmount_usb_mounts_sysfs(usb_sysfs, only_automounts=True)
                time.sleep(MSC_POST_UNMOUNT_DELAY)
                if not _usb_identity_unchanged(dev, token):
                    return False
                remaining_mounts = _usb_block_mounts(usb_sysfs)
                if remaining_mounts:
                    _set_startup_phase("waiting-mounted", "MacroPad Mass Storage is mounted; CDC will stay untouched")
                    LOG.warning("MacroPad storage is still mounted (%s); refusing CDC probe until it is unmounted",
                                ", ".join(mp for _dev, mp in remaining_mounts))
                    return False
                blocks2 = _usb_block_devices_sysfs(usb_sysfs)
                inflight2 = sum(int(_block_inflight_total(name) or 0) for name in blocks2)
                if inflight2 != 0:
                    idle_since = None
                    continue
                _set_startup_phase("usb-ready", "MSC initialized and idle; CDC probe allowed")
                LOG.info("MacroPad USB runtime stable: MSC idle for %.1fs; CDC probe allowed",
                         USB_MSC_IDLE_STABLE_SECONDS)
                return True
        else:
            idle_since = None
        time.sleep(0.10)

    LOG.warning("MacroPad USB runtime did not reach a safe idle state within %.1fs; CDC was not opened",
                USB_RUNTIME_SETTLE_TIMEOUT)
    _set_startup_phase("waiting", "MSC did not become safely idle; will retry without touching CDC")
    return False


def serial_candidates() -> list[str]:
    found: list[str] = []
    for pattern in (
        "/dev/serial/by-id/*EezBotFun*",
        "/dev/serial/by-id/*Smart_Reach*",
        "/dev/serial/by-id/*MicroPad*",
    ):
        for path in sorted(glob.glob(pattern)):
            try:
                resolved = str(Path(path).resolve(strict=True))
            except (OSError, RuntimeError):
                LOG.debug("Ignoring stale serial symlink: %s", path)
                continue
            if not is_eezbotfun_runtime_tty(resolved):
                LOG.debug("Ignoring non-runtime EezBotFun serial candidate: %s", resolved)
                continue
            if resolved not in found:
                found.append(resolved)
    for path in sorted(glob.glob("/dev/ttyACM*")):
        if not os.path.exists(path) or path in found:
            continue
        if not is_eezbotfun_runtime_tty(path):
            LOG.debug("Ignoring non-EezBotFun ACM device: %s", path)
            continue
        found.append(path)
    return found


def open_serial(dev: str) -> int:
    fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    configure_serial(fd)
    return fd


def read_lines_for(fd: int, seconds: float) -> list[str]:
    deadline = time.monotonic() + seconds
    buf = b""
    lines: list[str] = []
    while time.monotonic() < deadline:
        timeout = max(0.0, min(0.2, deadline - time.monotonic()))
        readable, _, _ = select.select([fd], [], [], timeout)
        if not readable:
            continue
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            continue
        if not data:
            continue
        buf += data
        if len(buf) > MAX_SERIAL_BUFFER and b"\r\n" not in buf:
            LOG.debug("Probe serial excedeu %d bytes sem CRLF; descartando buffer", MAX_SERIAL_BUFFER)
            buf = b""
            continue
        while b"\r\n" in buf:
            raw, buf = buf.split(b"\r\n", 1)
            try:
                lines.append(raw.decode("ascii"))
            except UnicodeDecodeError:
                LOG.debug("Non-ASCII response: %s", raw.hex())
    return lines


def parse_profile_count_message(msg: str) -> Optional[int]:
    # Confirmed on real hardware: query command 'l' replies as n=<count>.
    if not msg.startswith("n="):
        return None
    try:
        value = int(msg[2:].strip())
    except ValueError:
        return None
    return value if 1 <= value <= MAX_PROFILE_UI else None


def probe_device(fd: int) -> tuple[Optional[str], Optional[str], Optional[int], list[str]]:
    """Query confirmed type (e), device ID (f), and profile count (l -> n=N)."""
    termios.tcflush(fd, termios.TCIFLUSH)
    for payload in ("e", "f", "l"):
        _write_fd_nonblocking(fd, frame(payload), timeout=1.0)
        time.sleep(0.04)
    lines = read_lines_for(fd, 1.3)
    dtype = did = None
    profile_count = None
    for msg in lines:
        if msg.startswith("e=") and len(msg) > 2:
            dtype = msg[2:].strip()
        elif msg.startswith("f=") and len(msg) > 2:
            did = msg[2:].strip()
        else:
            value = parse_profile_count_message(msg)
            if value is not None:
                profile_count = value
    return dtype, did, profile_count, lines


def _write_fd_nonblocking(fd: int, packet: bytes, timeout: float = 1.5) -> None:
    """Write a small serial packet without tcdrain().

    tcdrain() can remain blocked in a thread when a USB CDC ACM device disappears.
    That used to leave SERIAL_WRITE_LOCK permanently occupied even after the
    MacroPad reconnected.  The protocol frames are tiny, so waiting for fd
    writability and completing os.write() is sufficient for normal runtime.
    Firmware update keeps its proven 115200/tcdrain path separately.
    """
    view = memoryview(packet)
    deadline = time.monotonic() + timeout
    while view:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timeout waiting for serial write")
        try:
            _, writable, _ = select.select([], [fd], [], min(0.25, remaining))
        except (OSError, ValueError) as exc:
            raise OSError(f"invalid serial connection during write: {exc}") from exc
        if not writable:
            continue
        try:
            n = os.write(fd, view)
        except BlockingIOError:
            continue
        if n <= 0:
            raise OSError("escrita serial retornou 0 bytes")
        view = view[n:]


def reset_serial_write_lock(reason: str) -> None:
    """Replace the write mutex so a dead old tty cannot poison a new session."""
    global SERIAL_WRITE_LOCK
    SERIAL_WRITE_LOCK = threading.Lock()
    LOG.debug("Fila de escrita serial reinicializada: %s", reason)


def authenticate_device(fd: int, device_id: str) -> tuple[bool, list[str]]:
    """Perform the same read-only HMAC challenge used by the official Configurator.

    The official app authenticates before allowing configuration saves.  Runtime
    actions such as RGB can work without this, but key alias/display writes may be
    ignored by firmware when the session has not authenticated.
    """
    stamp = str(int(time.time()))
    expected = hmac.new(AUTH_KEY, (stamp + device_id).encode("utf-8"), hashlib.sha256).hexdigest().lower()
    try:
        _write_fd_nonblocking(fd, frame("g" + stamp), timeout=1.5)
        lines = read_lines_for(fd, 1.2)
    except (OSError, termios.error, TimeoutError) as exc:
        LOG.warning("Authentication: failed to send challenge: %s", exc)
        return False, []
    for msg in lines:
        if msg.startswith("g="):
            received = msg[2:].strip().lower()
            ok = hmac.compare_digest(received, expected)
            LOG.info("MacroPad authentication: %s", "OK" if ok else "FAILED")
            return ok, lines
    LOG.warning("MacroPad authentication: no g= response")
    return False, lines



def detect_device(required_usb: Optional[tuple[str, str]] = None):
    for dev in serial_candidates():
        fd = None
        try:
            usb_pre = usb_device_info_from_tty(dev)
            # During firmware update, never open a transient recovery personality.
            if required_usb is not None and (usb_pre.get("vid"), usb_pre.get("pid")) != required_usb:
                continue
            # Critical startup barrier: ttyACM may exist while usb-storage/SCSI is
            # still initializing. Do not even open/configure the tty until that
            # physical USB enumeration is demonstrably stable.
            if not wait_for_runtime_ready(dev):
                continue
            _set_startup_phase("probing", "USB stable; probing CDC identity")
            with DEVICE_IO_LOCK:
                # Re-check after taking the device-wide lock.
                usb_now = usb_device_info_from_tty(dev)
                if required_usb is not None and (usb_now.get("vid"), usb_now.get("pid")) != required_usb:
                    continue
                if not is_eezbotfun_runtime_tty(dev):
                    continue
                fd = open_serial(dev)
                dtype, did, profile_count, replies = probe_device(fd)
                if dtype is None:
                    os.close(fd)
                    fd = None
                    continue
                if not did:
                    usb_serial = usb_serial_from_tty(dev)
                    did = f"type{dtype}-{usb_serial}" if usb_serial else f"type{dtype}-{Path(dev).name}"
                authenticated, auth_replies = authenticate_device(fd, did)
                usb = usb_device_info_from_tty(dev)
                LOG.info(
                    "MacroPad detected: dev=%s type=%s id=%s profiles=%s usb=%s:%s auth=%s",
                    dev, dtype, did, profile_count, usb.get("vid") or "?", usb.get("pid") or "?",
                    "ok" if authenticated else "not-confirmed",
                )
                LOG.debug("Probe replies: %r auth replies: %r", replies, auth_replies)
                return {
                    "fd": fd, "dev": dev, "type": dtype, "id": did, "profile_count": profile_count,
                    "usb_sysfs": usb.get("sysfs"), "usb_vid": usb.get("vid"),
                    "usb_pid": usb.get("pid"), "usb_serial": usb.get("serial"),
                    "authenticated": authenticated,
                }
        except (OSError, termios.error, TimeoutError) as exc:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            LOG.debug("Failed to probe %s: %s", dev, exc)
    return None



def firmware_detection_target() -> Optional[tuple[str, str] | bool]:
    """Gate normal detection while the device is performing a USB-copy update.

    Returns:
      None  -> normal unrestricted detection
      False -> keep completely quiet; do not open any candidate serial device
      (vid, pid) -> the pre-update normal USB identity has returned; probe only it

    The guard deliberately has no automatic timeout that falls back to probing
    recovery mode: if an update fails, touching recovery automatically is less
    safe than remaining idle until the user intervenes/restarts the daemon.
    """
    with STATE_LOCK:
        waiting = bool(STATE.get("firmware_waiting_for_normal"))
        expected_vid = STATE.get("firmware_expected_vid")
        expected_pid = STATE.get("firmware_expected_pid")
        seen_transition = bool(STATE.get("firmware_seen_transition"))
    if not waiting:
        return None

    if not expected_vid or not expected_pid:
        # This should not happen on the known device, but still preserve a quiet
        # period instead of probing an updater/recovery personality immediately.
        with STATE_LOCK:
            started = STATE.get("firmware_wait_started") or time.monotonic()
            STATE["firmware_wait_started"] = started
        if time.monotonic() - float(started) < 15.0:
            return False
        LOG.warning("Firmware: normal USB identity unknown; allowing redetection after 15 s of silence")
        with STATE_LOCK:
            STATE["firmware_waiting_for_normal"] = False
        return None

    expected = (str(expected_vid).lower(), str(expected_pid).lower())
    normal_present = False
    for dev in serial_candidates():
        usb = usb_device_info_from_tty(dev)
        pair = ((usb.get("vid") or "").lower(), (usb.get("pid") or "").lower())
        if pair == expected:
            normal_present = True
            break

    if not seen_transition:
        if not normal_present:
            with STATE_LOCK:
                STATE["firmware_seen_transition"] = True
            LOG.info("Firmware: normal mode %s:%s disappeared; waiting for it to return without touching recovery mode", *expected)
        return False

    if not normal_present:
        return False

    return expected


def finish_firmware_reconnect_guard() -> None:
    with STATE_LOCK:
        if not STATE.get("firmware_waiting_for_normal"):
            return
        expected_vid = STATE.get("firmware_expected_vid")
        expected_pid = STATE.get("firmware_expected_pid")
        STATE["firmware_waiting_for_normal"] = False
        STATE["firmware_expected_vid"] = None
        STATE["firmware_expected_pid"] = None
        STATE["firmware_seen_transition"] = False
        STATE["firmware_wait_started"] = None
    LOG.info("Firmware: normal mode %s:%s returned; resuming EezOpen operation", expected_vid, expected_pid)


def data_root() -> Path:
    root = Path.home() / ".local" / "share" / APP_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def config_root() -> Path:
    root = data_root() / "configs"
    root.mkdir(parents=True, exist_ok=True)
    return root


def icon_root() -> Path:
    root = data_root() / "app_icons"
    root.mkdir(parents=True, exist_ok=True)
    return root


def local_config_dir(device_id: str) -> Path:
    path = config_root() / device_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def local_icon_dir(device_id: str) -> Path:
    path = icon_root() / device_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def script_root() -> Path:
    root = data_root() / "scripts"
    root.mkdir(parents=True, exist_ok=True)
    return root


def local_script_dir(device_id: str) -> Path:
    path = script_root() / device_id
    path.mkdir(parents=True, exist_ok=True)
    return path




def device_view_root(device_id: str) -> Path:
    """Ephemeral device snapshot used by the GUI; never the persistent home cache."""
    safe = "".join(ch for ch in str(device_id) if ch.isalnum() or ch in "-_.") or "unknown"
    root = runtime_dir() / "device-view" / safe
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def device_view_dirs(device_id: str) -> tuple[Path, Path, Path]:
    root = device_view_root(device_id)
    return root / "configs", root / "app_icons", root / "scripts"


def _looks_like_eez_root(root: Path) -> bool:
    try:
        return root.is_dir() and (root / "configs").is_dir() and (
            any((root / "configs").glob("profile_*_key_*.txt"))
            or (root / "app_icons").is_dir()
            or (root / "scripts").is_dir()
        )
    except OSError:
        return False


def _mounted_eez_roots() -> list[Path]:
    user = os.environ.get("USER") or Path.home().name
    roots: list[Path] = []
    with STATE_LOCK:
        known = STATE.get("storage_root")
    if known:
        p = Path(known)
        if _looks_like_eez_root(p):
            roots.append(p)
    for base in (Path("/run/media") / user, Path("/media") / user):
        if not base.is_dir():
            continue
        for p in sorted(base.iterdir()):
            if _looks_like_eez_root(p) and p not in roots:
                roots.append(p)
    return roots


def _current_usb_identity() -> tuple[Optional[Path], Optional[str], Optional[str]]:
    with STATE_LOCK:
        raw = STATE.get("usb_sysfs")
        vid = str(STATE.get("usb_vid") or "").lower() or None
        pid = str(STATE.get("usb_pid") or "").lower() or None
    target = None
    if raw:
        try:
            target = Path(str(raw)).resolve()
        except OSError:
            target = Path(str(raw))
    return target, vid, pid


def _block_matches_current_usb(name: str) -> bool:
    """Match a block device to the same physical USB parent as the tty.

    lsblk is called with -p, so KNAME can be `/dev/sdX`; always reduce it to
    the kernel basename before looking under /sys/class/block.  The physical
    sysfs path also survives a PID change across firmware modes.
    """
    name = Path(str(name)).name
    node = Path("/sys/class/block") / name
    try:
        node = node.resolve()
    except OSError:
        return False
    target, vid, pid = _current_usb_identity()
    lineage = (node, *node.parents)
    if target is not None and (node == target or target in node.parents):
        return True
    # Fallback when the tty sysfs path could not be captured.  Use the current
    # device's VID/PID, not a compile-time PID.
    if vid:
        for parent in lineage:
            pvid = _safe_read(parent / "idVendor").lower()
            ppid = _safe_read(parent / "idProduct").lower()
            if pvid == vid and (not pid or ppid == pid):
                return True
    return False


def _lsblk_records() -> list[dict[str, Any]]:
    if not shutil.which("lsblk"):
        return []
    try:
        out = subprocess.check_output(
            ["lsblk", "-b", "-J", "-p", "-o", "NAME,KNAME,TYPE,FSTYPE,MOUNTPOINTS,RM,SIZE,TRAN"],
            text=True, stderr=subprocess.DEVNULL, timeout=3,
        )
        data = json.loads(out)
    except Exception as exc:
        LOG.debug("lsblk failed: %s", exc)
        return []
    records: list[dict[str, Any]] = []
    def walk(items):
        for item in items or []:
            records.append(item)
            walk(item.get("children"))
    walk(data.get("blockdevices"))
    return records


def _item_has_mountable_fs(item: dict[str, Any]) -> bool:
    if str(item.get("type") or "").lower() not in {"disk", "part"}:
        return False
    fstype = str(item.get("fstype") or "").lower()
    return bool(fstype and fstype not in {"swap", "crypto_luks", "lvm2_member"})


def _tiny_removable_candidate(item: dict[str, Any]) -> bool:
    """Conservative fallback inspired by the official volume heuristic.

    The official Configurator searches ready removable drives below 3.2 MB.
    On Linux we additionally require USB/removable semantics and a filesystem;
    after mounting, _looks_like_eez_root() must still validate its contents.
    """
    if not _item_has_mountable_fs(item):
        return False
    try:
        size = int(item.get("size") or 0)
    except (TypeError, ValueError):
        return False
    rm_raw = item.get("rm")
    removable = rm_raw is True or str(rm_raw).lower() in {"1", "true", "yes"}
    transport_usb = str(item.get("tran") or "").lower() == "usb"
    fstype = str(item.get("fstype") or "").lower()
    fat_like = fstype in {"vfat", "fat", "msdos", "exfat"}
    return 0 < size < 3_200_000 and fat_like and (removable or transport_usb)


def _eez_block_candidates() -> tuple[list[dict[str, Any]], str]:
    records = _lsblk_records()
    direct: list[dict[str, Any]] = []
    for item in records:
        name = str(item.get("kname") or Path(str(item.get("name", ""))).name)
        if name and _item_has_mountable_fs(item) and _block_matches_current_usb(name):
            direct.append(item)
    direct.sort(key=lambda x: 0 if x.get("type") == "part" else 1)
    if direct:
        return direct, "same-usb"

    # Recovery/update firmware may enumerate the MSC function differently from
    # the runtime composite device.  Only accept this fallback when unambiguous.
    fallback = [item for item in records if _tiny_removable_candidate(item)]
    fallback.sort(key=lambda x: 0 if x.get("type") == "part" else 1)
    if len(fallback) == 1:
        LOG.info("Storage: using small removable USB volume fallback: %s", fallback[0].get("name"))
        return fallback, "tiny-removable"
    if len(fallback) > 1:
        LOG.warning("Storage: %d small USB volume candidates; refusing ambiguous association", len(fallback))
        return [], "ambiguous"
    return [], "none"


def _mountpoints(item: dict[str, Any]) -> list[Path]:
    raw = item.get("mountpoints")
    if isinstance(raw, str):
        raw = [raw]
    return [Path(x) for x in (raw or []) if x]


def _storage_failure(message: str) -> RuntimeError:
    with STATE_LOCK:
        STATE["storage_ready"] = False
        STATE["storage_error"] = message
    return RuntimeError(message)


def ensure_device_storage(try_mount: bool = True) -> Path:
    """Return mounted EezBotFun USB storage root when host MSC access is allowed."""
    if HOST_MSC_STABILITY_GUARD:
        raise _storage_failure(
            "USB Mass Storage access is disabled by the EezOpen stability guard; "
            "runtime uses CDC/HID plus the persistent local cache"
        )
    roots = _mounted_eez_roots()
    if len(roots) == 1:
        root = roots[0]
        with STATE_LOCK:
            STATE["storage_root"] = str(root)
            STATE["source_dir"] = str(root / "configs")
            STATE["storage_ready"] = True
            STATE["storage_error"] = None
        return root
    if len(roots) > 1:
        raise _storage_failure("More than one EezBotFun storage volume is mounted; refusing to guess which one to use")

    blocks, block_source = _eez_block_candidates()
    # A block device can already be mounted outside /run/media/$USER.
    for item in blocks:
        for mp in _mountpoints(item):
            if _looks_like_eez_root(mp):
                with STATE_LOCK:
                    STATE["storage_root"] = str(mp)
                    STATE["source_dir"] = str(mp / "configs")
                    STATE["storage_ready"] = True
                    STATE["storage_error"] = None
                return mp

    if try_mount and blocks:
        LOG.info("Storage: candidate(s) found via %s: %s", block_source, ", ".join(str(x.get("name")) for x in blocks))
        if not shutil.which("udisksctl"):
            raise _storage_failure("USB storage found but not mounted; install/enable udisks2 or mount the volume")
        errors = []
        for item in blocks:
            dev = str(item.get("name") or "")
            if not dev:
                continue
            try:
                proc = subprocess.run(
                    ["udisksctl", "mount", "-b", dev],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, timeout=12, check=False,
                )
                LOG.info("udisksctl mount %s: rc=%s %s", dev, proc.returncode, proc.stdout.strip())
                if proc.returncode != 0:
                    errors.append(proc.stdout.strip())
            except subprocess.TimeoutExpired as exc:
                # udisksctl is only the D-Bus client. Killing/timing out the
                # client does NOT guarantee that udisksd or the kernel mount
                # request stopped. Continuing with CDC here can recreate the
                # exact MSC/CDC race we are trying to avoid.
                reason = (
                    f"udisksctl mount {dev} timed out after {exc.timeout:g}s; "
                    "the underlying Mass Storage request may still be active. "
                    "CDC transmission is quarantined until the MacroPad is reconnected"
                )
                _set_storage_quarantine(reason)
                raise _storage_failure(reason) from exc
            except Exception as exc:
                errors.append(str(exc))
        time.sleep(0.25)
        roots = _mounted_eez_roots()
        if len(roots) == 1:
            root = roots[0]
            with STATE_LOCK:
                STATE["storage_root"] = str(root)
                STATE["source_dir"] = str(root / "configs")
                STATE["storage_ready"] = True
                STATE["storage_error"] = None
            return root
        # Try mountpoints reported by lsblk after the mount operation.
        refreshed, _ = _eez_block_candidates()
        for item in refreshed:
            for mp in _mountpoints(item):
                if _looks_like_eez_root(mp):
                    with STATE_LOCK:
                        STATE["storage_root"] = str(mp)
                        STATE["source_dir"] = str(mp / "configs")
                    return mp
        if errors:
            raise _storage_failure("EezBotFun storage detected but could not be mounted: " + " | ".join(x for x in errors if x))

    if blocks:
        raise _storage_failure("EezBotFun storage detected but is not mounted yet")
    raise _storage_failure("MacroPad USB storage was not found on the bus")



def _mounted_block_for_root(root: Path) -> Optional[str]:
    root = Path(root)
    for item in _lsblk_records():
        if root in _mountpoints(item):
            name = str(item.get("name") or "").strip()
            if name:
                return name
    return None


def unmount_device_storage(root: Path) -> None:
    """Release the MacroPad FAT mount without power-cycling the composite USB device."""
    root = Path(root)
    device = _mounted_block_for_root(root)
    if device is None:
        LOG.info("MacroPad storage already unmounted: %s", root)
    else:
        if shutil.which("udisksctl"):
            cmd = ["udisksctl", "unmount", "-b", device]
        elif shutil.which("umount"):
            cmd = ["umount", str(root)]
        else:
            raise RuntimeError("Cannot unmount MacroPad storage: udisksctl/umount not found")
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=12, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to unmount MacroPad storage {device}: {proc.stdout.strip()}")
        LOG.info("MacroPad storage unmounted: device=%s root=%s", device, root)
    with STATE_LOCK:
        if STATE.get("storage_root") == str(root):
            STATE["storage_root"] = None
        source = STATE.get("source_dir")
        if source and str(source).startswith(str(root) + os.sep):
            STATE["source_dir"] = None
        STATE["storage_ready"] = False
        STATE["storage_error"] = None


@contextmanager
def device_storage_session(*, write: bool = False):
    """Exclusive MSC session coordinated with every daemon CDC transmitter.

    The lock is acquired before the CDC->MSC quiet period and remains held until
    the filesystem is unmounted and block I/O has drained. If a mount times out,
    MSC is quarantined until physical reconnect because a timed-out udisksctl
    client cannot prove that the udisksd/kernel request was cancelled.
    """
    with DEVICE_IO_LOCK:
        with STATE_LOCK:
            if not STATE.get("connected") or not STATE.get("operational"):
                raise RuntimeError("MacroPad is not operational yet")
            if STATE.get("screen_script_active"):
                raise RuntimeError("Stop the Screen Script before accessing MacroPad USB Mass Storage")
            if STATE.get("storage_quarantined"):
                reason = str(STATE.get("storage_quarantine_reason") or "Mass Storage is quarantined")
                raise RuntimeError(f"{reason}. Reconnect the MacroPad before another storage operation")
            STATE["storage_busy"] = True
            STATE["pc_monitor_pause_until"] = max(
                float(STATE.get("pc_monitor_pause_until") or 0.0),
                time.monotonic() + 3600.0,
            )
            usb_sysfs = str(STATE.get("usb_sysfs") or "")

        mounted_before = {str(path) for path in _mounted_eez_roots()}
        root: Optional[Path] = None
        body_error: Optional[BaseException] = None
        attempted_storage = False
        try:
            # A lock prevents NEW daemon CDC frames, but the firmware may still
            # be finishing the frame sent just before we acquired it. Give the
            # composite device a real direction-change gap before mounting FAT.
            _wait_cdc_quiet_before_msc()
            if usb_sysfs and not _wait_for_usb_block_idle(
                    usb_sysfs,
                    timeout=MSC_PRE_ACCESS_IDLE_TIMEOUT,
                    stable=MSC_PRE_ACCESS_IDLE_STABLE):
                raise RuntimeError(
                    "MacroPad USB Mass Storage I/O is already active; "
                    "storage operation was not started"
                )

            attempted_storage = True
            root = ensure_device_storage(try_mount=True)
            yield root
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            cleanup_error: Optional[Exception] = None
            if root is not None:
                if write:
                    try:
                        flush_storage(root)
                    except Exception as exc:
                        cleanup_error = exc
                        LOG.error("MacroPad storage flush failed: %s", exc)
                # Even when flush reports an error, still attempt to release the
                # filesystem so a failed write cannot leave FAT mounted forever.
                if write or str(root) not in mounted_before:
                    try:
                        unmount_device_storage(root)
                    except Exception as exc:
                        if cleanup_error is None:
                            cleanup_error = exc
                        LOG.error("MacroPad storage unmount failed: %s", exc)

            # Drain the queue even when mounting failed before we obtained a
            # root. This was missing in v1.4.1 and allowed CDC to resume while a
            # timed-out udisksd request was still issuing SCSI commands.
            if attempted_storage and usb_sysfs:
                quarantined = _storage_quarantine_reason() is not None
                idle_timeout = 0.50 if quarantined else 2.50
                if not _wait_for_usb_block_idle(usb_sysfs, timeout=idle_timeout, stable=0.20):
                    if not quarantined:
                        LOG.warning("MacroPad block queue did not become idle promptly after storage cleanup")

            with STATE_LOCK:
                STATE["storage_busy"] = False
                STATE["pc_monitor_pause_until"] = time.monotonic() + MSC_POST_UNMOUNT_DELAY
                quarantined = bool(STATE.get("storage_quarantined"))

            # After a normal session, give the firmware a quiet post-MSC gap.
            # Quarantined sessions deliberately do not resume CDC at all.
            if attempted_storage and not quarantined:
                time.sleep(MSC_POST_UNMOUNT_DELAY)
            if cleanup_error is not None and body_error is None:
                raise cleanup_error


def replace_tree_from_source(source: Path, target: Path) -> None:
    tmp = target.with_name(target.name + ".tmp")
    backup = target.with_name(target.name + ".old")
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(source, tmp)
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists():
        target.rename(backup)
    try:
        tmp.rename(target)
        if backup.exists():
            shutil.rmtree(backup)
    except Exception:
        if not target.exists() and backup.exists():
            backup.rename(target)
        raise


def infer_profile_count(config_dir: Optional[Path], icon_dir: Optional[Path] = None,
                        script_dir: Optional[Path] = None) -> int:
    """Infer the highest profile represented by local per-key files."""
    highest = 1
    patterns: list[tuple[Optional[Path], str]] = [
        (config_dir, "profile_*_key_*.txt"),
        (icon_dir, "profile_*_key_*.png"),
        (script_dir, "profile_*_key_*.txt"),
    ]
    for base, pattern in patterns:
        if not base or not base.is_dir():
            continue
        for path in base.glob(pattern):
            parts = path.stem.split("_")
            try:
                highest = max(highest, int(parts[1]))
            except (ValueError, IndexError):
                continue
    return min(max(highest, 1), MAX_PROFILE_UI)

def _replace_tree_or_empty(source: Path, target: Path) -> bool:
    """Replace one destination tree with a directory from the MacroPad."""
    if source.is_dir():
        replace_tree_from_source(source, target)
        return True
    empty = target.with_name(target.name + ".empty")
    if empty.exists():
        shutil.rmtree(empty)
    empty.mkdir(parents=True)
    try:
        replace_tree_from_source(empty, target)
    finally:
        if empty.exists():
            shutil.rmtree(empty)
    return False


def _sync_device_view_from_root(root: Path, device_id: str) -> dict[str, Any]:
    """Mirror the connected MacroPad into an ephemeral runtime view only."""
    config_target, icon_target, script_target = device_view_dirs(device_id)
    source = root / "configs"
    configs_synced = _replace_tree_or_empty(source, config_target)
    icons_synced = _replace_tree_or_empty(root / "app_icons", icon_target)
    scripts_synced = _replace_tree_or_empty(root / "scripts", script_target)

    inferred = infer_profile_count(config_target, icon_target, script_target)
    with STATE_LOCK:
        current_source = str(STATE.get("profile_count_source") or "")
        has_serial_count = bool(STATE.get("connected")) and current_source == "serial"
        serial_count = int(STATE.get("profile_count") or 1) if has_serial_count else None
        STATE["view_config_dir"] = str(config_target)
        STATE["view_icon_dir"] = str(icon_target)
        STATE["view_script_dir"] = str(script_target)
        STATE["source_dir"] = str(source)
        STATE["storage_root"] = str(root)
        STATE["profile_count"] = serial_count if serial_count is not None else inferred
        STATE["profile_count_source"] = "serial" if serial_count is not None else "device-files"
    LOG.info("Imported MacroPad into temporary device view: %s -> %s", source, config_target)
    return {
        "ok": True, "view_config_dir": str(config_target), "source_dir": str(source),
        "storage_root": str(root),
        "configs": len(list(config_target.glob("profile_*_key_*.txt"))),
        "configs_synced": configs_synced, "icons_synced": icons_synced,
        "scripts_synced": scripts_synced, "profile_count_inferred": inferred,
        "persistent_cache_modified": False,
    }



def sync_from_device(device_id: str) -> dict[str, Any]:
    """Refresh the ephemeral device view without modifying the persistent cache."""
    with OPERATION_LOCK:
        with device_storage_session(write=False) as root:
            return _sync_device_view_from_root(root, device_id)




def import_from_device(device_id: str) -> dict[str, Any]:
    """Explicitly replace the persistent EezOpen cache with the MacroPad contents."""
    with STATE_LOCK:
        connected = bool(STATE.get("connected") and STATE.get("operational"))
        selected_id = str(STATE.get("id") or "")
    if not connected:
        raise RuntimeError("MacroPad is not operational yet")
    if not selected_id or selected_id != str(device_id):
        raise RuntimeError("The selected MacroPad changed before the import started")

    with OPERATION_LOCK:
        with device_storage_session(write=False) as root:
            config_target = local_config_dir(device_id)
            icon_target = local_icon_dir(device_id)
            script_target = local_script_dir(device_id)
            configs_synced = _replace_tree_or_empty(root / "configs", config_target)
            icons_synced = _replace_tree_or_empty(root / "app_icons", icon_target)
            scripts_synced = _replace_tree_or_empty(root / "scripts", script_target)
            view_result = _sync_device_view_from_root(root, device_id)

        with STATE_LOCK:
            STATE["config_dir"] = str(config_target)
            STATE["icon_dir"] = str(icon_target)
            STATE["script_dir"] = str(script_target)

        configs = len(list(config_target.glob("profile_*_key_*.txt")))
        scripts = len(list(script_target.glob("profile_*_key_*.txt")))
        icons = len(list(icon_target.glob("*"))) if icon_target.is_dir() else 0
        LOG.info("Imported MacroPad into persistent cache: id=%s configs=%s scripts=%s icons=%s",
                 device_id, configs, scripts, icons)
        return {
            "ok": True, "device_id": device_id,
            "config_dir": str(config_target), "script_dir": str(script_target),
            "icon_dir": str(icon_target), "configs": configs, "scripts": scripts,
            "icons": icons, "configs_synced": configs_synced,
            "scripts_synced": scripts_synced, "icons_synced": icons_synced,
            "persistent_cache_modified": True,
            "view_config_dir": view_result.get("view_config_dir"),
        }



def _refresh_view_key_from_cache(device_id: str, profile: int, key: int) -> None:
    """Reflect an explicit Save into the live view after persisting it in home."""
    with STATE_LOCK:
        if not STATE.get("connected") or str(STATE.get("id") or "") != str(device_id):
            return
        view_config_raw = STATE.get("view_config_dir")
        view_icon_raw = STATE.get("view_icon_dir")
        view_script_raw = STATE.get("view_script_dir")
    if not (view_config_raw and view_icon_raw and view_script_raw):
        return

    name_txt = f"profile_{profile}_key_{key}.txt"
    name_png = f"profile_{profile}_key_{key}.png"
    pairs = [
        (local_config_dir(device_id) / name_txt, Path(view_config_raw) / name_txt),
        (local_script_dir(device_id) / name_txt, Path(view_script_raw) / name_txt),
        (local_icon_dir(device_id) / name_png, Path(view_icon_raw) / name_png),
    ]
    for source, target in pairs:
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_file():
            copy_file_fsync(source, target)
        else:
            target.unlink(missing_ok=True)


def parse_config_file(filename: Path) -> Optional[dict[str, Any]]:
    if not filename.exists():
        return None
    cfg: dict[str, Any] = {"file": str(filename)}
    try:
        lines = filename.read_text(errors="replace").splitlines()
    except OSError:
        LOG.exception("Could not read %s", filename)
        return None
    actions: list[dict[str, Any]] = []
    for line in lines:
        if line.startswith("Alias="):
            cfg["alias"] = line[6:]
        elif line.startswith("HidMode="):
            cfg["hid"] = line[8:].lower() == "true"
        elif line.startswith("EezOpenExternalScript="):
            # EezOpen-only metadata. This line is kept in the HOME cache but
            # is deliberately stripped before a config is written to the
            # MacroPad, so the device only sees its known ACT syntax.
            cfg["eezopen_external_script"] = line.split("=", 1)[1].strip().lower() == "true"
        elif line.startswith("ACT "):
            parts = line.split(" ", 2)
            if len(parts) >= 2:
                action_id = parts[1]
                parsed_id: Any = int(action_id) if action_id.isdigit() else action_id
                arg = parts[2] if len(parts) == 3 else ""
                # Keep the legacy single-action fields exactly as before while
                # also exposing every ACT line to the new HID editor.
                cfg["act"] = parsed_id
                cfg["arg"] = arg
                actions.append({"act": parsed_id, "arg": arg})
    if actions:
        cfg["actions"] = actions
    return cfg


def read_config(config_dir: Optional[Path], profile: str | int, key: str | int):
    if not config_dir:
        return None
    return parse_config_file(config_dir / f"profile_{profile}_key_{key}.txt")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        fp.write(text)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, path)


def device_compatible_config_text(text: str) -> str:
    """Return the config representation safe to persist on the MacroPad.

    External Script is an EezOpen GUI/runtime feature, but the physical device
    should only receive action IDs already observed in the official format.
    New EezOpen configs therefore keep ACT 2 on-device and store the
    External-Script distinction only as HOME metadata. Legacy ACT p cache
    entries are translated to ACT 2 when copied to the device.
    """
    output: list[str] = []
    for line in str(text).splitlines():
        if line.startswith("EezOpenExternalScript="):
            continue
        if line == "ACT p":
            line = "ACT 2"
        elif line.startswith("ACT p "):
            line = "ACT 2 " + line[6:]
        output.append(line)
    return "\n".join(output) + ("\n" if str(text).endswith("\n") else "")


def copy_file_fsync(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(destination.name + ".tmp")
    with source.open("rb") as src, tmp.open("wb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    os.replace(tmp, destination)


def write_device_text_direct(path: Path, text: str) -> None:
    """Write a MacroPad MSC file at its final pathname.

    The official configurator writes directly to the final config/script/icon
    path.  Avoid temp-file + rename on the device volume because the firmware
    may cache directory entries while the USB mass-storage interface is live.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    with path.open("wb") as fp:
        fp.write(data)
        fp.flush()
        os.fsync(fp.fileno())


def copy_device_file_direct(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, destination.open("wb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())


def copy_device_icon_atomic(source: Path, destination: Path) -> None:
    """Replace one MacroPad icon without truncating the live pathname first.

    This is intentionally limited to dynamic icon updates. Config/script writes
    keep the already validated direct-write behavior. The temporary file lives
    in the same directory so os.replace() becomes a same-filesystem rename.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.eezopen-tmp")
    try:
        with source.open("rb") as src, temporary.open("wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def files_have_same_content(source: Path, destination: Path) -> bool:
    """Return True only when both files exist and have identical bytes."""
    try:
        if not source.is_file() or not destination.is_file():
            return False
        if source.stat().st_size != destination.stat().st_size:
            return False
        source_hash = hashlib.sha256(source.read_bytes()).digest()
        destination_hash = hashlib.sha256(destination.read_bytes()).digest()
        return hmac.compare_digest(source_hash, destination_hash)
    except OSError:
        return False


def per_key_icon_path(icon_dir: Optional[Path], profile: int, key: int) -> Optional[Path]:
    if not icon_dir:
        return None
    path = icon_dir / f"profile_{profile}_key_{key}.png"
    return path if path.is_file() else None


def png_info(path: Path) -> tuple[int, int, int, int]:
    with path.open("rb") as fp:
        header = fp.read(26)
    if len(header) < 26 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError("Icon must be a PNG")
    return (
        int.from_bytes(header[16:20], "big"),
        int.from_bytes(header[20:24], "big"),
        header[24],  # bit depth
        header[25],  # PNG color type (6 = RGBA)
    )


def copy_key_icon(profile: int, key: int, supplied: Optional[str], clear_icon: bool, root: Optional[Path]) -> dict[str, Any]:
    with STATE_LOCK:
        icon_raw = STATE.get("icon_dir")
    if not icon_raw:
        raise RuntimeError("Icon cache is not initialized")
    local_dir = Path(icon_raw)
    local_dir.mkdir(parents=True, exist_ok=True)
    name = f"profile_{profile}_key_{key}.png"
    local = local_dir / name
    device = (root / "app_icons" / name) if root else None
    if clear_icon:
        local_existed = local.is_file()
        device_existed = bool(device and device.is_file())
        local.unlink(missing_ok=True)
        if device:
            device.unlink(missing_ok=True)
        changed = local_existed or device_existed
        return {
            "icon_changed": changed,
            "device_changed": device_existed,
            "icon_removed": changed,
            "icon_path": None,
        }
    if not supplied:
        return {
            "icon_changed": False,
            "device_changed": False,
            "icon_removed": False,
            "icon_path": str(local) if local.is_file() else None,
        }
    src = Path(supplied).expanduser().resolve()
    if not src.is_file() or src.is_symlink():
        raise ValueError("Invalid icon file")
    width, height, bit_depth, color_type = png_info(src)
    if (width, height) != (64, 64):
        raise ValueError(f"Icon must be 64x64 px; received {width}x{height}")

    local_changed = not files_have_same_content(src, local)
    device_changed = bool(device and not files_have_same_content(src, device))

    if local_changed:
        copy_file_fsync(src, local)
    if device_changed and device is not None:
        device.parent.mkdir(parents=True, exist_ok=True)
        copy_device_icon_atomic(src, device)
        LOG.info("USB icon atomically replaced: %s (%sx%s, depth=%s, png-color-type=%s)",
                 device, width, height, bit_depth, color_type)
    elif device is not None:
        LOG.debug("USB icon already identical; write skipped: %s", device)

    return {
        "icon_changed": local_changed or device_changed,
        "device_changed": device_changed,
        "icon_removed": False,
        "icon_path": str(local),
        "icon_png_color_type": color_type,
    }


def spawn(argv: list[str]) -> None:
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def normalize_action_id(act: Any) -> str:
    return str(act).strip().lower()


def parse_external_script_command(command: str) -> tuple[str, list[str]]:
    try:
        argv = shlex.split(str(command or ""))
    except ValueError as exc:
        raise ValueError(f"Invalid External Script arguments: {exc}") from exc
    if not argv:
        raise ValueError("External Script path is empty")
    return argv[0], argv[1:]


def resolve_external_script_executable(script: str) -> str:
    raw = os.path.expandvars(os.path.expanduser(str(script).strip()))
    if not raw:
        raise ValueError("External Script path is empty")
    if "/" in raw:
        path = Path(raw)
        if not path.is_file():
            raise FileNotFoundError(f"External Script executable not found: {path}")
        if not os.access(path, os.X_OK):
            raise PermissionError(f"External Script file is not executable: {path}")
        return str(path.resolve())
    resolved = shutil.which(raw)
    if not resolved:
        raise FileNotFoundError(f"External Script executable not found in PATH: {raw}")
    return resolved


def external_script_environment(profile: Optional[int], key: Optional[int], alias: str) -> dict[str, str]:
    env = os.environ.copy()
    if profile is not None:
        env["EEZOPEN_PROFILE"] = str(profile)
    if key is not None:
        env["EEZOPEN_KEY"] = str(key)
    with STATE_LOCK:
        device_id = str(STATE.get("id") or "")
    if device_id:
        env["EEZOPEN_DEVICE_ID"] = device_id
    env["EEZOPEN_ALIAS"] = str(alias or "")
    return env


def launch_external_script(command: str, alias: str, profile: int, key: int) -> None:
    script, arguments = parse_external_script_command(command)
    argv = [resolve_external_script_executable(script), *arguments]
    env = external_script_environment(profile, key, alias)
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True, env=env)
    LOG.info("EXTERNAL SCRIPT start profile=%s key=%s pid=%s argv=%r",
             profile, key, proc.pid, argv)

    # Reap the child without tying process lifetime to any EezOpen key state.
    def reap() -> None:
        try:
            rc = proc.wait()
            LOG.info("EXTERNAL SCRIPT exit profile=%s key=%s pid=%s rc=%s",
                     profile, key, proc.pid, rc)
        except Exception:
            LOG.exception("EXTERNAL SCRIPT wait failed profile=%s key=%s pid=%s",
                          profile, key, proc.pid)

    threading.Thread(target=reap, name=f"eezopen-external-script-{profile}-{key}-{proc.pid}", daemon=True).start()


def test_external_script_command(script: str, arguments: list[str], alias: str = "GUI test") -> dict[str, Any]:
    if not isinstance(arguments, list) or not all(isinstance(x, str) for x in arguments):
        raise ValueError("External Script arguments must be a list of strings")
    argv = [resolve_external_script_executable(script), *arguments]
    try:
        completed = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, errors="replace", timeout=30.0,
                                   env=external_script_environment(None, None, alias))
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("External Script test exceeded 30 seconds") from exc
    return {
        "ok": True,
        "success": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-65536:],
        "stderr": completed.stderr[-65536:],
        "argv": argv,
    }


def run_action(cfg: dict[str, Any], profile: Optional[int] = None, key: Optional[int] = None) -> None:
    if cfg.get("hid") is True:
        return
    act = normalize_action_id(cfg.get("act", ""))
    arg = str(cfg.get("arg", ""))
    LOG.info("ACTION act=%r alias=%r arg=%r", act, cfg.get("alias"), arg)
    if act == "1" and arg:
        spawn(["xdg-open", arg if "://" in arg else "https://" + arg])
    elif act == "2":
        if cfg.get("eezopen_external_script") is True:
            if profile is None or key is None:
                LOG.error("External Script execution ignored without profile/key context")
                return
            try:
                launch_external_script(arg, str(cfg.get("alias", "")), int(profile), int(key))
            except Exception as exc:
                LOG.error("EXTERNAL SCRIPT failed to start profile=%s key=%s: %s", profile, key, exc)
            return
        argv = shlex.split(arg)
        if not argv:
            return
        exe = shutil.which(argv[0])
        if not exe:
            LOG.error("Executable not found: %s", argv[0])
            return
        argv[0] = exe
        spawn(argv)
    elif act in {"3", "f"} and arg:
        spawn(["xdg-open", arg])
    elif act == "p":
        if profile is None or key is None:
            LOG.error("External Script execution ignored without profile/key context")
            return
        try:
            launch_external_script(arg, str(cfg.get("alias", "")), int(profile), int(key))
        except Exception as exc:
            LOG.error("EXTERNAL SCRIPT failed to start profile=%s key=%s: %s", profile, key, exc)
    elif act not in SUPPORTED_ACTIONS:
        LOG.warning("ACT not supported by EezOpen: %r", act)


def invalidate_serial(fd: Optional[int], reason: str) -> None:
    """Invalidate the current serial connection and wake the reconnect loop."""
    with STATE_LOCK:
        current = STATE.get("fd")
        if fd is not None and current == fd:
            STATE["connected"] = False
            STATE["fd"] = None
            STATE["active_profile"] = None
    # A thread may be stuck inside a kernel tty operation while holding the old
    # mutex.  New USB sessions must never inherit that poisoned queue.
    reset_serial_write_lock(reason)
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    LOG.info("Serial connection invalidated: %s", reason)



def serial_payload(payload: str, *, allow_screen_script: bool = False) -> None:
    """Send one EBF command while holding the physical-device I/O gate."""
    with DEVICE_IO_LOCK:
        with STATE_LOCK:
            fd = STATE.get("fd") if STATE.get("connected") else None
            generation = int(STATE.get("connection_generation") or 0)
            operational = bool(STATE.get("operational"))
            screen_active = bool(STATE.get("screen_script_active"))
            storage_busy = bool(STATE.get("storage_busy"))
            storage_quarantined = bool(STATE.get("storage_quarantined"))
            quarantine_reason = str(STATE.get("storage_quarantine_reason") or "")
            usb_sysfs = str(STATE.get("usb_sysfs") or "")
        if fd is None or not operational:
            raise RuntimeError("MacroPad is not operational yet")
        if storage_quarantined:
            raise RuntimeError(
                (quarantine_reason or "MacroPad USB Mass Storage is quarantined") +
                "; CDC writes are disabled until the MacroPad is reconnected"
            )
        if storage_busy:
            raise RuntimeError("MacroPad USB Mass Storage operation is in progress")
        # A mounted-but-idle FAT volume is not itself an occupied CDC channel.
        # Blocking every foreground command merely because the desktop mounted
        # the volume made the Configurator unusable.  What matters for firmware
        # safety is active MSC I/O.  Since DEVICE_IO_LOCK is already held, no
        # EezOpen storage transaction can start while we perform this check.
        if usb_sysfs and not _wait_for_usb_block_idle(
                usb_sysfs,
                timeout=MSC_FOREGROUND_IDLE_TIMEOUT,
                stable=MSC_FOREGROUND_IDLE_STABLE):
            raise RuntimeError("MacroPad USB Mass Storage I/O is active; serial command was not sent")
        if screen_active and not allow_screen_script:
            raise RuntimeError("Screen Script is active; daemon CDC writes are paused to keep one serial writer")

        write_lock = SERIAL_WRITE_LOCK
        if not write_lock.acquire(timeout=2.0):
            invalidate_serial(fd, "fila de escrita serial travada")
            raise RuntimeError("Serial write queue stalled; connection invalidated for redetection")
        try:
            with STATE_LOCK:
                if (not STATE.get("connected") or not STATE.get("operational") or
                        STATE.get("fd") != fd or
                        int(STATE.get("connection_generation") or 0) != generation):
                    raise RuntimeError("Serial connection changed during the command; try again")
                if STATE.get("screen_script_active") and not allow_screen_script:
                    raise RuntimeError("Screen Script became active; serial command cancelled")
            _write_fd_nonblocking(fd, frame(payload), timeout=1.5)
            with STATE_LOCK:
                STATE["last_cdc_tx_at"] = time.monotonic()
        except (OSError, TimeoutError) as exc:
            invalidate_serial(fd, f"write {payload!r} failed: {exc}")
            raise RuntimeError("Serial connection dropped; waiting for MacroPad redetection") from exc
        finally:
            try:
                write_lock.release()
            except RuntimeError:
                pass
        LOG.debug("TX payload=%r generation=%s", payload, generation)



def pc_read_number(path: Path, divisor: float = 1.0) -> Optional[float]:
    try:
        return float(path.read_text(errors="replace").strip()) / divisor
    except (OSError, ValueError, ZeroDivisionError):
        return None


def pc_hwmon_entries() -> list[tuple[str, Path]]:
    result: list[tuple[str, Path]] = []
    for raw in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        root = Path(raw)
        try:
            name = (root / "name").read_text(errors="replace").strip().lower()
        except OSError:
            name = root.name.lower()
        result.append((name, root))
    return result


def pc_hwmon_temperature(kind: str) -> float:
    candidates: list[tuple[int, float]] = []
    for name, root in pc_hwmon_entries():
        for input_path in root.glob("temp*_input"):
            value = pc_read_number(input_path, 1000.0)
            if value is None or not (-20.0 <= value <= 200.0):
                continue
            stem = input_path.name[:-6]
            try:
                label = (root / f"{stem}_label").read_text(errors="replace").strip().lower()
            except OSError:
                label = ""
            text = f"{name} {label}"
            score = 0
            if kind == "cpu":
                if name in {"k10temp", "coretemp", "zenpower"}:
                    score += 100
                if any(x in text for x in ("tctl", "tdie", "package", "cpu", "core")):
                    score += 60
                if any(x in text for x in ("nvme", "gpu", "amdgpu")):
                    score -= 100
            elif kind == "storage":
                if any(x in text for x in ("nvme", "drivetemp", "ssd", "composite")):
                    score += 100
            elif kind == "board":
                if any(x in text for x in ("acpitz", "nct", "it87", "motherboard", "system", "board")):
                    score += 80
                if any(x in text for x in ("nvme", "gpu", "amdgpu")):
                    score -= 100
            if score > 0:
                candidates.append((score, value))
    if not candidates:
        return 0.0
    candidates.sort(key=lambda item: item[0], reverse=True)
    return round(candidates[0][1], 1)


def pc_hwmon_fan_value() -> float:
    for _name, root in pc_hwmon_entries():
        for path in sorted(root.glob("fan*_input")):
            value = pc_read_number(path)
            if value is not None and value >= 0:
                return round(value, 1)
    return 0.0


def pc_cpu_load_percent() -> float:
    global PC_CPU_PREV
    try:
        fields = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        values = [int(x) for x in fields]
        total = sum(values)
        idle = values[3] + (values[4] if len(values) > 4 else 0)
    except (OSError, ValueError, IndexError):
        return 0.0

    previous = PC_CPU_PREV
    PC_CPU_PREV = (total, idle)
    if previous is None:
        return 0.0
    dt = total - previous[0]
    di = idle - previous[1]
    if dt <= 0:
        return 0.0
    return round(max(0.0, min(100.0, 100.0 * (dt - di) / dt)), 1)


def _pc_cpu_energy_uj() -> Optional[float]:
    """Best-effort CPU/package energy counter in microjoules."""
    candidates = []
    candidates.extend(Path("/sys/class/powercap").glob("**/energy_uj"))
    for name, root in pc_hwmon_entries():
        if "amd_energy" in name or "energy" in name or "cpu" in name:
            candidates.extend(root.glob("energy*_input"))
    for path in candidates:
        value = pc_read_number(path)
        if value is not None and value >= 0:
            return float(value)
    return None


def pc_cpu_power_watts(cpu_load: float) -> float:
    """Get CPU package power when possible, otherwise use vendor-reference fallback."""
    global PC_CPU_ENERGY_PREV

    for name, root in pc_hwmon_entries():
        if name not in {"k10temp", "coretemp", "zenpower", "amd_energy"} and "cpu" not in name:
            continue
        for pattern in ("power*_average", "power*_input"):
            for path in root.glob(pattern):
                value = pc_read_number(path, 1_000_000.0)
                if value is not None and 0 <= value < 2000:
                    return round(value, 1)

    energy_uj = _pc_cpu_energy_uj()
    now = time.monotonic()
    if energy_uj is not None:
        previous = PC_CPU_ENERGY_PREV
        PC_CPU_ENERGY_PREV = (energy_uj, now)
        if previous is not None:
            delta_uj = energy_uj - previous[0]
            delta_s = now - previous[1]
            if delta_uj >= 0 and delta_s > 0:
                watts = (delta_uj / 1_000_000.0) / delta_s
                if 0 <= watts < 2000:
                    return round(watts, 1)

    # Same fallback idea used by the vendor's published Python reference:
    # keep the field useful when Linux exposes no package-power sensor.
    return round(10.0 + cpu_load * 0.2, 1)


def pc_memory_info() -> dict[str, float]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            try:
                values[key] = int(rest.strip().split()[0]) * 1024
            except (ValueError, IndexError):
                pass
    except OSError:
        pass

    total = float(values.get("MemTotal", 0))
    avail = float(values.get("MemAvailable", values.get("MemFree", 0)))
    used = max(0.0, total - avail)
    percent = used / total * 100.0 if total else 0.0
    gib = 1024.0 ** 3
    return {
        "used": round(used / gib, 3),
        "avail": round(avail / gib, 3),
        "percent": round(percent, 1),
    }


def pc_storage_io_mb() -> tuple[float, float]:
    read_sectors = 0
    write_sectors = 0
    try:
        for line in Path("/proc/diskstats").read_text().splitlines():
            parts = line.split()
            if len(parts) < 14:
                continue
            name = parts[2]
            if name.startswith(("loop", "ram", "fd", "sr", "dm-")):
                continue
            if ((name.startswith(("sd", "vd", "xvd")) and name[-1:].isdigit())
                    or (name.startswith("nvme") and "p" in name)
                    or (name.startswith("mmcblk") and "p" in name)):
                continue
            try:
                read_sectors += int(parts[5])
                write_sectors += int(parts[9])
            except ValueError:
                pass
    except OSError:
        pass
    return (
        round(read_sectors * 512 / (1024 ** 2), 1),
        round(write_sectors * 512 / (1024 ** 2), 1),
    )


def pc_storage_info() -> dict[str, float]:
    try:
        usage = shutil.disk_usage("/")
        percent = usage.used / usage.total * 100.0 if usage.total else 0.0
    except OSError:
        percent = 0.0
    read_mb, write_mb = pc_storage_io_mb()
    return {
        "temp": pc_hwmon_temperature("storage"),
        "read": read_mb,
        "write": write_mb,
        "percent": round(percent, 1),
    }


def pc_network_info() -> dict[str, float]:
    rx = 0
    tx = 0
    try:
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
            if ":" not in line:
                continue
            iface, rest = line.split(":", 1)
            if iface.strip() == "lo":
                continue
            fields = rest.split()
            if len(fields) >= 9:
                rx += int(fields[0])
                tx += int(fields[8])
    except (OSError, ValueError):
        pass
    mib = 1024.0 ** 2
    return {"up": round(tx / mib, 1), "down": round(rx / mib, 1)}



def pc_empty_gpu_info() -> dict[str, float]:
    return {
        "temp": 0.0,
        "tempMax": 0.0,
        "load": 0.0,
        "consume": 0.0,
        "rpm": 0.0,
        "memUsed": 0.0,
        "memTotal": 0.0,
        "freq": 0.0,
    }


def pc_intel_drm_card() -> Optional[Path]:
    """Return an Intel DRM card path such as /sys/class/drm/card1."""
    for raw in sorted(glob.glob("/sys/class/drm/card[0-9]*")):
        card = Path(raw)
        vendor = ""
        try:
            vendor = (card / "device/vendor").read_text(errors="replace").strip().lower()
        except OSError:
            continue
        if vendor == "0x8086":
            return card
    return None


def pc_intel_pci_slot(card: Path) -> Optional[str]:
    """Read PCI_SLOT_NAME (for example 0000:00:02.0) for intel_gpu_top."""
    try:
        for line in (card / "device/uevent").read_text(errors="replace").splitlines():
            if line.startswith("PCI_SLOT_NAME="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def pc_intel_sysfs_frequency(card: Path) -> float:
    """Best-effort Intel GT frequency in MHz."""
    candidates = [
        card / "gt/gt0/rps_act_freq_mhz",
        card / "gt/gt0/rps_cur_freq_mhz",
        card / "device/gt_act_freq_mhz",
        card / "device/gt_cur_freq_mhz",
    ]

    # Prefer a non-zero actual frequency. If actual is zero while idle,
    # fall back to the current/requested GT frequency.
    zero_value: Optional[float] = None
    for path in candidates:
        value = pc_read_number(path)
        if value is None:
            continue
        if value > 0:
            return round(value, 1)
        if zero_value is None:
            zero_value = value

    return round(zero_value or 0.0, 1)


def pc_intel_gpu_temperature() -> float:
    """Only accept a temperature exposed by an Intel GPU hwmon device."""
    for name, root in pc_hwmon_entries():
        lname = name.lower()
        if lname not in {"i915", "xe"} and "i915" not in lname and lname != "intel_gpu":
            continue

        for path in sorted(root.glob("temp*_input")):
            value = pc_read_number(path, 1000.0)
            if value is not None and -20.0 <= value <= 200.0:
                return round(value, 1)

    # Do not reuse CPU/Dell 'Other' temperatures as a fake GPU temperature.
    return 0.0


def pc_parse_intel_gpu_top_output(output: str) -> Optional[dict[str, Any]]:
    """Parse either a JSON array or intel_gpu_top's streamed JSON objects."""
    output = output.strip()
    if not output:
        return None

    try:
        parsed = json.loads(output)
        if isinstance(parsed, list):
            objects = [x for x in parsed if isinstance(x, dict)]
            return objects[-1] if objects else None
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Some versions emit a stream such as:
    #   { ... },
    #   { ... },
    # rather than one immediately parseable object.
    decoder = json.JSONDecoder()
    pos = 0
    objects: list[dict[str, Any]] = []

    while pos < len(output):
        while pos < len(output) and output[pos] in " \r\n\t,[]":
            pos += 1
        if pos >= len(output):
            break

        try:
            obj, end = decoder.raw_decode(output, pos)
        except json.JSONDecodeError:
            pos += 1
            continue

        if isinstance(obj, dict):
            objects.append(obj)
        pos = end

    return objects[-1] if objects else None


def pc_intel_gpu_top_sample(card: Path) -> Optional[dict[str, Any]]:
    """Collect a short intel_gpu_top sample when the tool is installed."""
    exe = shutil.which("intel_gpu_top")
    if not exe:
        return None

    cmd = [exe, "-J", "-s", "250", "-n", "2"]

    slot = pc_intel_pci_slot(card)
    if slot:
        cmd.extend(["-d", f"pci:slot={slot}"])

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    return pc_parse_intel_gpu_top_output(proc.stdout)


def pc_intel_gpu_info() -> dict[str, float]:
    """Intel iGPU metrics without inventing unsupported sensors."""
    global PC_GPU_TEMP_MAX

    result = pc_empty_gpu_info()
    card = pc_intel_drm_card()
    if card is None:
        return result

    # These are available even without intel_gpu_top on the tested Iris Xe.
    result["freq"] = pc_intel_sysfs_frequency(card)

    temp = pc_intel_gpu_temperature()
    if temp > 0:
        PC_GPU_TEMP_MAX = max(PC_GPU_TEMP_MAX, temp)
        result["temp"] = temp
        result["tempMax"] = round(PC_GPU_TEMP_MAX, 1)

    sample = pc_intel_gpu_top_sample(card)
    if not sample:
        return result

    engines = sample.get("engines", {})
    if isinstance(engines, dict):
        busy_values: list[float] = []
        for engine in engines.values():
            if not isinstance(engine, dict):
                continue
            try:
                busy = float(engine.get("busy", 0.0))
            except (TypeError, ValueError):
                continue
            if 0.0 <= busy <= 100.0:
                busy_values.append(busy)

        # Represent overall activity as the busiest GPU engine.
        if busy_values:
            result["load"] = round(max(busy_values), 1)

    power = sample.get("power", {})
    if isinstance(power, dict):
        try:
            gpu_w = float(power.get("GPU", 0.0))
            if 0.0 <= gpu_w < 1000.0:
                result["consume"] = round(gpu_w, 1)
        except (TypeError, ValueError):
            pass

    frequency = sample.get("frequency", {})
    if isinstance(frequency, dict):
        try:
            actual = float(frequency.get("actual", 0.0))
        except (TypeError, ValueError):
            actual = 0.0
        if actual > 0:
            result["freq"] = round(actual, 1)

    # Iris Xe uses shared system memory. Do not pretend total RAM is dedicated VRAM.
    result["memUsed"] = 0.0
    result["memTotal"] = 0.0

    # No dedicated Iris fan sensor was identified on the tested notebook.
    result["rpm"] = 0.0

    return result


def pc_nvidia_gpu_info() -> dict[str, float]:
    global PC_GPU_TEMP_MAX
    empty = {
        "temp": 0.0,
        "tempMax": 0.0,
        "load": 0.0,
        "consume": 0.0,
        "rpm": 0.0,
        "memUsed": 0.0,
        "memTotal": 0.0,
        "freq": 0.0,
    }
    exe = shutil.which("nvidia-smi")
    if not exe:
        return empty

    query = (
        "temperature.gpu,utilization.gpu,power.draw,fan.speed,"
        "memory.used,memory.total,clocks.gr"
    )
    try:
        proc = subprocess.run(
            [exe, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1.0,
            check=False,
        )
        line = proc.stdout.splitlines()[0]
        fields = [x.strip() for x in line.split(",")]
        if len(fields) != 7:
            return empty

        def number(value: str) -> float:
            try:
                return float(value)
            except ValueError:
                return 0.0

        temp = round(number(fields[0]), 1)
        PC_GPU_TEMP_MAX = max(PC_GPU_TEMP_MAX, temp)
        return {
            "temp": temp,
            "tempMax": round(PC_GPU_TEMP_MAX, 1),
            "load": round(number(fields[1]), 1),
            "consume": round(number(fields[2]), 1),
            # The vendor reference puts NVML fan percentage into the field named rpm.
            "rpm": round(number(fields[3]), 1),
            "memUsed": round(number(fields[4]), 1),
            "memTotal": round(number(fields[5]), 1),
            "freq": round(number(fields[6]), 1),
        }
    except (OSError, subprocess.SubprocessError, IndexError):
        return empty



def pc_gpu_info() -> dict[str, float]:
    """Choose a real GPU collector; NVIDIA first, then Intel iGPU."""
    if shutil.which("nvidia-smi"):
        info = pc_nvidia_gpu_info()
        # If nvidia-smi exists but produced no meaningful fields, still allow
        # an Intel iGPU to be used on hybrid systems.
        if any(info.get(k, 0.0) != 0.0 for k in ("temp", "load", "consume", "memTotal", "freq")):
            return info

    if pc_intel_drm_card() is not None:
        return pc_intel_gpu_info()

    return pc_empty_gpu_info()


def pc_adjusted_device_time() -> int:
    """Timestamp correction validated on the physical MacroPad.

    Raw Unix epoch was displayed by the firmware as UTC+8.  The tested host is
    UTC-3, so subtracting 11 hours makes the LCD wall clock match the host.
    """
    return int(time.time()) + PC_MONITOR_TIME_SHIFT_SECONDS


def pc_full_status() -> dict[str, Any]:
    """Build the same live payload validated by eez-pc-monitor-test-v4.py."""
    global PC_CPU_TEMP_MAX, PC_TICK
    PC_TICK += 1
    cpu_temp = pc_hwmon_temperature("cpu")
    PC_CPU_TEMP_MAX = max(PC_CPU_TEMP_MAX, cpu_temp)
    board_temp = pc_hwmon_temperature("board") or cpu_temp
    tj_max = 100
    cpu_load = pc_cpu_load_percent()
    return {
        "time": pc_adjusted_device_time(),
        "board": {
            "temp": round(board_temp, 1),
            "rpm": pc_hwmon_fan_value(),
            "tick": PC_TICK,
        },
        "cpu": {
            "temp": round(cpu_temp, 1),
            "tempMax": round(PC_CPU_TEMP_MAX, 1),
            "load": cpu_load,
            "consume": pc_cpu_power_watts(cpu_load),
            "tjMax": tj_max,
            "core1DistanceToTjMax": round(max(0.0, tj_max - cpu_temp), 1),
            "core1Temp": round(cpu_temp, 1),
        },
        "gpu": pc_gpu_info(),
        "storage": pc_storage_info(),
        "memory": pc_memory_info(),
        "network": pc_network_info(),
        "cmd": PC_MONITOR_CMD,
    }


def pause_pc_monitor_for_profile_transition(profile: int) -> None:
    """Temporarily suppress PCS telemetry while firmware animates a profile change."""
    pause_until = time.monotonic() + PC_MONITOR_PROFILE_TRANSITION_DELAY
    with STATE_LOCK:
        STATE["pc_monitor_pause_until"] = max(
            float(STATE.get("pc_monitor_pause_until") or 0.0),
            pause_until,
        )
    LOG.debug(
        "PC monitor paused %.0f ms for profile transition: profile=%s",
        PC_MONITOR_PROFILE_TRANSITION_DELAY * 1000.0,
        profile,
    )



def serial_pcs_status(status: dict[str, Any]) -> bool:
    """Best-effort PC Monitor write; never competes with MSC or foreground CDC."""
    body = json.dumps(status, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > 0xFFFF:
        raise ValueError("PC monitor JSON is too large")

    # Telemetry is disposable. If the physical device is doing anything else,
    # skip this sample instead of queueing behind it.
    if not DEVICE_IO_LOCK.acquire(timeout=0.05):
        return False
    try:
        with STATE_LOCK:
            fd = STATE.get("fd") if STATE.get("connected") else None
            generation = int(STATE.get("connection_generation") or 0)
            if (fd is None or not STATE.get("operational") or STATE.get("storage_busy") or
                    STATE.get("storage_quarantined") or STATE.get("screen_script_active") or
                    time.monotonic() < float(STATE.get("pc_monitor_ready_at") or 0.0) or
                    time.monotonic() < float(STATE.get("pc_monitor_pause_until") or 0.0)):
                return False
            usb_sysfs = str(STATE.get("usb_sysfs") or "")
        # PC Monitor is disposable background traffic.  Keep it more conservative
        # than foreground Configurator commands: pause while the FAT is mounted
        # *or* while any raw block request (for example fsck/udisks/GVfs) is active.
        # This also makes an offline fsck safe while the daemon stays running.
        if usb_sysfs:
            if _usb_block_mounts(usb_sysfs):
                return False
            inflight = _usb_block_inflight_total(usb_sysfs)
            if inflight is None or inflight > 0:
                return False

        write_lock = SERIAL_WRITE_LOCK
        if not write_lock.acquire(timeout=0.20):
            return False
        try:
            with STATE_LOCK:
                if (not STATE.get("connected") or not STATE.get("operational") or
                        STATE.get("fd") != fd or STATE.get("storage_busy") or
                        STATE.get("storage_quarantined") or STATE.get("screen_script_active") or
                        int(STATE.get("connection_generation") or 0) != generation):
                    return False
            _write_fd_nonblocking(fd, b"pcs", timeout=0.75)
            _write_fd_nonblocking(fd, len(body).to_bytes(2, "big"), timeout=0.75)
            _write_fd_nonblocking(fd, body, timeout=0.75)
            with STATE_LOCK:
                STATE["last_cdc_tx_at"] = time.monotonic()
        except TimeoutError as exc:
            with STATE_LOCK:
                STATE["pc_monitor_last_error"] = str(exc)
            LOG.debug("PC monitor sample skipped after serial write timeout: %s", exc)
            return False
        except OSError as exc:
            invalidate_serial(fd, f"PC monitor write failed: {exc}")
            raise RuntimeError("PC monitor lost the MacroPad serial connection") from exc
        finally:
            try:
                write_lock.release()
            except RuntimeError:
                pass

        with STATE_LOCK:
            STATE["pc_monitor_last_sent"] = int(time.time())
            STATE["pc_monitor_last_error"] = None
        LOG.debug("PC monitor: sent %d-byte JSON generation=%s", len(body), generation)
        return True
    finally:
        try:
            DEVICE_IO_LOCK.release()
        except RuntimeError:
            pass



def pc_monitor_loop() -> None:
    """Continuously update the MacroPad PC-monitor screen while connected."""
    global PC_CPU_PREV, PC_CPU_ENERGY_PREV

    LOG.info(
        "PC monitor enabled: interval=%.1fs time-shift=%+.0fh",
        PC_MONITOR_INTERVAL,
        PC_MONITOR_TIME_SHIFT_SECONDS / 3600.0,
    )
    last_generation: Optional[int] = None
    next_sample = time.monotonic()

    while RUNNING:
        with STATE_LOCK:
            connected = bool(STATE.get("connected"))
            generation = int(STATE.get("connection_generation") or 0)
            pause_until = float(STATE.get("pc_monitor_pause_until") or 0.0)
            screen_script_active = bool(STATE.get("screen_script_active"))

        if not connected:
            last_generation = None
            time.sleep(0.25)
            next_sample = time.monotonic()
            continue

        if screen_script_active:
            # A Screen Script owns the LCD contents, but not the serial port.
            # Keep normal EBF/configuration traffic alive and suppress only PCS
            # telemetry so two display producers do not fight over the screen.
            time.sleep(0.25)
            next_sample = time.monotonic()
            continue

        if generation != last_generation:
            # Prime delta-based CPU/energy readings for each new serial session.
            PC_CPU_PREV = None
            PC_CPU_ENERGY_PREV = None
            pc_cpu_load_percent()
            last_generation = generation
            with STATE_LOCK:
                ready_at = float(STATE.get("pc_monitor_ready_at") or 0.0)
            next_sample = max(time.monotonic() + 0.20, ready_at)

        now = time.monotonic()
        if pause_until > now:
            # Resume exactly when the transition guard expires so the first
            # complete PCS refresh is sent immediately after the animation.
            next_sample = pause_until
            time.sleep(min(pause_until - now, 0.25))
            continue

        delay = next_sample - time.monotonic()
        if delay > 0:
            time.sleep(min(delay, 0.25))
            continue

        started = time.monotonic()
        try:
            status = pc_full_status()
            serial_pcs_status(status)
        except RuntimeError as exc:
            with STATE_LOCK:
                STATE["pc_monitor_last_error"] = str(exc)
            LOG.debug("PC monitor runtime error: %s", exc)
        except Exception as exc:
            with STATE_LOCK:
                STATE["pc_monitor_last_error"] = str(exc)
            LOG.warning("PC monitor sample failed: %s", exc)

        # Aim for the requested cadence without trying to catch up in bursts if
        # a sensor command (for example intel_gpu_top) takes longer than expected.
        next_sample = max(started + PC_MONITOR_INTERVAL, time.monotonic())

def activate_profile(profile: int) -> dict[str, Any]:
    """Make the requested profile active on the physical MacroPad.

    Confirmed from the official configurator: c2d_change_profile ('9') is
    constructed with (profile, 0), producing payloads such as '94.0' and
    '95.0'.
    """
    if not (1 <= profile <= MAX_PROFILE_UI):
        raise ValueError("Invalid profile")
    payload = f"9{profile}.0"
    # Start the guard before the profile command so a pending telemetry sample
    # cannot race the transition. Extend it again after TX to guarantee the
    # full delay is measured from the start of the firmware animation.
    pause_pc_monitor_for_profile_transition(profile)
    serial_payload(payload)
    LOG.info("Changed active MacroPad profile: TX %r", payload)
    pause_pc_monitor_for_profile_transition(profile)
    with STATE_LOCK:
        STATE["active_profile"] = profile
    return {"ok": True, "profile": profile, "payload": payload}


def apply_hid_to_device(profile: int, key: int, alias: str) -> None:
    hid_payload = f"a{profile}.{key}"
    alias_payload = f"2{profile}.{key}:{alias}" if alias else f"3{profile}.{key}"
    with DEVICE_IO_LOCK:
        LOG.info("Applying HID/display: TX %r", hid_payload)
        serial_payload(hid_payload)
        time.sleep(0.052)
        LOG.info("Applying HID/display: TX %r", alias_payload)
        serial_payload(alias_payload)
        time.sleep(0.10)




def apply_nonhid_to_device(profile: int, key: int, alias: str) -> None:
    hid_payload = f"b{profile}.{key}"
    alias_payload = f"2{profile}.{key}:{alias}" if alias else f"3{profile}.{key}"
    with DEVICE_IO_LOCK:
        LOG.info("Applying Non-HID/display: TX %r", hid_payload)
        serial_payload(hid_payload)
        time.sleep(0.052)
        LOG.info("Applying Non-HID/display: TX %r", alias_payload)
        serial_payload(alias_payload)
        time.sleep(0.10)



def flush_storage(root: Path) -> None:
    """Flush writes for the MacroPad volume without syncing every filesystem."""
    if shutil.which("sync"):
        proc = subprocess.run(["sync", "-f", str(root)], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, timeout=10, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"sync -f failed: {proc.stdout.strip()}")
    else:
        os.sync()


def current_config_dir() -> Optional[Path]:
    with STATE_LOCK:
        raw = STATE.get("view_config_dir") if STATE.get("connected") else None
        if not raw:
            raw = STATE.get("config_dir")
    return Path(raw) if raw else None


def current_icon_dir() -> Optional[Path]:
    with STATE_LOCK:
        raw = STATE.get("view_icon_dir") if STATE.get("connected") else None
        if not raw:
            raw = STATE.get("icon_dir")
    return Path(raw) if raw else None


def profile_limit() -> int:
    with STATE_LOCK:
        return max(1, min(int(STATE.get("profile_count") or 1), MAX_PROFILE_UI))


def build_hid_files(actions: Any) -> tuple[str, str]:
    """Build confirmed Configurator ACT lines and firmware script lines.

    This intentionally supports only HID forms already confirmed by captures/tests:
    Shortcut (ACT 5), Functional/Fn key (ACT d), HID media key
    (ACT a MK_*), Wait (ACT 7), and Input Text (ACT 4, optionally followed
    by ENTER in the script).
    """
    if not isinstance(actions, list) or not actions:
        raise ValueError("Add at least one HID action")
    if len(actions) > MAX_HID_ACTIONS:
        raise ValueError(f"Maximum of {MAX_HID_ACTIONS} HID actions per key")

    config_lines: list[str] = []
    script_lines: list[str] = []
    for index, item in enumerate(actions, 1):
        if not isinstance(item, dict):
            raise ValueError(f"HID action {index} is invalid")
        kind = str(item.get("type", "")).strip().lower()

        if kind == "shortcut":
            value = " ".join(str(item.get("value", "")).split())
            if not value:
                raise ValueError(f"Shortcut {index} vazio")
            if any(ch in value for ch in "\r\n"):
                raise ValueError("Shortcut cannot contain a newline")
            config_lines.append(f"ACT 5 {value}")
            script_lines.append(value)

        elif kind == "key":
            value = str(item.get("value", "")).strip().upper()
            if value not in HID_KEY_TOKENS:
                raise ValueError(f"Unsupported HID key: {value!r}")
            if value in HID_CONSUMER_TOKENS:
                # Native Configurator capture with HidMode=True:
                #   config: ACT a MK_VOLUP
                #   script: MK_VOLUP
                # The same pattern is used for the other media tokens.
                media_token = HID_MEDIA_SCRIPTS[value]
                config_lines.append(f"ACT a {media_token}")
                script_lines.append(media_token)
            else:
                config_lines.append(f"ACT d {value}")
                script_lines.append(value)

        elif kind == "wait":
            try:
                ms = int(item.get("ms"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Wait {index} is invalid") from exc
            if ms < 0:
                raise ValueError("Wait cannot be negative")
            config_lines.append(f"ACT 7 {ms}")
            script_lines.append(f"DELAY {ms}")

        elif kind == "text":
            text = str(item.get("text", ""))
            if not text:
                raise ValueError(f"Input Text {index} vazio")
            if any(ch in text for ch in "\r\n"):
                raise ValueError("Input Text must be a single line")
            enter = bool(item.get("enter", False))
            suffix = " ENTER" if enter else ""
            config_lines.append(f"ACT 4 {text}{suffix}")
            script_lines.append(f"STRING {text}")
            if enter:
                script_lines.append("ENTER")

        else:
            raise ValueError(f"Unsupported HID action type: {kind!r}")

    # Official captures: config ends with LF, script does not.
    return "\n".join(config_lines) + "\n", "\n".join(script_lines)



def save_hid_key_config(profile: int, key: int, alias: str, actions: Any,
                        icon_path: Optional[str] = None, clear_icon: bool = False) -> dict[str, Any]:
    if not (1 <= profile <= MAX_PROFILE_UI):
        raise ValueError("Invalid profile")
    if not (1 <= key <= 8):
        raise ValueError("Key must be between 1 and 8")
    if any(ch in alias for ch in "\r\n"):
        raise ValueError("Alias cannot contain a newline")
    act_content, script_content = build_hid_files(actions)
    with STATE_LOCK:
        device_id = STATE.get("id")
        config_dir_raw = STATE.get("config_dir")
        connected = bool(STATE.get("connected") and STATE.get("operational"))
    if not device_id or not config_dir_raw:
        raise RuntimeError("No device/cache is selected")
    if connected and HOST_MSC_STABILITY_GUARD:
        raise RuntimeError("HID script editing is disabled by the USB Mass Storage stability guard")

    filename = f"profile_{profile}_key_{key}.txt"
    content = f"Alias={alias}\nHidMode=True\n{act_content}"
    local_path = Path(config_dir_raw) / filename
    local_script = local_script_dir(str(device_id)) / filename
    device_written = False
    icon_result: dict[str, Any] = {}
    serial_applied = False
    serial_error = None

    with OPERATION_LOCK:
        if connected:
            with DEVICE_IO_LOCK:
                with device_storage_session(write=True) as root:
                    source = root / "configs"
                    atomic_write(local_path, content)
                    atomic_write(local_script, script_content)
                    write_device_text_direct(source / filename, content)
                    time.sleep(0.12)
                    device_script = root / "scripts" / filename
                    write_device_text_direct(device_script, script_content)
                    LOG.info("USB HID script written: %s (%s bytes)", device_script, len(script_content.encode("utf-8")))
                    time.sleep(0.08)
                    device_written = True
                    icon_result = copy_key_icon(profile, key, icon_path, clear_icon, root)
                    LOG.info("USB HID config written: %s", source / filename)
                time.sleep(0.30)
                try:
                    apply_hid_to_device(profile, key, alias)
                    serial_applied = True
                except Exception as exc:
                    serial_error = str(exc)
                    LOG.exception("HID files were saved to USB, but serial application failed")
        else:
            atomic_write(local_path, content)
            atomic_write(local_script, script_content)
            icon_result = copy_key_icon(profile, key, icon_path, clear_icon, None)
        _refresh_view_key_from_cache(str(device_id), profile, key)

    with STATE_LOCK:
        STATE["profile_count"] = max(int(STATE.get("profile_count") or 1), profile)
        if STATE.get("profile_count_source") not in {"serial", "device-files"}:
            STATE["profile_count_source"] = "files"
    return {"ok": True, "local_path": str(local_path), "script_path": str(local_script),
            "device_written": device_written, "serial_applied": serial_applied,
            "serial_error": serial_error, **icon_result}




def save_key_config(profile: int, key: int, alias: str, act: str, arg: str,
                    icon_path: Optional[str] = None, clear_icon: bool = False,
                    eezopen_external_script: bool = False) -> dict[str, Any]:
    act = normalize_action_id(act)
    if act not in SUPPORTED_ACTIONS:
        raise ValueError(f"Unsupported ACT: {act}")
    if not (1 <= profile <= MAX_PROFILE_UI):
        raise ValueError("Invalid profile")
    if not (1 <= key <= 8):
        raise ValueError("Key must be between 1 and 8")
    if any(ch in alias for ch in "\r\n") or any(ch in arg for ch in "\r\n"):
        raise ValueError("Alias/argument cannot contain a newline")
    with STATE_LOCK:
        device_id = STATE.get("id")
        config_dir_raw = STATE.get("config_dir")
        connected = bool(STATE.get("connected") and STATE.get("operational"))
    if not device_id or not config_dir_raw:
        raise RuntimeError("No device/cache is selected")

    filename = f"profile_{profile}_key_{key}.txt"
    metadata = "EezOpenExternalScript=True\n" if eezopen_external_script else ""
    content = f"Alias={alias}\nHidMode=False\n{metadata}ACT {act} {arg}\n"
    device_content = device_compatible_config_text(content)
    local_path = Path(config_dir_raw) / filename
    local_script = local_script_dir(str(device_id)) / filename
    device_written = False
    icon_result: dict[str, Any] = {}
    serial_applied = False
    serial_error = None

    with OPERATION_LOCK:
        if connected and not HOST_MSC_STABILITY_GUARD:
            with DEVICE_IO_LOCK:
                with device_storage_session(write=True) as root:
                    source = root / "configs"
                    atomic_write(local_path, content)
                    atomic_write(local_script, "")
                    write_device_text_direct(source / filename, device_content)
                    time.sleep(0.12)
                    device_script = root / "scripts" / filename
                    write_device_text_direct(device_script, "")
                    LOG.info("USB script written: %s (0 bytes)", device_script)
                    time.sleep(0.08)
                    device_written = True
                    icon_result = copy_key_icon(profile, key, icon_path, clear_icon, root)
                    LOG.info("USB config written: %s", source / filename)
                time.sleep(0.30)
                try:
                    apply_nonhid_to_device(profile, key, alias)
                    serial_applied = True
                except Exception as exc:
                    serial_error = str(exc)
                    LOG.exception("File was saved to USB, but serial application failed")
        else:
            atomic_write(local_path, content)
            atomic_write(local_script, "")
            icon_result = copy_key_icon(profile, key, icon_path, clear_icon, None)
            if connected:
                try:
                    apply_nonhid_to_device(profile, key, alias)
                    serial_applied = True
                except Exception as exc:
                    serial_error = str(exc)
        _refresh_view_key_from_cache(str(device_id), profile, key)

    with STATE_LOCK:
        STATE["profile_count"] = max(int(STATE.get("profile_count") or 1), profile)
        if STATE.get("profile_count_source") not in {"serial", "device-files"}:
            STATE["profile_count_source"] = "files"
    return {"ok": True, "local_path": str(local_path), "script_path": str(local_script),
            "device_written": device_written, "serial_applied": serial_applied,
            "serial_error": serial_error, **icon_result}



def save_external_script_key_config(profile: int, key: int, alias: str, script: str, arguments: Any,
                                    icon_path: Optional[str] = None, clear_icon: bool = False) -> dict[str, Any]:
    if not isinstance(arguments, list) or not all(isinstance(x, str) for x in arguments):
        raise ValueError("External Script arguments must be a list of strings")
    if any("\x00" in x or "\n" in x or "\r" in x for x in [script, *arguments]):
        raise ValueError("External Script path/arguments cannot contain NUL or newline characters")
    resolve_external_script_executable(script)
    command = shlex.join([script, *arguments])
    return save_key_config(profile, key, alias, "2", command, icon_path, clear_icon,
                           eezopen_external_script=True)



def delete_key_config(profile: int, key: int) -> dict[str, Any]:
    if not (1 <= profile <= MAX_PROFILE_UI):
        raise ValueError("Invalid profile")
    if not (1 <= key <= 8):
        raise ValueError("Key must be between 1 and 8")
    with STATE_LOCK:
        device_id = str(STATE.get("id") or "")
        connected = bool(STATE.get("connected") and STATE.get("operational"))
    if not device_id:
        raise RuntimeError("No device is selected")
    if not connected:
        raise RuntimeError("Connect the MacroPad before removing a device configuration")

    name_txt = f"profile_{profile}_key_{key}.txt"
    name_png = f"profile_{profile}_key_{key}.png"
    local_paths = [local_config_dir(device_id) / name_txt,
                   local_script_dir(device_id) / name_txt,
                   local_icon_dir(device_id) / name_png]
    removed_local = removed_device = 0
    serial_applied = False
    serial_error = None
    with OPERATION_LOCK, DEVICE_IO_LOCK:
        with device_storage_session(write=True) as root:
            device_paths = [root / "configs" / name_txt, root / "scripts" / name_txt,
                            root / "app_icons" / name_png]
            for path in local_paths:
                if path.is_file():
                    path.unlink(); removed_local += 1
            for path in device_paths:
                if path.is_file():
                    path.unlink(); removed_device += 1
        try:
            serial_payload(f"b{profile}.{key}")
            time.sleep(0.052)
            serial_payload(f"3{profile}.{key}")
            serial_applied = True
        except Exception as exc:
            serial_error = str(exc)
            LOG.exception("Key files were removed, but serial cleanup failed")
        _refresh_view_key_from_cache(device_id, profile, key)
    LOG.info("Configuration removed: profile=%s key=%s local=%s device=%s serial=%s",
             profile, key, removed_local, removed_device, serial_applied)
    return {"ok": True, "profile": profile, "key": key,
            "local_removed": removed_local, "device_removed": removed_device,
            "files_removed": removed_local + removed_device,
            "serial_applied": serial_applied, "serial_error": serial_error}



def _remove_device_extras(local_dir: Optional[Path], device_dir: Path, pattern: str) -> int:
    """Delete only configurable per-key files absent from the EezOpen cache."""
    local_names = {p.name for p in local_dir.glob(pattern)} if local_dir and local_dir.is_dir() else set()
    removed = 0
    if device_dir.is_dir():
        for remote in device_dir.glob(pattern):
            if remote.name not in local_names and remote.is_file():
                remote.unlink()
                removed += 1
    return removed



def push_to_device() -> dict[str, Any]:
    """Explicit EezOpen -> device mirror with MSC and CDC strictly serialized."""
    with STATE_LOCK:
        device_id = str(STATE.get("id") or "")
        config_raw = STATE.get("config_dir")
        icon_raw = STATE.get("icon_dir")
        script_raw = STATE.get("script_dir")
        connected = bool(STATE.get("connected") and STATE.get("operational"))
    if not connected:
        raise RuntimeError("MacroPad is not operational yet")
    if not config_raw or not Path(config_raw).is_dir():
        raise RuntimeError("Persistent local cache not found")

    config_dir = Path(config_raw)
    local_scripts = Path(script_raw) if script_raw else None
    local_icons = Path(icon_raw) if icon_raw else None
    inferred_profiles = infer_profile_count(config_dir, local_icons, local_scripts)
    written = serial_applied = 0
    serial_errors: list[str] = []
    pending_serial: list[tuple[int, int, str, str, bool]] = []

    with OPERATION_LOCK, DEVICE_IO_LOCK:
        with device_storage_session(write=True) as root:
            source = root / "configs"
            dst_scripts = root / "scripts"; dst_scripts.mkdir(exist_ok=True)
            dst_icons = root / "app_icons"; dst_icons.mkdir(exist_ok=True)
            scripts_written = 0
            for local in sorted(config_dir.glob("profile_*_key_*.txt")):
                local_text = local.read_text(encoding="utf-8", errors="replace")
                write_device_text_direct(source / local.name, device_compatible_config_text(local_text))
                cfg = parse_config_file(local)
                is_hid = bool(cfg and cfg.get("hid") is True)
                script_src = local_scripts / local.name if local_scripts else None
                if script_src and script_src.is_file():
                    copy_device_file_direct(script_src, dst_scripts / local.name)
                else:
                    write_device_text_direct(dst_scripts / local.name, "")
                scripts_written += 1; written += 1
                if cfg:
                    act = normalize_action_id(cfg.get("act", ""))
                    if is_hid or act in SUPPORTED_ACTIONS:
                        parts = local.stem.split("_")
                        try:
                            p, k = int(parts[1]), int(parts[3])
                            pending_serial.append((p, k, str(cfg.get("alias", "")), local.name, is_hid))
                        except Exception as exc:
                            serial_errors.append(f"{local.name}: {exc}")
            icons_written = 0
            if local_icons and local_icons.is_dir():
                for local in sorted(local_icons.glob("*.png")):
                    if local.is_file():
                        copy_device_file_direct(local, dst_icons / local.name); icons_written += 1
            configs_removed = _remove_device_extras(config_dir, source, "profile_*_key_*.txt")
            scripts_removed = _remove_device_extras(local_scripts, dst_scripts, "profile_*_key_*.txt")
            icons_removed = _remove_device_extras(local_icons, dst_icons, "profile_*_key_*.png")
            # Refresh while the volume is intentionally mounted.
            if device_id:
                _sync_device_view_from_root(root, device_id)
        time.sleep(0.30)
        # Device metadata/commands are applied only after FAT has been flushed and unmounted.
        profile_result = set_profile_count(inferred_profiles)
        for p, k, alias, filename, is_hid in pending_serial:
            try:
                (apply_hid_to_device if is_hid else apply_nonhid_to_device)(p, k, alias)
                serial_applied += 1
                time.sleep(0.10)
            except Exception as exc:
                serial_errors.append(f"{filename}: {exc}")

    return {"ok": True, "configs_written": written, "scripts_written": scripts_written,
            "icons_written": icons_written, "configs_removed": configs_removed,
            "scripts_removed": scripts_removed, "icons_removed": icons_removed,
            "serial_applied": serial_applied, "serial_errors": serial_errors,
            "profile_count": int(profile_result.get("profile_count") or inferred_profiles),
            "profile_count_confirmed": bool(profile_result.get("confirmed")),
            "storage_root": None, "storage_unmounted": True}



def set_profile_count(count: int) -> dict[str, Any]:
    if not (1 <= count <= MAX_PROFILE_UI):
        raise ValueError(f"Profile count must be between 1 and {MAX_PROFILE_UI}")
    with STATE_LOCK:
        connected = bool(STATE.get("connected") and STATE.get("fd") is not None and STATE.get("operational"))
    if not connected:
        with STATE_LOCK:
            STATE["profile_count"] = count
            STATE["profile_count_source"] = "eezopen-local"
        return {"ok": True, "requested": count, "confirmed": False,
                "profile_count": count, "source": "eezopen-local",
                "note": "MacroPad disconnected; profile count changed only in the local editor."}
    with DEVICE_IO_LOCK:
        payload = f"m{count}"
        LOG.info("Changing MacroPad profile count: TX %r", payload)
        serial_payload(payload)
        time.sleep(0.10)
        result = query_profile_count()
    confirmed_count = int(result.get("profile_count") or 0)
    if confirmed_count != count:
        raise RuntimeError(f"MacroPad confirmed {confirmed_count} profiles after requesting {count}")
    return {"ok": True, "requested": count, "confirmed": True,
            "profile_count": confirmed_count, "source": result.get("source", "serial")}



def query_profile_count() -> dict[str, Any]:
    """Query confirmed command l and wait for the runtime reader to receive n=<count>."""
    with PROFILE_COND:
        if not STATE.get("connected") or STATE.get("fd") is None:
            return {"ok": True, "profile_count": profile_limit(),
                    "source": str(STATE.get("profile_count_source") or "cache")}
        generation = int(STATE.get("profile_generation") or 0)

    serial_payload("l")
    deadline = time.monotonic() + 2.0
    with PROFILE_COND:
        while int(STATE.get("profile_generation") or 0) == generation:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            PROFILE_COND.wait(timeout=remaining)
        if int(STATE.get("profile_generation") or 0) == generation:
            raise RuntimeError("MacroPad did not answer the profile-count query (l -> n=N)")
        return {"ok": True,
                "profile_count": int(STATE.get("profile_count") or 1),
                "source": str(STATE.get("profile_count_source") or "serial")}


# EezBotFun Configurator 1.8.3 exposes five RGB modes in this order.
# Live hardware testing also confirms j0 is constant-on (the previous EezOpen
# mapping incorrectly labelled j0 as Breath).
RGB_MODES = [
    "Always On",
    "When Pressing The Key",
    "Breath",
    "Flowing",
    "Always Off",
]
RGB_SETTINGS_SCHEMA = 2
# v0.5.0-v0.5.3 exposed an incorrect eight-item list.  Migrate the user's
# *intended label* to the corrected device index rather than blindly reusing
# the old numeric value.  Legacy-only effects are mapped conservatively to
# Always On because their current-device indices are not confirmed.
LEGACY_RGB_MODE_MAP = {0: 2, 1: 0, 2: 0, 3: 3, 4: 1, 5: 0, 6: 0, 7: 4}
DEFAULT_RGB = {"mode": 0, "r": 0, "g": 160, "b": 255, "a": 255}


def settings_path() -> Path:
    return data_root() / "settings.json"


def load_settings() -> dict[str, Any]:
    path = settings_path()
    with SETTINGS_LOCK:
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except Exception as exc:
            LOG.warning("Could not read %s: %s", path, exc)
            raw = {}
    return raw if isinstance(raw, dict) else {}


def save_settings(settings: dict[str, Any]) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with SETTINGS_LOCK:
        with tmp.open("w", encoding="utf-8") as fp:
            json.dump(settings, fp, ensure_ascii=False, indent=2, sort_keys=True)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)


def rgb_settings() -> dict[str, int]:
    raw = load_settings().get("rgb", {})
    out = dict(DEFAULT_RGB)
    if not isinstance(raw, dict):
        return out

    for key in ("r", "g", "b", "a"):
        try:
            value = int(raw[key])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= value <= 255:
            out[key] = value

    try:
        saved_mode = int(raw.get("mode", DEFAULT_RGB["mode"]))
    except (TypeError, ValueError):
        saved_mode = DEFAULT_RGB["mode"]
    try:
        schema = int(raw.get("schema", 1))
    except (TypeError, ValueError):
        schema = 1

    if schema < RGB_SETTINGS_SCHEMA:
        out["mode"] = LEGACY_RGB_MODE_MAP.get(saved_mode, DEFAULT_RGB["mode"])
    elif 0 <= saved_mode < len(RGB_MODES):
        out["mode"] = saved_mode
    return out


def persist_rgb_settings(r: int, g: int, b: int, a: int, mode: int) -> None:
    settings = load_settings()
    settings["rgb"] = {
        "schema": RGB_SETTINGS_SCHEMA,
        "mode": mode, "r": r, "g": g, "b": b, "a": a,
    }
    save_settings(settings)



def set_rgb(r: int, g: int, b: int, a: int, mode: int) -> dict[str, Any]:
    values = (r, g, b, a)
    if any(v < 0 or v > 255 for v in values):
        raise ValueError("RGBA values must be between 0 and 255")
    if not (0 <= mode < len(RGB_MODES)):
        raise ValueError("Invalid RGB mode")
    mode_payload = f"j{mode}"
    color_payload = f"1{r:02X}{g:02X}{b:02X}{a}"
    with DEVICE_IO_LOCK:
        LOG.info("RGB: TX mode %r", mode_payload)
        serial_payload(mode_payload)
        time.sleep(0.25)
        LOG.info("RGB: TX color %r", color_payload)
        serial_payload(color_payload)
    persist_rgb_settings(r, g, b, a, mode)
    return {"ok": True, "r": r, "g": g, "b": b, "a": a,
            "mode": mode, "mode_name": RGB_MODES[mode]}



def _screen_script_entry(entry: dict[str, Any], *, require_file: bool = False) -> dict[str, str]:
    if not isinstance(entry, dict):
        raise ValueError("Invalid Screen Script entry")
    raw_path = str(entry.get("path") or "").strip()
    if not raw_path:
        raise ValueError("Screen Script path is required")
    path = Path(raw_path).expanduser()
    if require_file:
        path = path.resolve(strict=True)
        if not path.is_file():
            raise FileNotFoundError(f"Screen Script not found: {path}")
    else:
        path = path.resolve(strict=False)
    name = str(entry.get("name") or path.stem or path.name).strip()
    if not name:
        name = path.name
    args = str(entry.get("args") or "").strip()
    # Validate quoting now. Execution still uses shell=False.
    shlex.split(args)
    return {"name": name, "path": str(path), "args": args}


def screen_script_settings() -> tuple[list[dict[str, str]], int]:
    """Load the persisted Screen Script list, migrating the legacy single path."""
    settings = load_settings()
    scripts: list[dict[str, str]] = []
    raw_scripts = settings.get("screen_scripts")
    if isinstance(raw_scripts, list):
        for raw in raw_scripts[:64]:
            try:
                scripts.append(_screen_script_entry(raw, require_file=False))
            except (ValueError, OSError):
                continue

    if not scripts:
        legacy = settings.get("screen_script", {})
        if isinstance(legacy, dict) and str(legacy.get("path") or "").strip():
            try:
                scripts.append(_screen_script_entry({
                    "name": Path(str(legacy["path"])).stem,
                    "path": str(legacy["path"]),
                    "args": "",
                }, require_file=False))
            except (ValueError, OSError):
                pass

    try:
        selected = int(settings.get("screen_script_selected", 0))
    except (TypeError, ValueError):
        selected = 0
    if not scripts:
        selected = 0
    else:
        selected = max(0, min(selected, len(scripts) - 1))
    return scripts, selected


def persist_screen_scripts(entries: list[dict[str, Any]], selected: int = 0) -> dict[str, Any]:
    if not isinstance(entries, list):
        raise ValueError("Screen Scripts must be a list")
    if len(entries) > 64:
        raise ValueError("At most 64 Screen Scripts may be configured")
    scripts = [_screen_script_entry(entry, require_file=True) for entry in entries]
    if scripts:
        selected = max(0, min(int(selected), len(scripts) - 1))
    else:
        selected = 0

    with STATE_LOCK:
        if STATE.get("screen_script_active"):
            raise RuntimeError("Stop the active Screen Script before editing the Screen Script list")

    settings = load_settings()
    settings["screen_scripts"] = scripts
    settings["screen_script_selected"] = selected
    # Keep the old key for downgrade/backward compatibility.
    settings["screen_script"] = {"path": scripts[selected]["path"] if scripts else ""}
    save_settings(settings)
    with STATE_LOCK:
        STATE["screen_script_name"] = scripts[selected]["name"] if scripts else None
        STATE["screen_script_path"] = scripts[selected]["path"] if scripts else None
        STATE["screen_script_args"] = scripts[selected]["args"] if scripts else ""
    return {"ok": True, "scripts": scripts, "selected": selected}


def set_screen_script_selected(selected: int) -> dict[str, Any]:
    """Persist only which saved Screen Script is selected.

    Selection changes do not modify the script list itself, so the Configurator
    does not need to resend/revalidate/rewrite every entry just to switch the
    current item in the DropDown.
    """
    scripts, _current = screen_script_settings()
    if scripts:
        selected = max(0, min(int(selected), len(scripts) - 1))
    else:
        selected = 0

    with STATE_LOCK:
        if STATE.get("screen_script_active"):
            raise RuntimeError("Stop the active Screen Script before selecting another Screen Script")

    settings = load_settings()
    settings["screen_script_selected"] = selected
    # Keep the legacy single-path key coherent for downgrade compatibility.
    settings["screen_script"] = {"path": scripts[selected]["path"] if scripts else ""}
    save_settings(settings)

    with STATE_LOCK:
        STATE["screen_script_name"] = scripts[selected]["name"] if scripts else None
        STATE["screen_script_path"] = scripts[selected]["path"] if scripts else None
        STATE["screen_script_args"] = scripts[selected]["args"] if scripts else ""

    return {"ok": True, "selected": selected}


def configured_screen_script_path() -> str:
    scripts, selected = screen_script_settings()
    return scripts[selected]["path"] if scripts else ""


def set_screen_script_path(script_path: str) -> dict[str, Any]:
    """Legacy single-script API; preserves existing callers."""
    raw = str(script_path or "").strip()
    if not raw:
        return persist_screen_scripts([], 0)
    path = Path(raw).expanduser().resolve(strict=True)
    scripts, selected = screen_script_settings()
    entry = {"name": path.stem, "path": str(path), "args": ""}
    if scripts:
        scripts[selected] = entry
    else:
        scripts = [entry]
        selected = 0
    result = persist_screen_scripts(scripts, selected)
    return {"ok": True, "script": str(path), **result}


def _screen_script_argv(path: Path, arguments: str = "") -> list[str]:
    # Python source files do not need the executable bit; other script/binary
    # types are executed directly, without a shell.
    if path.suffix.lower() == ".py":
        argv = [sys.executable, str(path)]
    else:
        if not os.access(path, os.X_OK):
            raise PermissionError(f"Screen Script is not executable: {path}")
        argv = [str(path)]
    argv.extend(shlex.split(str(arguments or "")))
    return argv


def _send_cus_stop_to_tty(dev: str) -> None:
    """Best-effort CUS stop through the same physical-device I/O gate."""
    payload = json.dumps({"cmd": "stop"}, separators=(",", ":")).encode("utf-8")
    packet = b"cus" + len(payload).to_bytes(2, "big") + payload
    with DEVICE_IO_LOCK:
        with STATE_LOCK:
            fd = STATE.get("fd") if STATE.get("connected") else None
            generation = int(STATE.get("connection_generation") or 0)
            if STATE.get("storage_quarantined"):
                LOG.warning("Screen Script cleanup skipped CUS stop: MSC is quarantined")
                return
        if fd is None:
            LOG.warning("Screen Script cleanup skipped CUS stop: MacroPad is disconnected")
            return
        write_lock = SERIAL_WRITE_LOCK
        if not write_lock.acquire(timeout=1.0):
            LOG.warning("Screen Script cleanup could not acquire serial write lock")
            return
        try:
            with STATE_LOCK:
                if (not STATE.get("connected") or STATE.get("fd") != fd or
                        int(STATE.get("connection_generation") or 0) != generation):
                    return
            _write_fd_nonblocking(fd, packet, timeout=1.5)
            with STATE_LOCK:
                STATE["last_cdc_tx_at"] = time.monotonic()
            LOG.info("Screen Script cleanup: CUS stop sent via daemon serial fd (%s)", dev or "unknown tty")
        except Exception as exc:
            LOG.warning("Screen Script cleanup could not send CUS stop: %s", exc)
        finally:
            try:
                write_lock.release()
            except RuntimeError:
                pass


def _clear_screen_script_state(returncode: Optional[int], error: Optional[str] = None) -> None:
    global SCREEN_SCRIPT_PROCESS
    SCREEN_SCRIPT_PROCESS = None
    scripts, selected = screen_script_settings()
    with STATE_LOCK:
        STATE["screen_script_active"] = False
        STATE["screen_script_pid"] = None
        STATE["screen_script_name"] = scripts[selected]["name"] if scripts else None
        STATE["screen_script_path"] = scripts[selected]["path"] if scripts else None
        STATE["screen_script_args"] = scripts[selected]["args"] if scripts else ""
        STATE["screen_script_last_error"] = error if error else (
            None if returncode in (None, 0, -signal.SIGTERM) else f"Screen Script exited with code {returncode}"
        )



def start_screen_script(script_path: str, arguments: str = "", name: str = "") -> dict[str, Any]:
    """Start one managed Screen Script as the exclusive CDC writer.

    The daemon intentionally keeps its descriptor open only so it can continue
    receiving key events for host-side actions. While the script is active, all
    daemon CDC transmissions (including PC Monitor) and all MSC operations are
    blocked. This preserves existing direct-PySerial Screen Scripts without two
    independent writers fighting over the firmware.
    """
    global SCREEN_SCRIPT_PROCESS
    path = Path(str(script_path or "").strip()).expanduser().resolve(strict=True)
    if not path.is_file():
        raise FileNotFoundError(f"Screen Script not found: {path}")
    arguments = str(arguments or "").strip()
    argv = _screen_script_argv(path, arguments)
    display_name = str(name or path.stem or path.name).strip()

    with SCREEN_SCRIPT_LOCK, DEVICE_IO_LOCK:
        if SCREEN_SCRIPT_PROCESS is not None and SCREEN_SCRIPT_PROCESS.poll() is None:
            raise RuntimeError("A Screen Script is already running")
        with STATE_LOCK:
            if STATE.get("screen_script_active"):
                raise RuntimeError("A Screen Script is already starting or running")
            if not STATE.get("connected") or not STATE.get("operational") or STATE.get("fd") is None:
                raise RuntimeError("Wait for the MacroPad to become operational before starting a Screen Script")
            if STATE.get("storage_busy"):
                raise RuntimeError("Wait for the MacroPad storage operation to finish")
            usb_sysfs = str(STATE.get("usb_sysfs") or "")
            tty = str(STATE.get("dev") or "")
            device_id = str(STATE.get("id") or "")
        # Do not start an external CDC writer while the FAT volume is mounted.
        if usb_sysfs and _usb_block_mounts(usb_sysfs):
            if not _unmount_usb_mounts_sysfs(usb_sysfs, only_automounts=False):
                raise RuntimeError("Could not unmount MacroPad storage before starting Screen Script")
            time.sleep(MSC_POST_UNMOUNT_DELAY)
        if usb_sysfs and not _wait_for_usb_block_idle(usb_sysfs, timeout=2.0, stable=0.20):
            raise RuntimeError("MacroPad Mass Storage still has active I/O; Screen Script was not started")
        with STATE_LOCK:
            STATE["screen_script_active"] = True
            STATE["screen_script_name"] = display_name
            STATE["screen_script_path"] = str(path)
            STATE["screen_script_args"] = arguments
            STATE["screen_script_pid"] = None
            STATE["screen_script_tty"] = tty
            STATE["screen_script_last_error"] = None

        env = os.environ.copy()
        env.update({"EEZOPEN_TTY": tty, "EEZOPEN_DEVICE_ID": device_id,
                    "EEZOPEN_SCREEN_SCRIPT": "1", "EEZOPEN_SOCKET": str(socket_path())})
        log_path = data_root() / "screen-script.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with log_path.open("ab", buffering=0) as log_fp:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log_fp,
                                        stderr=subprocess.STDOUT, shell=False,
                                        start_new_session=True, env=env)
        except Exception as exc:
            _clear_screen_script_state(None, str(exc))
            raise
        SCREEN_SCRIPT_PROCESS = proc
        with STATE_LOCK:
            STATE["screen_script_pid"] = proc.pid
        LOG.info("Screen Script started: pid=%s name=%s tty=%s; external script is exclusive CDC writer; daemon remains read-only",
                 proc.pid, display_name, tty)
        return {"ok": True, "active": True, "name": display_name, "script": str(path),
                "args": arguments, "pid": proc.pid, "tty": tty}



def stop_screen_script() -> dict[str, Any]:
    """Stop the managed Screen Script and leave CUS display mode."""
    global SCREEN_SCRIPT_PROCESS
    with SCREEN_SCRIPT_LOCK:
        proc = SCREEN_SCRIPT_PROCESS
        with STATE_LOCK:
            active = bool(STATE.get("screen_script_active"))
            tty = str(STATE.get("screen_script_tty") or "")
            script = str(STATE.get("screen_script_path") or "")
        if proc is None and not active:
            return {"ok": True, "active": False, "script": script, "already_stopped": True}

        returncode: Optional[int] = proc.poll() if proc is not None else None
        if proc is not None and returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                returncode = proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                returncode = proc.wait(timeout=2.0)

        with DEVICE_IO_LOCK:
            with STATE_LOCK:
                daemon_fd = STATE.get("fd") if STATE.get("connected") else None
            if daemon_fd is not None:
                try:
                    configure_serial(daemon_fd)
                except (OSError, termios.error) as exc:
                    LOG.warning("Could not restore daemon serial settings after Screen Script: %s", exc)
            _send_cus_stop_to_tty(tty)
        _clear_screen_script_state(returncode)
        with STATE_LOCK:
            STATE["pc_monitor_ready_at"] = time.monotonic() + PC_MONITOR_STARTUP_DELAY
        LOG.info("Screen Script stopped: path=%s returncode=%s; daemon serial stayed active", script, returncode)
        return {"ok": True, "active": False, "script": script, "returncode": returncode}



def reap_screen_script_if_exited() -> bool:
    """Reap the managed Screen Script. Returns whether it is still active."""
    global SCREEN_SCRIPT_PROCESS
    with SCREEN_SCRIPT_LOCK:
        with STATE_LOCK:
            active = bool(STATE.get("screen_script_active"))
            tty = str(STATE.get("screen_script_tty") or "")
            script = str(STATE.get("screen_script_path") or "")
        if not active:
            return False
        proc = SCREEN_SCRIPT_PROCESS
        if proc is None:
            return True
        returncode = proc.poll()
        if returncode is None:
            return True
        with DEVICE_IO_LOCK:
            with STATE_LOCK:
                daemon_fd = STATE.get("fd") if STATE.get("connected") else None
            if daemon_fd is not None:
                try:
                    configure_serial(daemon_fd)
                except (OSError, termios.error) as exc:
                    LOG.warning("Could not restore daemon serial settings after Screen Script exit: %s", exc)
            _send_cus_stop_to_tty(tty)
        error = None if returncode == 0 else f"Screen Script exited with code {returncode}"
        _clear_screen_script_state(returncode, error)
        with STATE_LOCK:
            STATE["pc_monitor_ready_at"] = time.monotonic() + PC_MONITOR_STARTUP_DELAY
        LOG.info("Screen Script exited: path=%s returncode=%s", script, returncode)
        return False


def screen_script_watch_loop() -> None:
    """Reap a Screen Script that exits on its own without affecting CDC runtime."""
    while RUNNING:
        try:
            reap_screen_script_if_exited()
        except Exception as exc:
            LOG.warning("Screen Script watcher failed: %s", exc)
        time.sleep(0.25)



def remove_background() -> dict[str, Any]:
    with STATE_LOCK:
        connected = bool(STATE.get("connected") and STATE.get("operational"))
        guarded = bool(STATE.get("storage_guarded"))
    if not connected:
        raise RuntimeError("MacroPad is not operational yet")
    if guarded:
        raise RuntimeError("MacroPad USB Mass Storage is unavailable")
    removed: list[str] = []
    with OPERATION_LOCK:
        with device_storage_session(write=True) as root:
            app_icons = root / "app_icons"
            for filename in sorted(set(BACKGROUND_FILENAMES.values())):
                target = app_icons / filename
                try:
                    target.unlink()
                except FileNotFoundError:
                    continue
                removed.append(filename)
    LOG.info("Background files removed from MacroPad: %s", removed or "none present")
    return {"ok": True, "removed": removed, "storage_root": None, "storage_unmounted": True}




def install_background(bin_path: str, theme: str) -> dict[str, Any]:
    theme = str(theme or "").lower().strip()
    if theme not in BACKGROUND_FILENAMES:
        raise ValueError("Background theme must be dark or light")
    source = Path(bin_path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"Converted background file not found: {source}")
    size = source.stat().st_size
    if size != BACKGROUND_BIN_SIZE:
        raise ValueError(f"Unexpected background BIN size: {size} bytes; expected {BACKGROUND_BIN_SIZE}")
    with STATE_LOCK:
        connected = bool(STATE.get("connected") and STATE.get("operational"))
        guarded = bool(STATE.get("storage_guarded"))
    if not connected:
        raise RuntimeError("MacroPad is not operational yet")
    if guarded:
        raise RuntimeError("MacroPad USB Mass Storage is unavailable")
    filename = BACKGROUND_FILENAMES[theme]
    with OPERATION_LOCK:
        with device_storage_session(write=True) as root:
            destination = root / "app_icons" / filename
            copy_device_file_direct(source, destination)
    LOG.info("Background installed: theme=%s source=%s filename=%s", theme, source, filename)
    return {"ok": True, "theme": theme, "filename": filename, "source": str(source),
            "destination": filename, "size": size,
            "light_filename_confirmed": False if theme == "light" else True,
            "storage_unmounted": True}




def set_device_theme(theme: str) -> dict[str, Any]:
    theme = theme.lower().strip()
    if theme not in {"dark", "light"}:
        raise ValueError("Theme must be dark or light")
    with DEVICE_IO_LOCK:
        serial_payload("h0" if theme == "dark" else "h1")
        time.sleep(0.25)
        with STATE_LOCK:
            fd = STATE.get("fd") if STATE.get("connected") else None
        serial_payload("c")
        invalidate_serial(fd, "reboot after theme change")
    return {"ok": True, "theme": theme, "reboot_sent": True, "reconnecting": True}



def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _observe_firmware_trigger_fd(fd: int, seconds: float = 3.0) -> None:
    """Keep the fresh 115200 descriptor alive briefly, like the working Bash updater.

    Any RX is diagnostic only.  A disconnect/EIO is expected when the device
    re-enumerates and is not considered a trigger failure.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        timeout = max(0.0, min(0.25, deadline - time.monotonic()))
        try:
            readable, _, _ = select.select([fd], [], [], timeout)
        except (OSError, ValueError):
            return
        if not readable:
            continue
        try:
            data = os.read(fd, 4096)
        except (BlockingIOError, OSError):
            return
        if data:
            LOG.debug("Firmware RX after trigger: %r", data)



def firmware_update(bin_path: str, json_path: str) -> dict[str, Any]:
    bin_src = Path(bin_path).expanduser().resolve()
    json_src = Path(json_path).expanduser().resolve()
    for p in (bin_src, json_src):
        if not p.is_file() or p.is_symlink():
            raise ValueError(f"Invalid file: {p}")
    try:
        metadata = json.loads(json_src.read_text(encoding="utf-8"))
        info = metadata["EezBotFunFirmwareInfo"]
    except Exception as exc:
        raise ValueError(f"Invalid firmware JSON: {exc}") from exc
    if info.get("device") != "mc-08":
        raise ValueError("JSON is not for the mc-08 device")
    version = info.get("version"); expected_size = info.get("size")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValueError("Invalid version in JSON")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size <= 0:
        raise ValueError("Invalid size in JSON")
    actual_size = bin_src.stat().st_size
    if actual_size != expected_size:
        raise ValueError(f"BIN size mismatch: JSON={expected_size}, BIN={actual_size}")

    with STATE_LOCK:
        connected = bool(STATE.get("connected") and STATE.get("operational"))
        fd = STATE.get("fd") if connected else None
        dev = STATE.get("dev")
        normal_vid = STATE.get("usb_vid"); normal_pid = STATE.get("usb_pid")
    if not connected or fd is None or not dev:
        raise RuntimeError("MacroPad is not operational yet")

    with OPERATION_LOCK, DEVICE_IO_LOCK:
        with device_storage_session(write=True) as root:
            dst_bin = root / "mc-08.bin"; dst_json = root / "mc-08.json"
            copy_file_fsync(bin_src, dst_bin); copy_file_fsync(json_src, dst_json)
            if sha256(bin_src) != sha256(dst_bin):
                raise RuntimeError("Verification failed: mc-08.bin on the MacroPad differs from the source")
            if sha256(json_src) != sha256(dst_json):
                raise RuntimeError("Verification failed: mc-08.json on the MacroPad differs from the source")
        # FAT is now clean and unmounted. Arm reconnect guard before closing CDC.
        with STATE_LOCK:
            STATE["firmware_waiting_for_normal"] = True
            STATE["firmware_expected_vid"] = normal_vid
            STATE["firmware_expected_pid"] = normal_pid
            STATE["firmware_seen_transition"] = False
            STATE["firmware_wait_started"] = time.monotonic()
            STATE["storage_error"] = "Firmware update in progress; waiting for normal mode to return"
        invalidate_serial(fd, "preparing firmware update")
        time.sleep(0.05)
        update_fd: Optional[int] = None
        try:
            update_fd = os.open(str(dev), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            configure_serial_baud(update_fd, termios.B115200)
            LOG.info("Firmware TX: fresh descriptor at %s, 115200 baud", dev)
            _write_fd_nonblocking(update_fd, frame("n"), timeout=1.5)
            LOG.info("Firmware TX HEX: 65 62 66 01 6e")
            _observe_firmware_trigger_fd(update_fd, 3.0)
        except (OSError, termios.error, TimeoutError) as exc:
            with STATE_LOCK:
                STATE["firmware_waiting_for_normal"] = False
                STATE["firmware_expected_vid"] = None; STATE["firmware_expected_pid"] = None
                STATE["firmware_seen_transition"] = False; STATE["firmware_wait_started"] = None
            raise RuntimeError(f"Failed to send firmware trigger at 115200: {exc}") from exc
        finally:
            if update_fd is not None:
                try: os.close(update_fd)
                except OSError: pass
    return {"ok": True, "device": "mc-08", "version": version, "size": actual_size,
            "usb_root": None, "bin_sha256": sha256(bin_src), "json_sha256": sha256(json_src),
            "trigger_sent": True, "trigger_baud": 115200, "trigger_hex": "65 62 66 01 6e",
            "reconnecting": True, "normal_usb": f"{normal_vid or '?'}:{normal_pid or '?'}",
            "storage_unmounted": True,
            "note": "Firmware files verified and FAT unmounted before frame n; EezOpen waits for normal USB mode to return."}



def handle_message(msg: str, config_dir: Path) -> None:
    if msg.startswith("e="):
        LOG.debug("Device type response: %s", msg[2:])
        return
    if msg.startswith("f="):
        LOG.debug("Device id response: %s", msg[2:])
        return
    count = parse_profile_count_message(msg)
    if count is not None:
        with PROFILE_COND:
            STATE["profile_count"] = count
            STATE["profile_count_source"] = "serial"
            STATE["profile_generation"] = int(STATE.get("profile_generation", 0)) + 1
            PROFILE_COND.notify_all()
        LOG.info("Profile count confirmed by MacroPad: %s", count)
        return
    if not msg.startswith("ebf.k."):
        LOG.debug("Ignored message: %r", msg)
        return
    parts = msg.split(".")
    if len(parts) != 4:
        LOG.warning("Invalid key event: %r", msg)
        return
    profile, key = parts[2], parts[3]
    LOG.info("KEY profile=%s key=%s", profile, key)
    try:
        with STATE_LOCK:
            STATE["active_profile"] = int(profile)
    except (TypeError, ValueError):
        pass
    cfg = read_config(config_dir, profile, key)
    if cfg is not None:
        run_action(cfg, int(profile), int(key))


def state_snapshot() -> dict[str, Any]:
    with STATE_LOCK:
        return {
            "connected": bool(STATE.get("connected")), "device": STATE.get("dev"),
            "type": STATE.get("type"), "id": STATE.get("id"),
            "config_dir": STATE.get("config_dir"), "icon_dir": STATE.get("icon_dir"),
            "script_dir": STATE.get("script_dir"),
            "view_config_dir": STATE.get("view_config_dir"), "view_icon_dir": STATE.get("view_icon_dir"),
            "view_script_dir": STATE.get("view_script_dir"),
            "source_dir": STATE.get("source_dir"), "storage_root": STATE.get("storage_root"), "socket": str(socket_path()),
            "storage_ready": bool(STATE.get("storage_ready")), "storage_error": STATE.get("storage_error"),
            "storage_guarded": bool(STATE.get("storage_guarded")),
            "safe_mode": bool(STATE.get("safe_mode")),
            "usb_vid": STATE.get("usb_vid"), "usb_pid": STATE.get("usb_pid"),
            "authenticated": bool(STATE.get("authenticated")),
            "operational": bool(STATE.get("operational")),
            "startup_phase": STATE.get("startup_phase"),
            "startup_detail": STATE.get("startup_detail"),
            "storage_busy": bool(STATE.get("storage_busy")),
            "storage_quarantined": bool(STATE.get("storage_quarantined")),
            "storage_quarantine_reason": STATE.get("storage_quarantine_reason"),
            "connection_generation": int(STATE.get("connection_generation") or 0),
            "pc_monitor_enabled": bool(STATE.get("pc_monitor_enabled")),
            "pc_monitor_interval": float(STATE.get("pc_monitor_interval") or PC_MONITOR_INTERVAL),
            "pc_monitor_last_sent": STATE.get("pc_monitor_last_sent"),
            "pc_monitor_last_error": STATE.get("pc_monitor_last_error"),
            "screen_script_active": bool(STATE.get("screen_script_active")),
            "screen_script_name": STATE.get("screen_script_name"),
            "screen_script_path": STATE.get("screen_script_path"),
            "screen_script_args": STATE.get("screen_script_args") or "",
            "screen_script_pid": STATE.get("screen_script_pid"),
            "screen_script_tty": STATE.get("screen_script_tty"),
            "screen_script_last_error": STATE.get("screen_script_last_error"),
            "screen_scripts": screen_script_settings()[0],
            "screen_script_selected": screen_script_settings()[1],
            "supported_actions": sorted(SUPPORTED_ACTIONS),
            "profile_count": int(STATE.get("profile_count") or 1),
            "profile_count_source": STATE.get("profile_count_source"),
            "rgb_modes": RGB_MODES, "rgb": rgb_settings(),
        }


def key_record(profile: int, key: int) -> dict[str, Any]:
    cfgdir = current_config_dir()
    icon = per_key_icon_path(current_icon_dir(), profile, key)
    cfg = read_config(cfgdir, profile, key)
    if cfg is not None:
        cfg["icon_path"] = str(icon) if icon else None

        # A refreshed device view contains the compatible ACT 2 representation
        # and intentionally has no EezOpen-only marker. If the persistent HOME
        # cache still identifies the same command as External Script, overlay
        # that local semantic metadata for the GUI only.
        if (normalize_action_id(cfg.get("act", "")) == "2"
                and cfg.get("eezopen_external_script") is not True):
            with STATE_LOCK:
                did = str(STATE.get("id") or "")
            if did:
                local_cfg = read_config(local_config_dir(did), profile, key)
                if (local_cfg
                        and local_cfg.get("eezopen_external_script") is True
                        and normalize_action_id(local_cfg.get("act", "")) == "2"
                        and str(local_cfg.get("arg", "")) == str(cfg.get("arg", ""))):
                    cfg["eezopen_external_script"] = True

        is_external_script = (cfg.get("eezopen_external_script") is True
                              or normalize_action_id(cfg.get("act", "")) == "p")
        if is_external_script:
            try:
                script, arguments = parse_external_script_command(str(cfg.get("arg", "")))
            except ValueError:
                script, arguments = "", []
            # "p" remains a virtual GUI action ID only. It is never required
            # on the physical MacroPad in v1.4.3.
            cfg["act"] = "p"
            cfg["external_script"] = script
            cfg["external_script_args"] = arguments
    return {"key": key, "config": cfg, "icon_path": str(icon) if icon else None}


def list_profile(profile: int) -> list[dict[str, Any]]:
    return [key_record(profile, key) for key in range(1, 9)]


def handle_ipc_request(req: dict[str, Any]) -> dict[str, Any]:
    cmd = req.get("cmd")
    if cmd == "status":
        return {"ok": True, "status": state_snapshot()}
    if cmd == "get_profile":
        profile = int(req.get("profile", 1))
        if not (1 <= profile <= MAX_PROFILE_UI):
            raise ValueError("Invalid profile")
        return {"ok": True, "profile": profile, "keys": list_profile(profile)}
    if cmd == "get_key":
        profile, key = int(req["profile"]), int(req["key"])
        rec = key_record(profile, key)
        return {"ok": True, "profile": profile, "key": key,
                "config": rec["config"], "icon_path": rec["icon_path"]}
    if cmd == "save_key":
        return save_key_config(int(req["profile"]), int(req["key"]), str(req.get("alias", "")),
                               str(req.get("act", "")), str(req.get("arg", "")),
                               str(req.get("icon_path")) if req.get("icon_path") else None,
                               bool(req.get("clear_icon", False)))
    if cmd == "save_hid_key":
        return save_hid_key_config(int(req["profile"]), int(req["key"]), str(req.get("alias", "")),
                                   req.get("actions"),
                                   str(req.get("icon_path")) if req.get("icon_path") else None,
                                   bool(req.get("clear_icon", False)))
    if cmd == "save_external_script_key":
        return save_external_script_key_config(
            int(req["profile"]), int(req["key"]), str(req.get("alias", "")),
            str(req.get("script", "")), req.get("arguments", []),
            str(req.get("icon_path")) if req.get("icon_path") else None,
            bool(req.get("clear_icon", False)),
        )
    if cmd == "delete_key":
        return delete_key_config(int(req["profile"]), int(req["key"]))
    if cmd == "test_action":
        act = normalize_action_id(req.get("act", ""))
        if act not in SUPPORTED_ACTIONS or act == "p":
            raise ValueError(f"Unsupported ACT for generic test: {act}")
        run_action({"act": act, "arg": str(req.get("arg", "")), "alias": "GUI test", "hid": False})
        return {"ok": True}
    if cmd == "test_external_script":
        return test_external_script_command(str(req.get("script", "")), req.get("arguments", []), "GUI test")
    if cmd == "pull_from_device":
        with STATE_LOCK:
            did = STATE.get("id")
        if not did:
            raise RuntimeError("No device is selected")
        result = sync_from_device(str(did))
        # A refresh updates the physical-device view and its confirmed profile count,
        # but intentionally leaves the persistent home cache untouched.
        profile_result = query_profile_count()
        result["profile_count"] = profile_result["profile_count"]
        result["profile_count_source"] = profile_result["source"]
        return result
    if cmd == "import_from_device":
        with STATE_LOCK:
            did = STATE.get("id")
        if not did:
            raise RuntimeError("No device is selected")
        result = import_from_device(str(did))
        profile_result = query_profile_count()
        result["profile_count"] = profile_result["profile_count"]
        result["profile_count_source"] = profile_result["source"]
        return result
    if cmd == "push_to_device":
        return push_to_device()
    if cmd == "query_profile_count":
        return query_profile_count()
    if cmd == "set_profile_count":
        return set_profile_count(int(req["count"]))
    if cmd == "activate_profile":
        return activate_profile(int(req["profile"]))
    if cmd == "set_rgb":
        return set_rgb(int(req["r"]), int(req["g"]), int(req["b"]), int(req.get("a", 255)), int(req["mode"]))
    if cmd == "install_background":
        return install_background(str(req["bin_path"]), str(req["theme"]))
    if cmd == "remove_background":
        return remove_background()
    if cmd == "set_screen_script_path":
        return set_screen_script_path(str(req.get("script", "")))
    if cmd == "get_screen_scripts":
        scripts, selected = screen_script_settings()
        return {"ok": True, "scripts": scripts, "selected": selected}
    if cmd == "set_screen_scripts":
        return persist_screen_scripts(req.get("scripts", []), int(req.get("selected", 0)))
    if cmd == "set_screen_script_selected":
        return set_screen_script_selected(int(req.get("selected", 0)))
    if cmd == "start_screen_script":
        return start_screen_script(
            str(req.get("script", "")),
            str(req.get("args", "")),
            str(req.get("name", "")),
        )
    if cmd == "stop_screen_script":
        return stop_screen_script()
    if cmd == "set_theme":
        return set_device_theme(str(req["theme"]))
    if cmd == "firmware_update":
        return firmware_update(str(req["bin_path"]), str(req["json_path"]))
    raise ValueError(f"Comando IPC desconhecido: {cmd!r}")


def ipc_client_handler(conn: socket.socket) -> None:
    try:
        conn.settimeout(15.0)
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_IPC_REQUEST:
                raise ValueError("IPC request is too large")
        raw = buf.split(b"\n", 1)[0]
        if not raw:
            return
        req = json.loads(raw.decode("utf-8"))
        if not isinstance(req, dict):
            raise ValueError("Request must be a JSON object")
        try:
            resp = handle_ipc_request(req)
        except Exception as exc:
            LOG.warning("IPC %r failed: %s", req.get("cmd"), exc)
            resp = {"ok": False, "error": str(exc)}
        try:
            conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError):
            # The GTK client may have timed out or closed while a hardware
            # operation was still completing. This is not a daemon fault and
            # must not produce a traceback or affect the device session.
            LOG.debug("IPC client disconnected before reply: cmd=%r", req.get("cmd"))
            return
    except Exception:
        LOG.exception("IPC client error")
    finally:
        try:
            conn.close()
        except OSError:
            pass


def ipc_server() -> None:
    path = socket_path()
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(8)
    server.settimeout(1.0)
    LOG.info("IPC available at %s", path)
    try:
        while RUNNING:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=ipc_client_handler, args=(conn,), daemon=True).start()
    finally:
        server.close()
        path.unlink(missing_ok=True)



def set_connected_state(device, config_dir: Path, source_dir: Optional[Path]) -> None:
    serial_count = device.get("profile_count")
    safe_mode = is_safe_mode_usb(device.get("usb_vid"), device.get("usb_pid"))
    icons = local_icon_dir(device["id"]); scripts = local_script_dir(device["id"])
    with STATE_LOCK:
        view_config_raw = STATE.get("view_config_dir"); view_icon_raw = STATE.get("view_icon_dir")
    infer_configs = Path(view_config_raw) if view_config_raw else config_dir
    infer_icons = Path(view_icon_raw) if view_icon_raw else icons
    inferred = infer_profile_count(infer_configs, infer_icons, scripts)
    effective = int(serial_count) if serial_count is not None else inferred
    reset_serial_write_lock("new serial session")
    with STATE_LOCK:
        generation = int(STATE.get("connection_generation") or 0) + 1
        STATE.update({
            "connected": True, "operational": True, "startup_phase": "operational",
            "startup_detail": "USB settled; CDC probe/auth complete",
            "fd": device["fd"], "dev": device["dev"], "type": device["type"], "id": device["id"],
            "config_dir": str(config_dir), "icon_dir": str(icons), "script_dir": str(scripts),
            "source_dir": str(source_dir) if source_dir else None,
            "usb_sysfs": device.get("usb_sysfs"), "usb_vid": device.get("usb_vid"),
            "usb_pid": device.get("usb_pid"), "usb_serial": device.get("usb_serial"),
            "safe_mode": safe_mode, "storage_guarded": HOST_MSC_STABILITY_GUARD or safe_mode,
            "authenticated": bool(device.get("authenticated")), "connection_generation": generation,
            "profile_count": effective, "profile_count_source": "serial" if serial_count is not None else "files",
            "pc_monitor_last_error": None, "pc_monitor_ready_at": time.monotonic() + PC_MONITOR_STARTUP_DELAY,
            "storage_busy": False, "storage_quarantined": False,
            "storage_quarantine_reason": None, "last_cdc_tx_at": time.monotonic(),
        })
        if serial_count:
            STATE["profile_generation"] = int(STATE.get("profile_generation", 0)) + 1
    LOG.info("Serial session operational: generation=%s dev=%s; PC Monitor held for %.1fs",
             generation, device.get("dev"), PC_MONITOR_STARTUP_DELAY)
    finish_firmware_reconnect_guard()




def prime_device_identity(device: dict[str, Any]) -> None:
    safe_mode = is_safe_mode_usb(device.get("usb_vid"), device.get("usb_pid"))
    with STATE_LOCK:
        STATE.update({
            "dev": device.get("dev"), "type": device.get("type"), "id": device.get("id"),
            "usb_sysfs": device.get("usb_sysfs"), "usb_vid": device.get("usb_vid"),
            "usb_pid": device.get("usb_pid"), "usb_serial": device.get("usb_serial"),
            "safe_mode": safe_mode, "storage_guarded": HOST_MSC_STABILITY_GUARD or safe_mode,
            "authenticated": bool(device.get("authenticated")), "operational": False,
            "startup_phase": "serial-detected", "startup_detail": "CDC identity confirmed; activating runtime session",
            "storage_ready": False, "storage_busy": False,
            "storage_quarantined": False, "storage_quarantine_reason": None,
            "last_cdc_tx_at": time.monotonic(),
            "storage_error": "Safe Mode: USB Mass Storage is disabled" if safe_mode else None,
            "view_config_dir": None, "view_icon_dir": None, "view_script_dir": None,
        })




def mark_disconnected() -> None:
    reset_serial_write_lock("device disconnected")
    with STATE_LOCK:
        STATE["connected"] = False; STATE["operational"] = False
        STATE["fd"] = None; STATE["dev"] = None; STATE["type"] = None
        STATE["usb_sysfs"] = None; STATE["usb_vid"] = None; STATE["usb_pid"] = None
        STATE["usb_serial"] = None; STATE["safe_mode"] = False
        STATE["storage_guarded"] = HOST_MSC_STABILITY_GUARD
        STATE["authenticated"] = False; STATE["storage_ready"] = False
        STATE["storage_busy"] = False; STATE["storage_error"] = None
        STATE["storage_quarantined"] = False; STATE["storage_quarantine_reason"] = None
        STATE["last_cdc_tx_at"] = 0.0
        STATE["pc_monitor_ready_at"] = 0.0
        STATE["startup_phase"] = "waiting"; STATE["startup_detail"] = None



def run_connected(device) -> None:
    fd, dev, device_id = device["fd"], device["dev"], device["id"]
    prime_device_identity(device)
    safe_mode = is_safe_mode_usb(device.get("usb_vid"), device.get("usb_pid"))
    storage_guarded = HOST_MSC_STABILITY_GUARD or safe_mode
    config_dir = local_config_dir(device_id)

    # Runtime connection is CDC/serial only. Recursive Mass Storage reads are
    # performed only for explicit Configurator IPC requests such as
    # pull_from_device/import_from_device/push_to_device.
    set_connected_state(device, config_dir, None)
    if storage_guarded:
        reason = "Safe Mode" if safe_mode else "Linux MSC stability guard"
        LOG.info("MacroPad runtime storage disabled (%s); using serial/HID + persistent cache", reason)
    else:
        LOG.info("MacroPad connected without automatic Mass Storage scan; using persistent cache")
    if any(config_dir.glob("profile_*_key_*.txt")):
        LOG.info("Using persistent local cache: %s", config_dir)

    buf = b""
    while RUNNING:
        with STATE_LOCK:
            if not STATE.get("connected") or STATE.get("fd") != fd:
                LOG.info("Connection changed; starting MacroPad redetection")
                return
        try:
            readable, _, _ = select.select([fd], [], [], 1.0)
        except (OSError, ValueError):
            LOG.info("Serial disconnected: %s", dev)
            return
        if not readable:
            continue
        try:
            data = os.read(fd, 4096)
        except BlockingIOError:
            continue
        except OSError as exc:
            LOG.info("Read failed / device removed: %s", exc)
            return
        if not data:
            continue
        buf += data
        if len(buf) > MAX_SERIAL_BUFFER and b"\r\n" not in buf:
            LOG.warning("Serial buffer without CRLF exceeded %d bytes; discarding data to prevent memory growth", MAX_SERIAL_BUFFER)
            buf = b""
            continue
        while b"\r\n" in buf:
            raw, buf = buf.split(b"\r\n", 1)
            try:
                msg = raw.decode("ascii")
            except UnicodeDecodeError:
                LOG.warning("Non-ASCII message: %s", raw.hex())
                continue
            handle_message(msg, config_dir)


def main() -> int:
    logging.basicConfig(level=os.environ.get("EEZOPEN_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    LOG.info("EezOpen v%s started", EEZOPEN_VERSION)
    scripts, selected = screen_script_settings()
    with STATE_LOCK:
        STATE["screen_script_name"] = scripts[selected]["name"] if scripts else None
        STATE["screen_script_path"] = scripts[selected]["path"] if scripts else None
        STATE["screen_script_args"] = scripts[selected]["args"] if scripts else ""
    threading.Thread(target=ipc_server, name="eezopen-ipc", daemon=True).start()
    threading.Thread(target=screen_script_watch_loop, name="eezopen-screen-script", daemon=True).start()
    if PC_MONITOR_ENABLED:
        threading.Thread(target=pc_monitor_loop, name="eezopen-pc-monitor", daemon=True).start()
    while RUNNING:
        # Screen Scripts no longer own/suspend the CDC connection. Reap them
        # opportunistically while normal device detection/serial handling continues.
        reap_screen_script_if_exited()
        target = firmware_detection_target()
        if target is False:
            time.sleep(POLL_INTERVAL)
            continue
        device = detect_device(required_usb=target if isinstance(target, tuple) else None)
        if device is None:
            time.sleep(POLL_INTERVAL)
            continue
        try:
            run_connected(device)
        finally:
            mark_disconnected()
            try:
                os.close(device["fd"])
            except OSError:
                pass
        if RUNNING:
            LOG.info("Waiting for MacroPad reconnection...")
            time.sleep(POLL_INTERVAL)
    try:
        stop_screen_script()
    except Exception as exc:
        LOG.warning("Screen Script shutdown cleanup failed: %s", exc)
    LOG.info("EezOpen stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
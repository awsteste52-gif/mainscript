from __future__ import annotations

import importlib.util
import ipaddress
import json
import math
import os
import ctypes
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional
from urllib.parse import quote, urlparse


# ============================================================
# BOOTSTRAP DE DEPENDENCIAS
# ============================================================

def ensure_pip() -> bool:
    try:
        import pip  # noqa: F401
        return True
    except Exception:
        pass
    try:
        import ensurepip
        ensurepip.bootstrap(upgrade=True)
        return True
    except Exception as exc:
        print(f"Nao foi possivel instalar o pip: {exc}")
        return False


def install_deps() -> None:
    deps_map = {
        "customtkinter": "customtkinter",
        "requests": "requests",
        "selenium": "selenium",
        "fastapi": "fastapi",
        "uvicorn": "uvicorn[standard]",
        "pydantic": "pydantic",
        "structlog": "structlog",
        "screeninfo": "screeninfo",
    }
    missing = [package for module, package in deps_map.items() if importlib.util.find_spec(module) is None]
    if not missing:
        return
    if not ensure_pip():
        input("Erro ao preparar pip. ENTER p/ sair...")
        raise SystemExit(1)
    command = [sys.executable, "-m", "pip", "install", "--upgrade", *missing]
    if subprocess.call(command) != 0:
        input("Erro ao instalar dependencias. ENTER p/ sair...")
        raise SystemExit(1)
    os.execl(sys.executable, sys.executable, *sys.argv)


install_deps()

import requests
import structlog
from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from pydantic import BaseModel, Field
from screeninfo import get_monitors
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

try:
    from pywinauto import Desktop
except Exception:
    Desktop = None

try:
    import customtkinter as ctk
except Exception as exc:
    print(f"Falha ao importar customtkinter: {exc}")
    raise


# ============================================================
# LOGGING
# ============================================================

def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),
        logger_factory=structlog.PrintLoggerFactory(),
    )


configure_logging()
logger = structlog.get_logger(__name__)


# ============================================================
# MODELS / EVENTOS
# ============================================================

def model_dump_compat(model: BaseModel, **kwargs) -> dict:
    if hasattr(model, "model_dump"):
        return model.model_dump(**kwargs)
    return model.dict(**kwargs)


class RelayConfig(BaseModel):
    host: str = ""
    port: int = 19876

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}/ws"

    @property
    def http_origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def local_origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class RelayMessage(BaseModel):
    action: str
    session_id: str = ""
    tab_id: str = ""
    domain: str = "?"
    role: str = "slave"
    type: str = ""
    selector: str = ""
    value: str = ""
    path: str = ""
    x: float = 0
    y: float = 0
    key: str = ""
    code: str = ""
    level: str = "info"
    msg: str = ""
    enabled: Optional[bool] = None


class ScriptRecord(BaseModel):
    id: str
    name: str
    description: str = ""
    code: str
    active: bool = True


class WorkspaceData(BaseModel):
    mobile_mode: str = "Modo Mobile (Android e iOS)"
    stealth_mode: bool = True
    add_extension_mode: bool = True
    resume_only: bool = False
    security_password: str = ""
    withdraw_password: str = ""
    pix_keys: list[str] = Field(default_factory=list)
    accounts: list[str] = Field(default_factory=list)
    proxies: list[str] = Field(default_factory=list)
    deposit_values: list[str] = Field(default_factory=lambda: ["22", "33"])
    master_urls: list[str] = Field(default_factory=list)
    child_urls: list[str] = Field(default_factory=list)
    completed_links: list[str] = Field(default_factory=list)
    html5_speed_enabled: bool = False
    html5_speed: float = 1.0


class LogEvent(BaseModel):
    type: str = "log"
    level: str = "INFO"
    message: str
    source: str = "app"
    timestamp: float = Field(default_factory=time.time)


class BrowserDebugEvent(BaseModel):
    type: str = "browser_debug"
    domain: str = "?"
    level: str = "INFO"
    message: str
    timestamp: float = Field(default_factory=time.time)


class ChildLinkCapturedEvent(BaseModel):
    type: str = "child_link_captured"
    domain: str = "?"
    url: str = ""
    timestamp: float = Field(default_factory=time.time)


class AccountCapturedEvent(BaseModel):
    type: str = "account_captured"
    domain: str = "?"
    account: str = ""
    phone: str = ""
    password: str = ""
    url: str = ""
    timestamp: float = Field(default_factory=time.time)


DEFAULT_HTML5_SPEED_CONFIG = {
    "enabled": False,
    "speed": 1.0,
    "cbSetIntervalChecked": True,
    "cbSetTimeoutChecked": True,
    "cbPerformanceNowChecked": True,
    "cbDateNowChecked": True,
    "cbRequestAnimationFrameChecked": True,
}
HTML5_SPEED_MAX_MULTIPLIER = 4.0


def build_html5_speed_config(data: WorkspaceData | None) -> dict:
    current = data or WorkspaceData()
    speed = max(0.1, min(HTML5_SPEED_MAX_MULTIPLIER, float(getattr(current, "html5_speed", 1.0) or 1.0)))
    return {
        "enabled": bool(getattr(current, "html5_speed_enabled", False)),
        "speed": speed,
        "cbSetIntervalChecked": True,
        "cbSetTimeoutChecked": True,
        "cbPerformanceNowChecked": True,
        "cbDateNowChecked": True,
        "cbRequestAnimationFrameChecked": True,
    }


def normalize_speed_config_for_cdp(config: dict | None) -> dict:
    config = config or {}
    try:
        speed = float(config.get("speed", 1.0) or 1.0)
    except Exception:
        speed = 1.0
    return {
        "enabled": bool(config.get("enabled", False)),
        "speed": max(0.1, min(HTML5_SPEED_MAX_MULTIPLIER, speed)),
        "cbSetIntervalChecked": config.get("cbSetIntervalChecked") is not False,
        "cbSetTimeoutChecked": config.get("cbSetTimeoutChecked") is not False,
        "cbPerformanceNowChecked": config.get("cbPerformanceNowChecked") is not False,
        "cbDateNowChecked": config.get("cbDateNowChecked") is not False,
        "cbRequestAnimationFrameChecked": config.get("cbRequestAnimationFrameChecked") is not False,
    }


class BotStatusEvent(BaseModel):
    type: str = "bot_status"
    state: str = "idle"
    message: str = ""
    opened_tabs: int = 0
    profile_id: str = ""
    profile_name: str = ""
    session_id: str = ""
    running_profiles: int = 0
    total_profiles: int = 0
    timestamp: float = Field(default_factory=time.time)


class ProfilesLoadedEvent(BaseModel):
    type: str = "profiles_loaded"
    profiles: list["AdsPowerProfile"] = Field(default_factory=list)
    reachable: bool = True
    timestamp: float = Field(default_factory=time.time)


class MirrorGeneratedEvent(BaseModel):
    type: str = "mirror_generated"
    path: str
    timestamp: float = Field(default_factory=time.time)


class GridActionRequest(BaseModel):
    session_id: str
    action: str
    index: int | None = None
    x_ratio: float | None = None
    y_ratio: float | None = None
    delta_y: int | None = None
    text: str = ""


class AdsPowerProfile(BaseModel):
    user_id: str
    name: str

    @property
    def display_name(self) -> str:
        return f"{self.name} ({self.user_id})"


class AdsPowerBrowserInfo(BaseModel):
    debug_port: int | None = None
    webdriver: str = ""


class VolumeSettings(BaseModel):
    display_mode: str = "grade_real"
    monitor_choice: str = "Todos os monitores"
    url_assignment_mode: str = "distribute"
    master_passthrough_first_url: bool = False
    max_concurrent_profiles: int = 2
    url_batch_size: int = 8
    max_tabs_per_profile: int = 24
    batch_pause_seconds: float = 1.0
    profile_launch_pause_seconds: float = 0.5
    tab_open_delay_seconds: float = 0.15
    layout_columns: int = 0
    window_origin_x: int = 0
    window_origin_y: int = 0
    window_area_width: int = 0
    window_area_height: int = 0
    window_gap: int = 6
    base_window_pause_seconds: float = 5.0
    grid_columns: int = 0
    grid_auto_reload_seconds: float = 30.0
    iframe_retry_limit: int = 3
    browser_ready_seconds: float = 3.0

GRID_PREVIEW_MIN_INTERVAL_MASTER = 0.067
GRID_PREVIEW_MIN_INTERVAL_SLAVE = 0.08
GRID_DASHBOARD_REFRESH_MS = 66
GRID_CAPTURE_JPEG_QUALITY = 48
NETWORK_PROXY_MIN_CLICK_INTERVAL_SECONDS = 0.12


@dataclass
class DesktopBounds:
    x: int = 0
    y: int = 0
    width: int = 1920
    height: int = 1080


@dataclass
class WindowPlacement:
    x: int
    y: int
    width: int
    height: int


MOBILE_VIEWPORT_WIDTH = 390
MOBILE_VIEWPORT_HEIGHT = 844


class NetworkAdapterInfo(BaseModel):
    interface: str
    ipv4: str
    netmask: str
    cidr_prefix: int
    network: str
    source: str = "pc"


class BrowserNetworkInfo(BaseModel):
    profile_id: str = ""
    profile_name: str = ""
    session_id: str = ""
    local_ips: list[str] = Field(default_factory=list)
    matched_adapter: NetworkAdapterInfo | None = None
    note: str = ""


class NetworkSnapshotEvent(BaseModel):
    type: str = "network_snapshot"
    pc_adapters: list[NetworkAdapterInfo] = Field(default_factory=list)
    browser_networks: list[BrowserNetworkInfo] = Field(default_factory=list)
    timestamp: float = Field(default_factory=time.time)


class ExecutionPlanEntry(BaseModel):
    profile: AdsPowerProfile
    urls: list[str] = Field(default_factory=list)
    slave_urls: list[str] = Field(default_factory=list)
    batches: list[list[str]] = Field(default_factory=list)
    skipped_urls: int = 0


class ExecutionPlan(BaseModel):
    entries: list[ExecutionPlanEntry] = Field(default_factory=list)
    assignment_mode: str = "distribute"
    total_input_urls: int = 0
    total_assigned_urls: int = 0
    total_profiles: int = 0
    max_concurrent_profiles: int = 1
    total_batches: int = 0
    execution_waves: int = 0
    estimated_peak_tabs: int = 0
    estimated_runtime_seconds: float = 0.0
    skipped_urls: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# ============================================================
# THEME
# ============================================================
C_BG = "#0d131a"
C_PANEL = "#131d28"
C_PANEL_2 = "#0f1721"
C_ACCENT = "#00ff88"
C_TEXT = "#e8f2ff"
C_MUTED = "#7a8da6"
C_BORDER = "#213040"
C_WARN = "#ffbf47"
C_DANGER = "#ff5c7a"
FONT_MONO = "Consolas"
FONT_DISPLAY = "Consolas"


def apply_theme() -> None:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("green")


# ============================================================
# UTIL - JANELAS
# ============================================================
def detect_desktop_bounds() -> DesktopBounds:
    if sys.platform == "win32":
        try:
            user32 = ctypes.windll.user32
            x = int(user32.GetSystemMetrics(76))
            y = int(user32.GetSystemMetrics(77))
            width = int(user32.GetSystemMetrics(78))
            height = int(user32.GetSystemMetrics(79))
            if width > 0 and height > 0:
                return DesktopBounds(x=x, y=y, width=width, height=height)
        except Exception as exc:
            logger.warning("desktop_bounds_detect_failed", error=str(exc))
    return DesktopBounds()


def list_desktop_monitors() -> list[DesktopBounds]:
    monitors: list[DesktopBounds] = []
    if sys.platform == "win32":
        try:
            user32 = ctypes.windll.user32
            callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulonglong, ctypes.c_ulonglong, ctypes.POINTER(ctypes.c_long * 4), ctypes.c_double)
            seen: set[tuple[int, int, int, int]] = set()

            def _enum_proc(_hmonitor, _hdc, lprc, _lparam):
                rect = lprc.contents
                left = int(rect[0])
                top = int(rect[1])
                right = int(rect[2])
                bottom = int(rect[3])
                bounds = (left, top, max(1, right - left), max(1, bottom - top))
                if bounds not in seen:
                    seen.add(bounds)
                    monitors.append(DesktopBounds(x=bounds[0], y=bounds[1], width=bounds[2], height=bounds[3]))
                return 1

            user32.EnumDisplayMonitors(0, 0, callback_type(_enum_proc), 0)
        except Exception as exc:
            logger.warning("desktop_monitors_enum_failed", error=str(exc))
    try:
        seen: set[tuple[int, int, int, int]] = {(m.x, m.y, m.width, m.height) for m in monitors}
        for monitor in get_monitors():
            bounds = (int(monitor.x), int(monitor.y), int(monitor.width), int(monitor.height))
            if bounds in seen:
                continue
            seen.add(bounds)
            monitors.append(DesktopBounds(x=bounds[0], y=bounds[1], width=bounds[2], height=bounds[3]))
    except Exception as exc:
        logger.warning("desktop_monitors_list_failed", error=str(exc))
    if not monitors:
        monitors.append(detect_desktop_bounds())
    monitors.sort(key=lambda item: (item.x, item.y, item.width, item.height))
    return monitors


def format_monitor_label(index: int, monitor: DesktopBounds) -> str:
    return f"Monitor {index + 1}: {monitor.width}x{monitor.height} @ ({monitor.x},{monitor.y})"


def detect_all_monitors_bounds(monitors: list[DesktopBounds] | None = None) -> DesktopBounds:
    items = monitors or list_desktop_monitors()
    if not items:
        return detect_desktop_bounds()
    min_x = min(item.x for item in items)
    min_y = min(item.y for item in items)
    max_x = max(item.x + item.width for item in items)
    max_y = max(item.y + item.height for item in items)
    return DesktopBounds(x=min_x, y=min_y, width=max_x - min_x, height=max_y - min_y)


def detect_monitor_from_cell(cell: tuple[int, int, int, int] | None) -> DesktopBounds:
    if cell is None:
        return detect_desktop_bounds()
    try:
        cx = int(cell[0]) + int(cell[2]) // 2
        cy = int(cell[1]) + int(cell[3]) // 2
        for monitor in list_desktop_monitors():
            if monitor.x <= cx < monitor.x + monitor.width and monitor.y <= cy < monitor.y + monitor.height:
                return monitor
    except Exception as exc:
        logger.warning("grid_monitor_detect_failed", error=str(exc))
    return detect_desktop_bounds()


def monitor_storage_key(monitor: DesktopBounds) -> str:
    return f"{int(monitor.x)}:{int(monitor.y)}:{int(monitor.width)}:{int(monitor.height)}"


def find_monitor_by_storage_key(key: str, monitors: list[DesktopBounds] | None = None) -> DesktopBounds | None:
    items = monitors or list_desktop_monitors()
    for monitor in items:
        if monitor_storage_key(monitor) == str(key):
            return monitor
    return None


def calculate_calibrated_slots_for_monitor(
    monitor: DesktopBounds,
    cell: tuple[int, int, int, int] | None,
    gap: int = 0,
) -> list[WindowPlacement]:
    workspace = monitor
    gap = max(0, int(gap))
    if cell is None:
        return [WindowPlacement(x=int(workspace.x), y=int(workspace.y), width=int(workspace.width), height=int(workspace.height))]
    base_x, base_y, base_w, base_h = [int(v) for v in cell]
    base_w = max(80, base_w)
    base_h = max(80, base_h)
    cols_max = max(1, (workspace.width + gap) // max(1, base_w + gap))
    rows_max = max(1, (workspace.height + gap) // max(1, base_h + gap))
    start_x = workspace.x if base_x < workspace.x or base_x >= workspace.x + workspace.width else base_x
    start_y = workspace.y if base_y < workspace.y or base_y >= workspace.y + workspace.height else base_y
    placements: list[WindowPlacement] = []
    for index in range(max(1, cols_max * rows_max)):
        row, col = divmod(index, cols_max)
        x = start_x + col * (base_w + gap)
        y = start_y + row * (base_h + gap)
        if x + base_w > workspace.x + workspace.width:
            x = workspace.x + max(0, workspace.width - base_w)
        if y + base_h > workspace.y + workspace.height:
            y = workspace.y + max(0, workspace.height - base_h)
        placements.append(WindowPlacement(x=int(x), y=int(y), width=int(base_w), height=int(base_h)))
    return placements


def calculate_multi_monitor_grid(
    item_count: int,
    monitors: list[DesktopBounds],
    cells_by_monitor: dict[str, tuple[int, int, int, int]] | None,
    gap: int = 0,
) -> tuple[list[WindowPlacement], DesktopBounds]:
    items = sorted(monitors or list_desktop_monitors(), key=lambda item: (item.x, item.y, item.width, item.height))
    if not items:
        workspace = detect_desktop_bounds()
        items = [workspace]
    workspace = detect_all_monitors_bounds(items)
    if item_count <= 0:
        return [], workspace
    placements: list[WindowPlacement] = []
    cells_map = dict(cells_by_monitor or {})
    for monitor in items:
        placements.extend(calculate_calibrated_slots_for_monitor(monitor, cells_map.get(monitor_storage_key(monitor)), gap))
        if len(placements) >= item_count:
            return placements[:item_count], workspace
    remaining = item_count - len(placements)
    cols = max(1, math.ceil(math.sqrt(remaining * workspace.width / max(1, workspace.height))))
    rows = max(1, math.ceil(remaining / cols))
    cell_width = max(80, int((workspace.width - gap * (cols - 1)) / cols))
    cell_height = max(80, int((workspace.height - gap * (rows - 1)) / rows))
    for index in range(remaining):
        row = index // cols
        col = index % cols
        placements.append(
            WindowPlacement(
                x=int(workspace.x + col * (cell_width + gap)),
                y=int(workspace.y + row * (cell_height + gap)),
                width=int(cell_width),
                height=int(cell_height),
            )
        )
    return placements[:item_count], workspace


def calculate_calibrated_grid(
    item_count: int,
    cell: tuple[int, int, int, int] | None,
    gap: int = 0,
) -> tuple[list[WindowPlacement], DesktopBounds]:
    workspace = detect_monitor_from_cell(cell)
    if cell is None or item_count <= 0:
        return [], workspace
    base_x, base_y, base_w, base_h = [int(v) for v in cell]
    base_w = max(80, base_w)
    base_h = max(80, base_h)
    gap = max(0, int(gap))
    cols_max = max(1, (workspace.width + gap) // max(1, base_w + gap))
    rows_max = max(1, (workspace.height + gap) // max(1, base_h + gap))
    capacity = max(1, cols_max * rows_max)
    if item_count > capacity:
        cols = max(1, math.ceil(math.sqrt(item_count * workspace.width / max(1, workspace.height))))
        rows = max(1, math.ceil(item_count / cols))
        base_w = max(80, int((workspace.width - gap * (cols - 1)) / cols))
        base_h = max(80, int((workspace.height - gap * (rows - 1)) / rows))
    else:
        cols = cols_max
    placements: list[WindowPlacement] = []
    start_x = workspace.x if base_x < workspace.x or base_x >= workspace.x + workspace.width else base_x
    start_y = workspace.y if base_y < workspace.y or base_y >= workspace.y + workspace.height else base_y
    for index in range(item_count):
        row, col = divmod(index, cols)
        x = start_x + col * (base_w + gap)
        y = start_y + row * (base_h + gap)
        if x + base_w > workspace.x + workspace.width:
            x = workspace.x + max(0, workspace.width - base_w)
        if y + base_h > workspace.y + workspace.height:
            y = workspace.y + max(0, workspace.height - base_h)
        placements.append(WindowPlacement(x=int(x), y=int(y), width=int(base_w), height=int(base_h)))
    return placements, workspace


# ============================================================
# UTIL - REDE
# ============================================================

def is_valid_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(str(value))
        return True
    except Exception:
        return False


def is_valid_relay_ipv4(value: str) -> bool:
    if not is_valid_ipv4(value):
        return False
    return not str(value).startswith("127.")


def prefix_to_netmask(prefix: int) -> str:
    network = ipaddress.IPv4Network(f"0.0.0.0/{prefix}")
    return str(network.netmask)


def detect_local_ipv4(default: str = "127.0.0.1") -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if is_valid_relay_ipv4(ip):
                return ip
    except Exception:
        pass
    return default


def pick_available_port(preferred: int = 19876, host: str = "0.0.0.0") -> int:
    def can_bind(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return True
            except OSError:
                return False

    if can_bind(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def detect_network_adapters() -> list[NetworkAdapterInfo]:
    if sys.platform != "win32":
        ip = detect_local_ipv4("")
        if not ip:
            return []
        return [
            NetworkAdapterInfo(
                interface="default",
                ipv4=ip,
                netmask="255.255.255.0",
                cidr_prefix=24,
                network=str(ipaddress.IPv4Network(f"{ip}/24", strict=False)),
            )
        ]

    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-NetIPAddress -AddressFamily IPv4 | "
            "Where-Object { $_.IPAddress -notlike '127.*' -and $_.PrefixOrigin -ne 'WellKnown' } | "
            "Select-Object InterfaceAlias,IPAddress,PrefixLength | ConvertTo-Json -Compress"
        ),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=5)
    except Exception as exc:
        logger.warning("network_adapter_detect_failed", error=str(exc))
        return []
    raw = (result.stdout or "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except Exception as exc:
        logger.warning("network_adapter_json_failed", error=str(exc))
        return []
    items = payload if isinstance(payload, list) else [payload]
    adapters: list[NetworkAdapterInfo] = []
    for item in items:
        ip = str(item.get("IPAddress") or "")
        if not is_valid_relay_ipv4(ip):
            continue
        prefix = int(item.get("PrefixLength") or 24)
        adapters.append(
            NetworkAdapterInfo(
                interface=str(item.get("InterfaceAlias") or "adapter"),
                ipv4=ip,
                netmask=prefix_to_netmask(prefix),
                cidr_prefix=prefix,
                network=str(ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)),
            )
        )
    return adapters


def detect_primary_adapter(default_ip: str = "") -> NetworkAdapterInfo | None:
    adapters = detect_network_adapters()
    if not adapters:
        return None
    preferred_ip = detect_local_ipv4(default=default_ip)
    for adapter in adapters:
        if adapter.ipv4 == preferred_ip:
            return adapter
    return adapters[0]


def detect_local_ipv4_candidates(default: str = "") -> list[str]:
    candidates: list[str] = []

    def add(host: str) -> None:
        current = str(host or "").strip()
        if not current:
            return
        if current in candidates:
            return
        if current in {"127.0.0.1", "localhost"} or is_valid_ipv4(current):
            candidates.append(current)

    for adapter in detect_network_adapters():
        add(adapter.ipv4)

    add(detect_local_ipv4(default=default or ""))
    add(default)
    add("127.0.0.1")
    add("localhost")
    return candidates


def match_browser_network(browser_ips: Iterable[str], adapters: list[NetworkAdapterInfo]) -> BrowserNetworkInfo | None:
    filtered = [ip for ip in browser_ips if is_valid_ipv4(ip) and not ip.startswith("127.")]
    if not filtered:
        return BrowserNetworkInfo(note="Browser nao expos IPv4 local utilizavel.")
    for browser_ip in filtered:
        browser_address = ipaddress.IPv4Address(browser_ip)
        for adapter in adapters:
            network = ipaddress.IPv4Network(f"{adapter.ipv4}/{adapter.cidr_prefix}", strict=False)
            if browser_address in network:
                return BrowserNetworkInfo(
                    local_ips=filtered,
                    matched_adapter=NetworkAdapterInfo(**model_dump_compat(adapter), source="browser_match"),
                    note=f"Browser corresponde a rede {adapter.network}.",
                )
    return BrowserNetworkInfo(local_ips=filtered, note="Browser expos IPv4 local, mas sem match com a rede do PC.")


def format_adapter_summary(adapter: NetworkAdapterInfo | None) -> str:
    if adapter is None:
        return "Rede indisponivel"
    return f"{adapter.interface}: {adapter.ipv4}/{adapter.cidr_prefix} ({adapter.netmask})"


# ============================================================
# PAYLOADS EMBUTIDOS
# ============================================================
DEFAULT_LAYOUT_JS = r"""
(async () => {
    if (window.__ltdf_layout_running__) return;
    window.__ltdf_layout_running__ = true;
    const sendLog = (msg, level = 'info') => {
        try {
            window.postMessage({ __ltdf__: true, to: 'bg', data: {
                action: 'browser_log', level, domain: location.hostname, msg: '[layout] ' + msg
            } }, '*');
        } catch (_) {}
        console.log('[LTDF-LAYOUT] ' + msg);
    };
    const sendBGAction = (payload) => {
        try {
            window.postMessage({ __ltdf__: true, to: 'bg', data: {
                ...(payload || {}),
                domain: payload?.domain || location.hostname,
            } }, '*');
        } catch (_) {}
    };
    const delay = ms => new Promise(r => setTimeout(r, ms));
    const params = new URLSearchParams(location.search || '');
    const workspaceCfg = window.__ltdfWorkspace || {};
    const isStandaloneApp = (() => {
        try {
            return !!(window.matchMedia && window.matchMedia('(display-mode: standalone)').matches);
        } catch (_) {
            return false;
        }
    })() || !!window.navigator.standalone;
    let installPopupSeen = false;
    let installPanelShieldActive = false;
    if (isStandaloneApp) {
        sendLog('Janela em modo standalone detectada; layout pausado nesta etapa.');
        window.__ltdf_layout_running__ = false;
        return;
    }
    const sendChildLink = (value) => {
        const current = String(value || '').trim();
        if (!current) return false;
        try {
            window.postMessage({ __ltdf__: true, to: 'bg', data: {
                action: 'child_link_captured', domain: location.hostname, value: current
            } }, '*');
            sendLog('Link da filha capturado: ' + current);
            return true;
        } catch (_) {
            return false;
        }
    };
    const extractFirstUrl = (text) => {
        const current = String(text || '');
        const match = current.match(/https?:\/\/[^\s"'<>]+/i);
        return match ? match[0].trim() : '';
    };
    const extractChildLink = () => {
        const directSelectors = [
            'p.share_link',
            '.share_link',
            '[class*="share_link"]',
            '.box_link p',
            '.box_link .fontColor21',
            '.linkBox p',
            '.sh_box p',
        ];
        for (const sel of directSelectors) {
            for (const el of qsa(sel)) {
                const found = extractFirstUrl(el.textContent || '');
                if (found && found.includes('code=')) return found;
            }
        }
        const labels = qsa('div,span,p,section,strong,b');
        for (const label of labels) {
            const text = norm(label.textContent || '');
            if (!text.includes('meu link')) continue;
            const root = label.closest('.box_link, .linkBox, .sh_box, .share_box, section, div') || document;
            for (const el of qsa('a,p,div,span', root)) {
                const found = extractFirstUrl(el.textContent || '');
                if (found && found.includes('code=')) return found;
                const href = String(el.getAttribute?.('href') || '').trim();
                if (href.startsWith('http') && href.includes('code=')) return href;
            }
        }
        return '';
    };
    const captureChildLinkOnce = () => {
        if (window.__ltdf_child_link_sent__) return false;
        const found = extractChildLink();
        if (!found) return false;
        if (sendChildLink(found)) {
            window.__ltdf_child_link_sent__ = found;
            return true;
        }
        return false;
    };
    const watchChildLink = (timeout = 45000) => {
        if (window.__ltdf_child_link_watch__) return;
        window.__ltdf_child_link_watch__ = true;
        const startedAt = Date.now();
        const timer = setInterval(() => {
            if (captureChildLinkOnce()) {
                clearInterval(timer);
                window.__ltdf_child_link_watch__ = false;
                return;
            }
            if (Date.now() - startedAt > timeout) {
                clearInterval(timer);
                window.__ltdf_child_link_watch__ = false;
            }
        }, 800);
    };
    const sendAccountCaptured = (payload) => {
        try {
            sendBGAction({
                action: 'account_captured',
                account: String(payload?.account || '').trim(),
                phone: String(payload?.phone || '').trim(),
                password: String(payload?.password || '').trim(),
                url: String(payload?.url || location.href || '').trim(),
            });
            return true;
        } catch (_) {
            return false;
        }
    };

    const waitFor = async (sel, timeout = 20000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < timeout) {
            const el = document.querySelector(sel);
            if (el && el.offsetParent !== null) return el;
            await delay(400);
        }
        return null;
    };
    const waitForGameDiv = async (imageUrl, timeout = 20000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < timeout) {
            const img = document.querySelector(`img[src="${imageUrl}"]`);
            if (img) {
                const gameDiv = img.closest('.item, .game_item, [class*="item"]');
                if (gameDiv && gameDiv.offsetParent !== null) return gameDiv;
            }
            await delay(400);
        }
        return null;
    };
    const findDownloadPopupButton = () => {
        const selectors = [
            '.pwaDownload_popup .btn.shiny.btncss1',
            '.pwaDownload_box .btn.shiny.btncss1',
            '.pwaDownload_box_bottom .btn_box .btn.shiny.btncss1',
            '.pwaDownload_box_button.btn',
            '.btn.shiny.btncss1',
        ];
        for (const sel of selectors) {
            for (const el of qsa(sel)) {
                if (!isVisible(el)) continue;
                const popupRoot = el.closest('.pwaDownload_popup, .pwaDownload_box, [class*="pwaDownload"]');
                if (popupRoot && isVisible(popupRoot)) return el;
                const text = norm(el.textContent || '');
                if (
                    !text ||
                    text.includes('instalar') ||
                    text.includes('baixar') ||
                    text.includes('download') ||
                    text.includes('ganhe')
                ) return el;
            }
        }
        for (const popupRoot of qsa('.pwaDownload_popup, .pwaDownload_box, [class*="pwaDownload"]')) {
            if (!isVisible(popupRoot)) continue;
            const candidate = qsa('button, .btn, div[class*="btn"]', popupRoot).find(isVisible);
            if (candidate) return candidate;
        }
        return null;
    };
    const setInstallPanelShield = (active) => {
        installPanelShieldActive = !!active;
        try {
            const panel = document.getElementById('__ltdf_panel__');
            if (!panel) return;
            if (active) {
                panel.style.pointerEvents = 'none';
                panel.style.opacity = '0';
                panel.style.visibility = 'hidden';
            } else {
                panel.style.pointerEvents = '';
                panel.style.opacity = '';
                panel.style.visibility = '';
            }
        } catch (_) {}
    };
    const isDownloadPopupVisible = () => {
        for (const popupRoot of qsa('.pwaDownload_popup, .pwaDownload_box, [class*="pwaDownload"]')) {
            if (isVisible(popupRoot)) return true;
        }
        return false;
    };
    const forceActivateElement = (el) => {
        if (!el) return false;
        try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (_) {}
        const rect = typeof el.getBoundingClientRect === 'function' ? el.getBoundingClientRect() : null;
        const x = rect ? Math.round(rect.left + rect.width / 2) : 0;
        const y = rect ? Math.round(rect.top + rect.height / 2) : 0;
        const mouseOpts = { bubbles: true, cancelable: true, clientX: x, clientY: y, view: window };
        try { if (typeof el.focus === 'function') el.focus({ preventScroll: true }); } catch (_) {}
        try { el.dispatchEvent(new PointerEvent('pointerdown', mouseOpts)); } catch (_) {}
        try { el.dispatchEvent(new PointerEvent('pointerup', mouseOpts)); } catch (_) {}
        try { el.dispatchEvent(new MouseEvent('mousedown', mouseOpts)); } catch (_) {}
        try { el.dispatchEvent(new MouseEvent('mouseup', mouseOpts)); } catch (_) {}
        try { el.dispatchEvent(new MouseEvent('click', mouseOpts)); } catch (_) {}
        try { el.click(); } catch (_) {}
        if (rect) {
            try {
                const centerEl = document.elementFromPoint(x, y);
                if (centerEl && centerEl !== el) {
                    try { centerEl.dispatchEvent(new PointerEvent('pointerdown', mouseOpts)); } catch (_) {}
                    try { centerEl.dispatchEvent(new PointerEvent('pointerup', mouseOpts)); } catch (_) {}
                    try { centerEl.dispatchEvent(new MouseEvent('click', mouseOpts)); } catch (_) {}
                    try { if (typeof centerEl.click === 'function') centerEl.click(); } catch (_) {}
                }
            } catch (_) {}
        }
        return true;
    };
    const forceTouchActivateElement = (el) => {
        if (!el) return false;
        const chain = [];
        let current = el;
        while (current && chain.length < 4) {
            chain.push(current);
            current = current.parentElement;
        }
        for (const node of chain) {
            try { node.scrollIntoView({ block: 'center', inline: 'center' }); } catch (_) {}
            const rect = typeof node.getBoundingClientRect === 'function' ? node.getBoundingClientRect() : null;
            const x = rect ? Math.round(rect.left + rect.width / 2) : 0;
            const y = rect ? Math.round(rect.top + rect.height / 2) : 0;
            const mouseOpts = { bubbles: true, cancelable: true, clientX: x, clientY: y, view: window };
            try { if (typeof node.focus === 'function') node.focus({ preventScroll: true }); } catch (_) {}
            try { node.dispatchEvent(new TouchEvent('touchstart', { bubbles: true, cancelable: true })); } catch (_) {}
            try { node.dispatchEvent(new TouchEvent('touchend', { bubbles: true, cancelable: true })); } catch (_) {}
            try { node.dispatchEvent(new PointerEvent('pointerdown', mouseOpts)); } catch (_) {}
            try { node.dispatchEvent(new PointerEvent('pointerup', mouseOpts)); } catch (_) {}
            try { node.dispatchEvent(new MouseEvent('mousedown', mouseOpts)); } catch (_) {}
            try { node.dispatchEvent(new MouseEvent('mouseup', mouseOpts)); } catch (_) {}
            try { node.dispatchEvent(new MouseEvent('click', mouseOpts)); } catch (_) {}
            try { if (typeof node.click === 'function') node.click(); } catch (_) {}
        }
        return true;
    };
const maybeHandleDownloadPopup = async (timeout = 12000) => {
        const startedAt = Date.now();
        while (Date.now() - startedAt < timeout) {
            const button = findDownloadPopupButton();
            if (button) {
                installPopupSeen = true;
                setInstallPanelShield(true);
                const exactDiv =
                    findVisible([
                        '.pwaDownload_popup div.btn.shiny.btncss1',
                        '.pwaDownload_box div.btn.shiny.btncss1',
                        'div.btn.shiny.btncss1',
                    ]) || null;
                const target = exactDiv || button;
                const activationChain = [];
                const pushNode = (node) => {
                    if (!node) return;
                    if (!activationChain.includes(node)) activationChain.push(node);
                };
                pushNode(exactDiv);
                pushNode(button);
                for (const node of activationChain) {
                    forceTouchActivateElement(node);
                    await delay(120);
                    forceActivateElement(node);
                    await delay(120);
                }
                sendLog(`Clique forÃ§ado no botao/div do popup executado: ${target.tagName}.${String(target.className || '').trim()}`);
                sendLog('Popup de download detectado; tentando liberar o formulario.');
                await delay(1200);
                return true;
            }
            await delay(350);
        }
        return false;
    };
    const ensureInstallBeforeRegister = async () => {
        const popupAppeared = await maybeHandleDownloadPopup(3500);
        if (!popupAppeared) {
            setInstallPanelShield(false);
            sendLog('Popup de instalacao nao apareceu; seguindo para o formulario de registro.', 'warn');
            return true;
        }
        if (isDownloadPopupVisible()) {
            sendLog('Popup de download ainda visivel; tentando liberar o formulario.');
            await closeDownloadPopupIfVisible();
        }
        setInstallPanelShield(false);
        sendLog('Popup removido/liberado; seguindo registro.');
        return true;
    };
    const input = (sel, value) => {
        const el = document.querySelector(sel);
        if (!el) return false;
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
        if (setter) setter.call(el, value); else el.value = value;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return true;
    };
    const isVisible = (el) => !!(el && el.isConnected && el.offsetParent !== null);
    const norm = (txt) => String(txt || '').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '');
    const qsa = (sel, root = document) => {
        try { return Array.from(root.querySelectorAll(sel)); } catch (_) { return []; }
    };
    const findVisible = (selectors) => {
        for (const sel of selectors) {
            for (const el of qsa(sel)) {
                if (isVisible(el)) return el;
            }
        }
        return null;
    };
    const clickFirstByText = (texts, selectors = ['button','a','div','span','section','label']) => {
        const wanted = texts.map(norm);
        for (const sel of selectors) {
            for (const el of qsa(sel)) {
                if (!isVisible(el)) continue;
                const text = norm(el.textContent || '');
                if (!text) continue;
                if (wanted.some(token => text.includes(token))) {
                    try { el.click(); return true; } catch (_) {}
                }
            }
        }
        return false;
    };
    const findFirstByText = (texts, selectors = ['button','a','div','span','section','label'], root = document) => {
        const wanted = texts.map(norm);
        for (const sel of selectors) {
            for (const el of qsa(sel, root)) {
                if (!isVisible(el)) continue;
                const text = norm(el.textContent || '');
                if (!text) continue;
                if (wanted.some(token => text.includes(token))) return el;
            }
        }
        return null;
    };
    const closeDownloadPopupIfVisible = async () => {
        setInstallPanelShield(false);
        const closeBtn = findVisible([
            '.pwaDownload_popup .close_btn',
            '.pwaDownload_popup .close',
            '.pwaDownload_box .close_btn',
            '.pwaDownload_box .close',
            '[class*="pwaDownload"] [class*="close"]',
        ]);
        if (closeBtn) {
            forceActivateElement(closeBtn);
            installPopupSeen = false;
            sendLog('Popup removido/liberado; seguindo registro.');
            await delay(500);
            return true;
        }
        return false;
    };
    const getRegisterForm = () => {
        const forms = [
            ...qsa('form[class*="loginRegisterForm"]'),
            ...qsa('form[class*="login-register"]'),
            ...qsa('.ui-tab__panel form'),
            ...qsa('form'),
        ];
        for (const form of forms) {
            if (!isVisible(form)) continue;
            const text = norm(form.textContent || '');
            if (text.includes('registro') || text.includes('register') || text.includes('senha') || text.includes('celular') || text.includes('conta')) {
                return form;
            }
        }
        const registerButtons = qsa('button, div, span, a, section').filter((el) => {
            if (!isVisible(el)) return false;
            const text = norm(el.textContent || '');
            return text.includes('registro') || text.includes('registrar') || text.includes('register');
        });
        for (const button of registerButtons) {
            let current = button.closest('section, div, article, main');
            while (current) {
                if (!isVisible(current)) {
                    current = current.parentElement?.closest?.('section, div, article, main') || null;
                    continue;
                }
                const inputs = qsa('input', current).filter(isVisible);
                const text = norm(current.textContent || '');
                if (
                    inputs.length >= 2 &&
                    (
                        text.includes('celular') ||
                        text.includes('senha') ||
                        text.includes('confirmar senha') ||
                        text.includes('numero do celular')
                    )
                ) {
                    return current;
                }
                current = current.parentElement?.closest?.('section, div, article, main') || null;
            }
        }
        const containers = qsa('section, div, article, main').filter((el) => {
            if (!isVisible(el)) return false;
            const text = norm(el.textContent || '');
            if (!text) return false;
            const inputs = qsa('input', el).filter(isVisible);
            const hasRegisterText = text.includes('registro') || text.includes('registrar') || text.includes('numero de celular') || text.includes('celular') || text.includes('confirmar senha');
            const hasRegisterBtn = qsa('button, div, span, a', el).some((child) => {
                if (!isVisible(child)) return false;
                const childText = norm(child.textContent || '');
                return childText.includes('registrar') || childText.includes('registro') || childText.includes('register');
            });
            return inputs.length >= 2 && (hasRegisterText || hasRegisterBtn);
        });
        containers.sort((a, b) => qsa('input', b).length - qsa('input', a).length);
        if (containers.length) return containers[0];
        return null;
    };
    const waitForRegisterForm = async (timeout = 12000) => {
        const t0 = Date.now();
        while (Date.now() - t0 < timeout) {
            const form = getRegisterForm();
            if (form) return form;
            await delay(300);
        }
        return null;
    };
    const ensureRegisterContext = async () => {
        let form = getRegisterForm();
        if (form) return form;
        sendLog('Formulario de registro nao visivel; tentando abrir contexto de registro...');
        const clickedTab = clickFirstByText(
            ['registro', 'register', 'conta registro', 'crie uma conta', 'suporta apenas conta registro'],
            ['button','a','div','span','section','label','li']
        );
        if (clickedTab) {
            await delay(1200);
            form = await waitForRegisterForm(6000);
            if (form) {
                sendLog('Contexto de registro aberto por clique na aba/botao.');
                return form;
            }
        }
        const path = String(location.pathname || '').toLowerCase();
        if (!path.includes('/register') && !path.includes('/registro') && !path.includes('/registered')) {
            try {
                const fallback = new URL('/registered', location.origin).href;
                sendLog('Registro nao apareceu; usando fallback para /registered');
                location.assign(fallback);
                await delay(4000);
            } catch (_) {}
        }
        return await waitForRegisterForm(8000);
    };
    const findFieldInForm = (form, kind) => {
        const selectorsByKind = {
            account: [
                'input[name="account"]',
                'input[name="username"]',
                'input[placeholder*="Conta"]',
                'input[placeholder*="conta"]',
                'input[placeholder*="Account"]',
            ],
            phone: [
                'input[name="phone"]',
                'input[name="mobile"]',
                'input[name="telephone"]',
                'input[type="tel"]',
                'input[inputmode="numeric"]',
                'input[placeholder*="Celular"]',
                'input[placeholder*="celular"]',
                'input[placeholder*="Numero de Celular"]',
                'input[placeholder*="NÃºmero de Celular"]',
                'input[placeholder*="Telefone"]',
                'input[placeholder*="telefone"]',
                'input[placeholder*="Phone"]',
            ],
            password: [
                'input[name="password"]',
                'input[name="pwd"]',
                'input[placeholder*="senha"]',
                'input[placeholder*="Senha"]',
                'input[type="password"]',
            ],
            confirm: [
                'input[name="repeatPassword"]',
                'input[name="confirmPassword"]',
                'input[name="rePassword"]',
                'input[placeholder*="Confirmar senha"]',
                'input[placeholder*="confirmar senha"]',
                'input[placeholder*="Repita a senha"]',
            ],
        };
        const selectors = selectorsByKind[kind] || [];
        for (const sel of selectors) {
            for (const el of qsa(sel, form)) {
                if (!isVisible(el)) continue;
                if (kind === 'password' && norm(el.getAttribute('placeholder') || '').includes('confirm')) continue;
                return el;
            }
        }
        if (kind === 'account') {
            for (const el of qsa('input:not([type="password"]):not([type="hidden"])', form)) {
                if (!isVisible(el)) continue;
                const ph = norm(el.getAttribute('placeholder') || '');
                if (ph.includes('conta') || ph.includes('account')) return el;
            }
        }
        if (kind === 'phone') {
            for (const el of qsa('input:not([type="password"]):not([type="hidden"])', form)) {
                if (!isVisible(el)) continue;
                const ph = norm(el.getAttribute('placeholder') || '');
                if (ph.includes('celular') || ph.includes('telefone') || ph.includes('phone')) return el;
            }
        }
        return null;
    };
    const setInputElement = (el, value) => {
        if (!el) return false;
        try { el.focus(); } catch (_) {}
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
        if (setter) setter.call(el, value); else el.value = value;
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: '1' }));
        try { el.blur(); } catch (_) {}
        return true;
    };
    const checkTermsIfPresent = (form) => {
        const checkbox = findVisible([
            'input[type="checkbox"]',
            '[role="checkbox"]',
            '.ui-checkbox',
            '.van-checkbox',
            '.van-checkbox__icon',
            '.van-checkbox__label',
            '[class*="checkbox"]',
        ]);
        if (!checkbox) return false;
        try {
            if (checkbox.matches?.('input[type="checkbox"]')) {
                if (!checkbox.checked) checkbox.click();
            } else {
                checkbox.click();
            }
            return true;
        } catch (_) {
            return false;
        }
    };
    const registrationAdvanced = () => {
        const currentPath = String(location.pathname || '').toLowerCase();
        if (!currentPath.includes('/registered') && !currentPath.includes('/register') && !currentPath.includes('/registro')) return true;
        if (findVisible([
            '.spin_center',
            '.btn.btncss1.fontColor13',
            '.btncss1.fontColor13',
            '.share_link',
            '[class*="share_link"]',
            '.myInvitation',
            '.toRink',
            '.invite',
            '.wallet',
            '.deposit',
        ])) return true;
        const regStillVisible = findVisible([
            'button.van-button.van-button--default.van-button--normal.btn.btncss1.fontColor13',
            '.btn.btncss1.fontColor13',
            '[class*="register"]',
        ]);
        return !regStillVisible;
    };
    const detectLegacyRegisterForm = () => {
        const phoneEl = document.querySelector('input[name="phone"]');
        const passwordEl = document.querySelector('input[name="password"]');
        if (!isVisible(phoneEl) || !isVisible(passwordEl)) return null;
        return {
            phone: phoneEl,
            password: passwordEl,
            confirm: document.querySelector('input[name="repeatPassword"]'),
            form: phoneEl.closest('form') || document.querySelector('form'),
            mode: 'legacy',
        };
    };
    const detectVisualRegisterContainer = () => {
        const root = getRegisterForm();
        if (!root) return null;
        const phoneEl = findFieldInForm(root, 'phone');
        const passwordEl = findFieldInForm(root, 'password');
        if (!isVisible(phoneEl) || !isVisible(passwordEl)) return null;
        return {
            phone: phoneEl,
            password: passwordEl,
            confirm: findFieldInForm(root, 'confirm'),
            account: findFieldInForm(root, 'account'),
            form: root,
            mode: 'visual',
        };
    };

    if (workspaceCfg.resume_only) {
        sendLog('Modo retomada ativo; abrindo apenas o link atual sem fluxo de cadastro.');
        watchChildLink();
        return;
    }

    try {
        const path = String(location.pathname || '').toLowerCase();
        const legacyForm = detectLegacyRegisterForm();
        const visualForm = detectVisualRegisterContainer();
        const isLegacyRoute = path.includes('/register') || path.includes('/registro') || path.includes('/registered');
        const ddds = [11, 21, 31, 41, 48, 51, 61, 71, 85, 91];
        const ddd = ddds[Math.floor(Math.random() * ddds.length)];
        const phone = `${ddd}9${Math.floor(Math.random() * 7 + 2)}${Math.floor(1000000 + Math.random() * 9000000)}`;
        const account = `usr${Math.random().toString(36).slice(2, 10)}`;
        const configuredPassword = String(workspaceCfg.security_password || '').trim();
        const password = configuredPassword || ('Aa' + Math.random().toString(36).slice(-6) + '!');
        const depositValues = Array.isArray(workspaceCfg.deposit_values) ? workspaceCfg.deposit_values.map(v => String(v).trim()).filter(Boolean) : [];
        let form = null;
        let accountInput = null;
        let phoneInput = null;
        let passwordInput = null;
        let confirmInput = null;

        const installGateOk = await ensureInstallBeforeRegister();
        if (!installGateOk) {
            return;
        }

        if (legacyForm && isLegacyRoute) {
            form = legacyForm.form || document;
            phoneInput = legacyForm.phone;
            passwordInput = legacyForm.password;
            confirmInput = isVisible(legacyForm.confirm) ? legacyForm.confirm : null;
            sendLog('Formulario detectado (layout antigo)');
        } else if (visualForm) {
            form = visualForm.form || document;
            accountInput = isVisible(visualForm.account) ? visualForm.account : null;
            phoneInput = visualForm.phone;
            passwordInput = visualForm.password;
            confirmInput = isVisible(visualForm.confirm) ? visualForm.confirm : null;
            sendLog('Formulario detectado (container visual)');
        } else {
            form = await ensureRegisterContext();
            if (!form) {
                sendLog('Formulario de registro nao encontrado', 'error');
                return;
            }
            sendLog('Formulario detectado');
            accountInput = findFieldInForm(form, 'account');
            phoneInput = findFieldInForm(form, 'phone');
            passwordInput = findFieldInForm(form, 'password');
            confirmInput = findFieldInForm(form, 'confirm');
        }
        await delay(2000);
        sendLog(`Campos detectados -> conta:${!!accountInput} celular:${!!phoneInput} senha:${!!passwordInput} confirmar:${!!confirmInput}`);
        if (accountInput) {
            setInputElement(accountInput, account);
            await delay(500);
        }
        if (phoneInput) {
            setInputElement(phoneInput, phone);
            await delay(500);
        }
        if (passwordInput) {
            setInputElement(passwordInput, password);
            await delay(500);
        }
        if (confirmInput) {
            setInputElement(confirmInput, password);
            await delay(500);
        }
        if (!accountInput && !phoneInput) {
            sendLog('Campos principais de registro nao encontrados', 'error');
            return;
        }
        const termsChecked = checkTermsIfPresent(form);
        sendLog(`Checkbox termos -> ${termsChecked ? 'acionado' : 'nao encontrado'}`);
        await delay(800);
        let regBtn = findVisible([
            'button.van-button.van-button--default.van-button--normal.btn.btncss1.fontColor13',
            'button.van-button.btn.btncss1.fontColor13',
            '.van-button.btn.btncss1.fontColor13',
            '.btn.btncss1.fontColor13',
        ]);
        if (!regBtn) regBtn = document.evaluate(
            "//*[self::button or self::div or self::span or self::a][contains(.,'Registrar') or contains(.,'Registro') or contains(.,'Register')]",
            form, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
        ).singleNodeValue;
        if (!regBtn) {
            regBtn = findVisible([
                'button[type="submit"]',
                '.login-register-btn',
                '.register-btn',
                '[class*="register"]',
                '[class*="btncss"]',
            ]);
        }
        if (regBtn) {
            sendLog(`Botao registrar detectado: ${regBtn.tagName}.${String(regBtn.className || '').trim()}`);
            forceActivateElement(regBtn);
            sendLog('Clique em registrar executado');
            await delay(250);
            await delay(700);
            if (confirmInput) {
                try {
                    confirmInput.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));
                    confirmInput.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true }));
                    sendLog('Enter enviado no campo confirmar senha.');
                } catch (_) {}
            }
        } else {
            sendLog('Botao registrar nao encontrado', 'warn');
        }
        sendLog('Aguardando redirecionamento apos cadastro...');
        let advanced = false;
        const waitAdvanceStarted = Date.now();
        while (Date.now() - waitAdvanceStarted < 9000) {
            if (registrationAdvanced()) {
                advanced = true;
                break;
            }
            await delay(400);
        }
        if (!advanced) {
            sendLog('Cadastro nao confirmou avancar apos clicar em Registrar.', 'error');
            return;
        }
        sendLog('Cadastro confirmado; iniciando etapa pos-registro.');
        sendAccountCaptured({
            account,
            phone,
            password,
            url: location.href,
        });
        watchChildLink();
        await delay(1200);
        await closeDownloadPopupIfVisible();

        let spinBtn = findVisible([
            '.spin_center',
            '[class*="spin_center"]',
            '[class*="spin-center"]',
            '[class*="turntable"] [class*="center"]',
            '[class*="wheel"] [class*="center"]',
        ]);
        if (!spinBtn) {
            const spinTexts = ['spin', 'girar', 'rodar', 'roleta', 'bonus', 'bÃ´nus', 'premio', 'prÃªmio', 'receber', 'coletar'];
            const maybeSpin = findFirstByText(spinTexts, ['button','div','span','a']);
            if (maybeSpin) spinBtn = maybeSpin;
        }
        if (!spinBtn) spinBtn = await waitFor('.spin_center', 12000);
        if (spinBtn) {
            sendLog('Botao spin_center encontrado, clicando...');
            forceActivateElement(spinBtn);
            await delay(3000);
            sendLog('Aguardando botao Confirmar aparecer...');
            let confirmBtn = findVisible([
                '.btn.btncss1.fontColor13',
                'button.van-button.van-button--default.van-button--normal.btn.btncss1.fontColor13',
                '.btncss1.fontColor13',
                '[class*="btncss1"]',
            ]);
            if (!confirmBtn) {
                confirmBtn = findFirstByText(
                    ['confirmar', 'confirm', 'ok', 'receber', 'coletar', 'claim'],
                    ['button','div','span','a']
                );
            }
            if (!confirmBtn) confirmBtn = await waitFor('.btn.btncss1.fontColor13', 15000);
            if (confirmBtn) {
                sendLog('Botao Confirmar encontrado, clicando...');
                forceActivateElement(confirmBtn);
                await delay(1000);
                sendLog('Clique em Confirmar executado');
                sendLog('Aguardando 3 segundos antes de clicar no jogo...');
                await delay(3000);
                const gameImageUrl = 'https://dz68qzgzvzly6.cloudfront.net/game_icons/IMG_5404__1_.png';
                const gameDiv = await waitForGameDiv(gameImageUrl, 15000);
                if (gameDiv) {
                    sendLog('Div do jogo encontrada, clicando...');
                    forceActivateElement(gameDiv);
                    await delay(2000);
                    sendLog('Clique no jogo executado');
                    sendLog('Aguardando botao Deposito aparecer...');
                    let depositBtn = findFirstByText(
                        ['deposito', 'depÃ³sito', 'deposit', 'recarregar', 'recarga', 'recharge'],
                        ['button','div','span','a']
                    );
                    if (!depositBtn) depositBtn = await waitFor('.btncss1.fontColor13', 15000);
                    if (depositBtn && String(depositBtn.textContent || '').includes('Dep')) {
                        sendLog('Botao Deposito encontrado, clicando...');
                        forceActivateElement(depositBtn);
                        await delay(1000);
                        sendLog('Clique em Deposito executado');
                    } else {
                        const depositBtn2 = document.evaluate(
                            "//div[contains(@class,'btncss1') and (contains(.,'Dep') or contains(.,'Deposit'))]",
                            document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null
                        ).singleNodeValue;
                        if (depositBtn2) {
                            forceActivateElement(depositBtn2);
                            sendLog('Clique em Deposito executado (XPath)');
                        } else {
                            sendLog('Botao Deposito nao encontrado', 'warn');
                        }
                    }
                    if (depositValues.length) {
                        await delay(1000);
                        const normalized = txt => String(txt || '').replace(/[^\d]/g, '');
                        let picked = false;
                        const candidates = Array.from(document.querySelectorAll('button,div,span,a'));
                        for (const wanted of depositValues) {
                            const wantedDigits = normalized(wanted);
                            if (!wantedDigits) continue;
                            const match = candidates.find(el => normalized(el.textContent || '') === wantedDigits && el.offsetParent !== null);
                            if (match) {
                                match.click();
                                sendLog(`Valor de deposito selecionado: ${wanted}`);
                                picked = true;
                                break;
                            }
                        }
                        if (!picked) sendLog('Nenhum valor de deposito configurado foi encontrado na tela.', 'warn');
                    }
                    await delay(1200);
                } else {
                    sendLog('Div do jogo nao encontrada', 'warn');
                }
            } else {
                sendLog('Botao Confirmar nao encontrado', 'warn');
            }
        } else {
            sendLog('Botao spin_center nao encontrado; seguindo fluxo sem rodada bonus.', 'warn');
        }
    } finally {
        window.__ltdf_layout_running__ = false;
    }
})();
""".strip()

SPEED_HACK_PAGE_SCRIPT = r"""
// LTDF speed bridge: estado global + proxy redundante de requestAnimationFrame.
window._ltdfSpeed = 1.0;
window._ltdfTargetSpeed = 1.0;
window._ltdfUserActivated = false;
window._ltdfReady = false;
window._ltdfSpeedConfig = { enabled: false, speed: 1.0 };

window.setSpeedConfig = function(val) {
  var s = parseFloat(val);
  if (!isNaN(s)) {
    s = Math.max(0.1, Math.min(4, s));
    window._ltdfSpeed = s;
    window._ltdfTargetSpeed = s;
    window._ltdfUserActivated = s > 1.0;
    window._ltdfReady = s > 1.0;
    window._ltdfSpeedConfig = { enabled: s > 1.0, speed: s };
    try {
      window.localStorage.setItem("ltdf_speed_value", String(s));
      window.localStorage.setItem("ltdf_speed_updated_at", String(Date.now()));
    } catch (_) {}
    try {
      var input = document.getElementById("ltdf-speed-value");
      if (!input && document.documentElement) {
        input = document.createElement("input");
        input.type = "hidden";
        input.id = "ltdf-speed-value";
        input.setAttribute("data-ltdf-speed", "1");
        document.documentElement.appendChild(input);
      }
      if (input) input.value = String(s);
    } catch (_) {}
  }
  return window._ltdfSpeed;
};

(function () {
  "use strict";
  if (window.__LTDF_TURBO_ACTIVE__) {
    try { console.warn("[WARN] Instancia do Turbo ja ativa nesta aba. Ignorando reinjecao."); } catch (_) {}
    return;
  }
  try { if (window.__LTDF_CHECK_INTERVAL__) window.clearInterval(window.__LTDF_CHECK_INTERVAL__); } catch (_) {}
  window.__LTDF_CHECK_INTERVAL__ = null;
  try { if (window.__ltdfNativeAutomationInterval) window.clearInterval(window.__ltdfNativeAutomationInterval); } catch (_) {}
  window.__ltdfNativeAutomationInterval = null;
  try { if (window.__ltdfNativeReobserveTimer) window.clearTimeout(window.__ltdfNativeReobserveTimer); } catch (_) {}
  window.__ltdfNativeReobserveTimer = null;
  try { if (window.__ltdfNativeAutomationObserver) window.__ltdfNativeAutomationObserver.disconnect(); } catch (_) {}
  window.__ltdfNativeAutomationObserver = null;
  try { if (window.__ltdfNativeBootstrapObserver) window.__ltdfNativeBootstrapObserver.disconnect(); } catch (_) {}
  window.__ltdfNativeBootstrapObserver = null;
  window.__LTDF_TURBO_ACTIVE__ = true;
  window.__LTDF_TURBO_VERSION__ = "singleton_anti_freeze_v1";
  if (window.__ltdfSpeedHackInstalled) return;
  window.__ltdfSpeedHackInstalled = true;
  window.__ltdfSpeedScriptId = "ltdf_silent_native_speed_v1";

  const normalizeConfig = (config) => {
    const speed = Math.max(0.1, Math.min(4, Number(config?.speed || 1) || 1));
    return {
      enabled: !!config?.enabled,
      speed,
      cbSetIntervalChecked: config?.cbSetIntervalChecked !== false,
      cbSetTimeoutChecked: config?.cbSetTimeoutChecked !== false,
      cbPerformanceNowChecked: config?.cbPerformanceNowChecked !== false,
      cbDateNowChecked: config?.cbDateNowChecked !== false,
      cbRequestAnimationFrameChecked: config?.cbRequestAnimationFrameChecked !== false,
    };
  };

  window.__ltdfSpeedDebug = {
    mode: "time-proxy",
    ready: false,
    speed: 1.0,
    targetSpeed: 1.0,
    userActivated: false,
    configCount: 0,
    rafProxyInstalled: false,
    rafProxyReapplyCount: 0,
    timeProxyInstalled: false,
    workerProxyInstalled: false,
    wasmProxyInstalled: false,
  };

  const nativeClock = window.__ltdfNativeClock || {
    performanceNow: performance && typeof performance.now === "function" ? performance.now.bind(performance) : null,
    dateNow: typeof Date.now === "function" ? Date.now.bind(Date) : null,
    setTimeout: typeof window.setTimeout === "function" ? window.setTimeout.bind(window) : null,
    setInterval: typeof window.setInterval === "function" ? window.setInterval.bind(window) : null,
    clearTimeout: typeof window.clearTimeout === "function" ? window.clearTimeout.bind(window) : null,
    clearInterval: typeof window.clearInterval === "function" ? window.clearInterval.bind(window) : null,
  };
  window.__ltdfNativeClock = nativeClock;
  const realNow = () => nativeClock.performanceNow ? nativeClock.performanceNow() : (nativeClock.dateNow ? nativeClock.dateNow() : Date.now());

  function effectiveSpeed(flagName) {
    const cfg = normalizeConfig(window._ltdfSpeedConfig || window.__ltdfSpeedInitialConfig || {});
    if (!cfg.enabled || cfg.speed <= 1.0 || cfg[flagName] === false) return 1.0;
    return cfg.speed;
  }

  function ensureTimeOrigin() {
    if (!window.__ltdfTimeOrigin) {
      const perf = realNow();
      const date = nativeClock.dateNow ? nativeClock.dateNow() : Math.floor(perf);
      window.__ltdfTimeOrigin = { perfReal: perf, perfVirtual: perf, dateReal: date, dateVirtual: date };
    }
    return window.__ltdfTimeOrigin;
  }

  function acceleratedPerfNow() {
    const origin = ensureTimeOrigin();
    const speed = effectiveSpeed("cbPerformanceNowChecked");
    const current = realNow();
    return speed > 1.0 ? origin.perfVirtual + ((current - origin.perfReal) * speed) : current;
  }

  function acceleratedDateNow() {
    const origin = ensureTimeOrigin();
    const speed = effectiveSpeed("cbDateNowChecked");
    const current = nativeClock.dateNow ? nativeClock.dateNow() : Math.floor(realNow());
    return Math.floor(speed > 1.0 ? origin.dateVirtual + ((current - origin.dateReal) * speed) : current);
  }

  function scaledDelay(delay, flagName) {
    const ms = Math.max(0, Number(delay || 0) || 0);
    const speed = effectiveSpeed(flagName);
    return speed > 1.0 ? Math.max(0, ms / speed) : ms;
  }

  function rebaseTimeOrigin() {
    try {
      const origin = ensureTimeOrigin();
      origin.perfVirtual = acceleratedPerfNow();
      origin.perfReal = realNow();
      origin.dateVirtual = acceleratedDateNow();
      origin.dateReal = nativeClock.dateNow ? nativeClock.dateNow() : Math.floor(origin.perfReal);
    } catch (_) {}
  }

  function installTimeProxies(source) {
    try {
      ensureTimeOrigin();
      if (nativeClock.performanceNow && effectiveSpeed("cbPerformanceNowChecked") > 1.0) {
        try {
          Object.defineProperty(performance, "now", { configurable: true, writable: true, value: acceleratedPerfNow });
        } catch (_) {
          try { performance.now = acceleratedPerfNow; } catch (_) {}
        }
      }
      if (nativeClock.dateNow && effectiveSpeed("cbDateNowChecked") > 1.0) {
        try { Date.now = acceleratedDateNow; } catch (_) {}
      }
      if (nativeClock.setTimeout && effectiveSpeed("cbSetTimeoutChecked") > 1.0) {
        window.setTimeout = function(handler, timeout, ...args) {
          return nativeClock.setTimeout(handler, scaledDelay(timeout, "cbSetTimeoutChecked"), ...args);
        };
      }
      if (nativeClock.setInterval && effectiveSpeed("cbSetIntervalChecked") > 1.0) {
        window.setInterval = function(handler, timeout, ...args) {
          return nativeClock.setInterval(handler, scaledDelay(timeout, "cbSetIntervalChecked"), ...args);
        };
      }
      window.__ltdfSpeedDebug.timeProxyInstalled = true;
      window.__ltdfSpeedDebug.timeProxySource = source || "install";
      return true;
    } catch (_) {
      return false;
    }
  }

  function installRafProxy(source) {
    try {
      const cfg = normalizeConfig(window._ltdfSpeedConfig || window.__ltdfSpeedInitialConfig || {});
      if (!cfg.enabled || cfg.speed <= 1.0 || cfg.cbRequestAnimationFrameChecked === false) return false;
      if (window.__ltdfNativeRequestAnimationFrame && window.requestAnimationFrame === window.__ltdfRafProxy) return true;
      const currentRaf = window.requestAnimationFrame;
      if (typeof currentRaf !== "function") return false;
      if (!window.__ltdfNativeRequestAnimationFrame || currentRaf !== window.__ltdfRafProxy) {
        window.__ltdfNativeRequestAnimationFrame = currentRaf.bind(window);
      }
      if (!window.__ltdfNativeCancelAnimationFrame && typeof window.cancelAnimationFrame === "function") {
        window.__ltdfNativeCancelAnimationFrame = window.cancelAnimationFrame.bind(window);
      }
      window.__ltdfRafOriginReal = 0;
      window.__ltdfRafOriginVirtual = 0;
      window.__ltdfRafProxy = function(callback) {
        return window.__ltdfNativeRequestAnimationFrame(function(timestamp) {
          try {
            const speed = effectiveSpeed("cbRequestAnimationFrameChecked");
            if (!window.__ltdfRafOriginReal || speed <= 1.0) {
              window.__ltdfRafOriginReal = timestamp;
              window.__ltdfRafOriginVirtual = timestamp;
            }
            const acceleratedTimestamp = speed > 1.0
              ? window.__ltdfRafOriginVirtual + ((timestamp - window.__ltdfRafOriginReal) * speed)
              : timestamp;
            callback(acceleratedTimestamp);
          } catch (_) {
            callback(timestamp);
          }
        });
      };
      window.__ltdfRafProxy.__ltdfRafProxy = true;
      window.requestAnimationFrame = window.__ltdfRafProxy;
      window.__ltdfSpeedDebug.rafProxyInstalled = true;
      window.__ltdfSpeedDebug.rafProxySource = source || "install";
      window.__ltdfSpeedDebug.rafProxyReapplyCount += 1;
      return true;
    } catch (_) {
      return false;
    }
  }

  const workerBootstrap = `
(function () {
  if (self.__ltdfWorkerSpeedInstalled) return;
  self.__ltdfWorkerSpeedInstalled = true;
  self._ltdfSpeedConfig = ${JSON.stringify(window._ltdfSpeedConfig || window.__ltdfSpeedInitialConfig || { enabled:false, speed:1.0 })};
  const normalizeConfig = (config) => {
    const speed = Math.max(0.1, Math.min(4, Number(config && config.speed || 1) || 1));
    return {
      enabled: !!(config && config.enabled),
      speed,
      cbSetIntervalChecked: !config || config.cbSetIntervalChecked !== false,
      cbSetTimeoutChecked: !config || config.cbSetTimeoutChecked !== false,
      cbPerformanceNowChecked: !config || config.cbPerformanceNowChecked !== false,
      cbDateNowChecked: !config || config.cbDateNowChecked !== false,
    };
  };
  const nativeClock = {
    performanceNow: self.performance && typeof self.performance.now === "function" ? self.performance.now.bind(self.performance) : null,
    dateNow: typeof Date.now === "function" ? Date.now.bind(Date) : null,
    setTimeout: typeof self.setTimeout === "function" ? self.setTimeout.bind(self) : null,
    setInterval: typeof self.setInterval === "function" ? self.setInterval.bind(self) : null,
  };
  const realNow = () => nativeClock.performanceNow ? nativeClock.performanceNow() : (nativeClock.dateNow ? nativeClock.dateNow() : Date.now());
  const effectiveSpeed = (flagName) => {
    const cfg = normalizeConfig(self._ltdfSpeedConfig || {});
    if (!cfg.enabled || cfg.speed <= 1.0 || cfg[flagName] === false) return 1.0;
    return cfg.speed;
  };
  const origin = { perfReal: realNow(), perfVirtual: realNow(), dateReal: nativeClock.dateNow ? nativeClock.dateNow() : Date.now(), dateVirtual: nativeClock.dateNow ? nativeClock.dateNow() : Date.now() };
  const acceleratedPerfNow = () => {
    const speed = effectiveSpeed("cbPerformanceNowChecked");
    const current = realNow();
    return speed > 1.0 ? origin.perfVirtual + ((current - origin.perfReal) * speed) : current;
  };
  const acceleratedDateNow = () => {
    const speed = effectiveSpeed("cbDateNowChecked");
    const current = nativeClock.dateNow ? nativeClock.dateNow() : Math.floor(realNow());
    return Math.floor(speed > 1.0 ? origin.dateVirtual + ((current - origin.dateReal) * speed) : current);
  };
  const scaledDelay = (delay, flagName) => {
    const ms = Math.max(0, Number(delay || 0) || 0);
    const speed = effectiveSpeed(flagName);
    return speed > 1.0 ? Math.max(0, ms / speed) : ms;
  };
  try { Object.defineProperty(self.performance, "now", { configurable:true, writable:true, value:acceleratedPerfNow }); } catch (_) { try { self.performance.now = acceleratedPerfNow; } catch (_) {} }
  try { Date.now = acceleratedDateNow; } catch (_) {}
  if (nativeClock.setTimeout) self.setTimeout = function(handler, timeout, ...args) { return nativeClock.setTimeout(handler, scaledDelay(timeout, "cbSetTimeoutChecked"), ...args); };
  if (nativeClock.setInterval) self.setInterval = function(handler, timeout, ...args) { return nativeClock.setInterval(handler, scaledDelay(timeout, "cbSetIntervalChecked"), ...args); };
  self.addEventListener("message", (event) => {
    if (event && event.data && event.data.__ltdfSpeedWorkerConfig) self._ltdfSpeedConfig = event.data.config || self._ltdfSpeedConfig;
  });
})();`;

  function installWorkerProxy(source) {
    try {
      if (window.__ltdfNativeWorker || typeof window.Worker !== "function") return true;
      window.__ltdfNativeWorker = window.Worker;
      window.Worker = new Proxy(window.__ltdfNativeWorker, {
        construct(target, args) {
          try {
            const workerUrl = args[0];
            const options = args[1];
            const resolvedWorkerUrl = new URL(String(workerUrl), location.href).href;
            const isModuleWorker = options && String(options.type || "").toLowerCase() === "module";
            const sourceCode = isModuleWorker
              ? `${workerBootstrap}\nimport ${JSON.stringify(resolvedWorkerUrl)};`
              : `${workerBootstrap}\ntry { importScripts(${JSON.stringify(resolvedWorkerUrl)}); } catch (error) { throw error; }`;
            const blob = new Blob([sourceCode], { type: "application/javascript" });
            const blobUrl = URL.createObjectURL(blob);
            const worker = options === undefined ? Reflect.construct(target, [blobUrl]) : Reflect.construct(target, [blobUrl, options]);
            try { worker.postMessage({ __ltdfSpeedWorkerConfig: true, config: window._ltdfSpeedConfig || window.__ltdfSpeedInitialConfig || {} }); } catch (_) {}
            nativeClock.setTimeout(() => { try { URL.revokeObjectURL(blobUrl); } catch (_) {} }, 30000);
            return worker;
          } catch (_) {
            const worker = Reflect.construct(target, args);
            try { worker.postMessage({ __ltdfSpeedWorkerConfig: true, config: window._ltdfSpeedConfig || window.__ltdfSpeedInitialConfig || {} }); } catch (_) {}
            return worker;
          }
        },
      });
      window.__ltdfSpeedDebug.workerProxyInstalled = true;
      window.__ltdfSpeedDebug.workerProxySource = source || "install";
      return true;
    } catch (_) {
      return false;
    }
  }

  function patchWasmImports(imports) {
    try {
      if (!imports || typeof imports !== "object") return imports;
      const patched = Array.isArray(imports) ? imports.slice() : { ...imports };
      for (const namespaceKey of Object.keys(patched)) {
        const namespace = patched[namespaceKey];
        if (!namespace || typeof namespace !== "object") continue;
        const namespaceCopy = { ...namespace };
        for (const key of Object.keys(namespaceCopy)) {
          if (typeof namespaceCopy[key] !== "function") continue;
          const label = `${namespaceKey}.${key}`.toLowerCase();
          if (/(now|time|date|clock|timestamp|performance)/.test(label)) {
            namespaceCopy[key] = function(...args) {
              const value = /date|unix|epoch|timestamp/.test(label) ? acceleratedDateNow() : acceleratedPerfNow();
              return Number.isInteger(value) ? value : +value;
            };
          }
        }
        patched[namespaceKey] = namespaceCopy;
      }
      return patched;
    } catch (_) {
      return imports;
    }
  }

  function installWasmProxy(source) {
    try {
      if (!window.WebAssembly || window.__ltdfWasmProxyInstalled) return true;
      const nativeInstantiate = WebAssembly.instantiate ? WebAssembly.instantiate.bind(WebAssembly) : null;
      const nativeInstantiateStreaming = WebAssembly.instantiateStreaming ? WebAssembly.instantiateStreaming.bind(WebAssembly) : null;
      if (nativeInstantiate) {
        WebAssembly.instantiate = function(moduleOrBytes, imports) {
          return nativeInstantiate(moduleOrBytes, patchWasmImports(imports));
        };
      }
      if (nativeInstantiateStreaming) {
        WebAssembly.instantiateStreaming = function(sourcePromise, imports) {
          return nativeInstantiateStreaming(sourcePromise, patchWasmImports(imports));
        };
      }
      window.__ltdfWasmProxyInstalled = true;
      window.__ltdfSpeedDebug.wasmProxyInstalled = true;
      window.__ltdfSpeedDebug.wasmProxySource = source || "install";
      return true;
    } catch (_) {
      return false;
    }
  }

  function ltdfIsVisible(el) {
    try {
      if (!el || !el.isConnected) return false;
      const rect = el.getBoundingClientRect();
      const style = getComputedStyle(el);
      return rect.width > 2 && rect.height > 2 && style.visibility !== "hidden" && style.display !== "none" && style.opacity !== "0";
    } catch (_) {
      return false;
    }
  }

  function ltdfNorm(text) {
    try {
      return String(text || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
    } catch (_) {
      return String(text || "").toLowerCase();
    }
  }

  function ltdfDispatchClick(el, source) {
    if (!el) return false;
    try { el.scrollIntoView({ block: "center", inline: "center" }); } catch (_) {}
    const rect = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
    const x = rect ? Math.round(rect.left + rect.width / 2) : Math.round(innerWidth / 2);
    const y = rect ? Math.round(rect.top + rect.height / 2) : Math.round(innerHeight / 2);
    const pointerOpts = { bubbles:true, cancelable:true, composed:true, clientX:x, clientY:y, screenX:x, screenY:y, button:0, buttons:1, pointerId:1, pointerType:"mouse", isPrimary:true, view:window };
    const mouseDown = { bubbles:true, cancelable:true, composed:true, clientX:x, clientY:y, screenX:x, screenY:y, button:0, buttons:1, view:window };
    const mouseUp = { ...mouseDown, buttons:0 };
    try { if (typeof el.focus === "function") el.focus({ preventScroll:true }); } catch (_) {}
    try { el.dispatchEvent(new PointerEvent("pointerdown", pointerOpts)); } catch (_) {}
    try { el.dispatchEvent(new MouseEvent("mousedown", mouseDown)); } catch (_) {}
    try { el.dispatchEvent(new PointerEvent("pointerup", { ...pointerOpts, buttons:0 })); } catch (_) {}
    try { el.dispatchEvent(new MouseEvent("mouseup", mouseUp)); } catch (_) {}
    try { el.dispatchEvent(new MouseEvent("click", mouseUp)); } catch (_) {}
    try { if (typeof el.click === "function") el.click(); } catch (_) {}
    try {
      window.__ltdfSpeedDebug.lastNativeClick = { source: source || "native", tag: el.tagName || "", cls: String(el.className || ""), at: Date.now() };
    } catch (_) {}
    return true;
  }

  function ltdfFindBySelectors(selectors) {
    for (const selector of selectors) {
      try {
        for (const el of Array.from(document.querySelectorAll(selector))) {
          if (ltdfIsVisible(el)) return el;
        }
      } catch (_) {}
    }
    return null;
  }

  function ltdfFindByText(tokens, selectors) {
    const wanted = tokens.map(ltdfNorm);
    for (const selector of selectors) {
      try {
        for (const el of Array.from(document.querySelectorAll(selector))) {
          if (!ltdfIsVisible(el)) continue;
          const text = ltdfNorm(`${el.textContent || ""} ${el.getAttribute("aria-label") || ""} ${el.getAttribute("title") || ""} ${el.className || ""} ${el.id || ""}`);
          if (wanted.some((token) => text.includes(token))) return el;
        }
      } catch (_) {}
    }
    return null;
  }

  function ltdfIsActiveToggle(el) {
    if (!el) return false;
    try {
      if (el.matches && el.matches("input[type='checkbox']")) return !!el.checked;
      const text = ltdfNorm(`${el.className || ""} ${el.getAttribute("aria-pressed") || ""} ${el.getAttribute("aria-checked") || ""} ${el.getAttribute("data-state") || ""}`);
      return /\b(active|ativo|on|true|checked|selected|selecionado|turbo-on|fast-on)\b/.test(text);
    } catch (_) {
      return false;
    }
  }

  function ltdfFindTurboControl() {
    const selectors = [
      "#turbo", "#fastPlay", "#fast-play", "#quickSpin", "#quick-spin",
      "[data-testid*='turbo' i]", "[data-testid*='fast' i]", "[data-testid*='quick' i]",
      "[class*='turbo' i]", "[class*='fast' i]", "[class*='quick' i]",
      "[id*='turbo' i]", "[id*='fast' i]", "[id*='quick' i]",
      "button[aria-label*='turbo' i]", "button[aria-label*='fast' i]", "button[aria-label*='quick' i]",
      "input[type='checkbox'][name*='turbo' i]", "input[type='checkbox'][name*='fast' i]",
    ];
    return ltdfFindBySelectors(selectors)
      || ltdfFindByText(["turbo", "fast play", "fastplay", "quick spin", "quickspin", "rapido", "rapida", "acelerar"], ["button", "div", "span", "a", "label", "input"]);
  }

  function ltdfActivateNativeTurbo(source) {
    try {
      const turbo = ltdfFindTurboControl();
      if (!turbo) return { ok:false, reason:"turbo_not_found" };
      if (turbo.matches && turbo.matches("input[type='checkbox']") && !turbo.checked) {
        turbo.checked = true;
        turbo.dispatchEvent(new Event("input", { bubbles:true }));
        turbo.dispatchEvent(new Event("change", { bubbles:true }));
      }
      if (!ltdfIsActiveToggle(turbo)) ltdfDispatchClick(turbo, source || "turbo");
      window.__ltdfNativeTurboEnabled = true;
      window.__ltdfSpeedDebug.nativeTurbo = { ok:true, source: source || "turbo", at: Date.now(), tag: turbo.tagName || "", cls: String(turbo.className || "") };
      return { ok:true, tag: turbo.tagName || "", cls: String(turbo.className || "") };
    } catch (error) {
      return { ok:false, reason:String(error) };
    }
  }

  function ltdfFindActionButton() {
    const selectors = [
      ".spin_center", "[class*='spin_center' i]", "[class*='spin-center' i]",
      "[class*='spin' i]", "[class*='bet' i]", "[class*='play' i]",
      "button[data-testid*='spin' i]", "button[data-testid*='play' i]",
      "button[aria-label*='spin' i]", "button[aria-label*='play' i]",
    ];
    return ltdfFindBySelectors(selectors)
      || ltdfFindByText(["spin", "girar", "rodar", "jogar", "apostar", "play", "start"], ["button", "div", "span", "a"]);
  }

  function ltdfIsActionReady(el) {
    if (!ltdfIsVisible(el)) return false;
    try {
      const text = ltdfNorm(`${el.className || ""} ${el.getAttribute("disabled") || ""} ${el.getAttribute("aria-disabled") || ""} ${el.getAttribute("aria-busy") || ""} ${el.getAttribute("data-state") || ""} ${el.getAttribute("data-status") || ""}`);
      if (el.disabled || el.getAttribute("aria-disabled") === "true") return false;
      if (el.getAttribute("aria-busy") === "true") return false;
      if (/\b(disabled|desabilitado|loading|loaded-false|spinning|girando|busy|locked|bloqueado|animating|animation|transition|transitioning|entering|leaving|pending|processing|processando)\b/.test(text)) return false;
      const style = getComputedStyle(el);
      if (style.pointerEvents === "none") return false;
      return true;
    } catch (_) {
      return false;
    }
  }

  function ltdfClickActionWhenReady(source) {
    if (!window.__ltdfNativeAutomationReady) return { ok:false, reason:"native_ui_not_ready" };
    const button = ltdfFindActionButton();
    if (!ltdfIsActionReady(button)) return { ok:false, reason:"action_not_ready" };
    const now = Date.now();
    if (window.__ltdfReadyActionClickPending) return { ok:false, reason:"ready_click_pending" };
    if (window.__ltdfLastReadyActionClickAt && now - window.__ltdfLastReadyActionClickAt < 120) {
      return { ok:false, reason:"ready_click_throttled" };
    }
    window.__ltdfReadyActionClickPending = true;
    nativeClock.setTimeout(() => {
      window.__ltdfReadyActionClickPending = false;
      const freshButton = ltdfFindActionButton();
      if (!ltdfIsActionReady(freshButton)) return;
      const clickAt = Date.now();
      if (window.__ltdfLastReadyActionClickAt && clickAt - window.__ltdfLastReadyActionClickAt < 120) return;
      window.__ltdfLastReadyActionClickAt = clickAt;
      ltdfDispatchClick(freshButton, source || "ready_action");
      try {
        window.__ltdfSpeedDebug.lastReadyActionClick = { source: source || "ready_action", at: clickAt, tag: freshButton.tagName || "", cls: String(freshButton.className || ""), debounced:true };
      } catch (_) {}
    }, 8);
    return { ok:true, queued:true, debounceMs:8, tag: button.tagName || "", cls: String(button.className || "") };
  }

  function ltdfScheduleReadyClick(source) {
    if (!window.__ltdfNativeAutomationReady) return;
    if (!window.__ltdfNativeTurboEnabled && !window._ltdfUserActivated) return;
    const attempts = [0, 35, 75, 140, 240];
    attempts.forEach((delay) => nativeClock.setTimeout(() => ltdfClickActionWhenReady(source || "server_payload"), delay));
  }

  function ltdfGetPersistentObserverRoot() {
    return document.documentElement || document.body || document;
  }

  function ltdfFindMainGameTarget() {
    try {
      const selector = [
        ".spin_center", "[class*='spin_center' i]", "[class*='spin-center' i]",
        "button[data-testid*='spin' i]", "button[data-testid*='play' i]",
        "button[aria-label*='spin' i]", "button[aria-label*='play' i]",
        "[class*='bet' i]", "[class*='play' i]"
      ].join(",");
      const el = document.querySelector(selector);
      return ltdfIsVisible(el) ? el : null;
    } catch (_) {
      return null;
    }
  }

  function ltdfHasMainGameInterface() {
    try {
      return !!ltdfFindMainGameTarget();
    } catch (_) {
      return false;
    }
  }

  function ltdfRunNativeAutomationTick(source) {
    if (!window.__ltdfNativeAutomationReady) return false;
    try { ltdfActivateNativeTurbo(source || "tick"); } catch (_) {}
    try { ltdfClickActionWhenReady(source || "tick"); } catch (_) {}
    return true;
  }

  function ltdfScheduleNativeAutomationTick(source, delay) {
    if (!window.__ltdfNativeAutomationReady) return;
    if (window.__ltdfNativeAutomationTickPending) return;
    window.__ltdfNativeAutomationTickPending = true;
    nativeClock.setTimeout(() => {
      window.__ltdfNativeAutomationTickPending = false;
      ltdfRunNativeAutomationTick(source || "scheduled_tick");
    }, typeof delay === "number" ? delay : 80);
  }

  function ltdfClearNativeAutomationTimers() {
    try { if (window.__LTDF_CHECK_INTERVAL__) nativeClock.clearInterval(window.__LTDF_CHECK_INTERVAL__); } catch (_) {}
    window.__LTDF_CHECK_INTERVAL__ = null;
    try { if (window.__ltdfNativeAutomationInterval) nativeClock.clearInterval(window.__ltdfNativeAutomationInterval); } catch (_) {}
    window.__ltdfNativeAutomationInterval = null;
    try { if (window.__ltdfNativeReobserveTimer) nativeClock.clearTimeout(window.__ltdfNativeReobserveTimer); } catch (_) {}
    window.__ltdfNativeReobserveTimer = null;
  }

  function ltdfEnsureNativeActiveLoops() {
    ltdfClearNativeAutomationTimers();
    window.__ltdfNativeActiveLoopsInstalled = true;
    try {
      try { if (window.__ltdfNativeAutomationObserver) window.__ltdfNativeAutomationObserver.disconnect(); } catch (_) {}
      const options = { childList:true, subtree:true, attributes:true, attributeFilter:["class", "disabled", "aria-disabled", "aria-busy", "data-state", "data-status"] };
      let observer = null;
      const reobserve = () => {
        if (!window.__ltdfNativeAutomationReady || !observer) return;
        try { observer.observe(ltdfGetPersistentObserverRoot(), options); } catch (_) {}
      };
      observer = new MutationObserver(() => {
        if (window.__ltdfNativeObserverBusy) return;
        window.__ltdfNativeObserverBusy = true;
        try { observer.disconnect(); } catch (_) {}
        nativeClock.setTimeout(() => {
          try {
            // O alvo e buscado de novo dentro do tick; nao reusa node antigo.
            ltdfScheduleNativeAutomationTick("button_ready_mutation", 80);
          } finally {
            window.__ltdfNativeObserverBusy = false;
            window.__ltdfNativeReobserveTimer = nativeClock.setTimeout(reobserve, 260);
          }
        }, 120);
      });
      observer.observe(ltdfGetPersistentObserverRoot(), options);
      window.__ltdfNativeAutomationObserver = observer;
    } catch (_) {}
  }

  function ltdfArmNativeBootstrap(source) {
    if (window.__ltdfNativeBootstrapArmed) return false;
    window.__ltdfNativeBootstrapArmed = true;
    window.__ltdfSpeedDebug.nativeAutomationWaiting = true;
    window.__ltdfSpeedDebug.nativeAutomationWaitSource = source || "bootstrap";

    const tryStart = (reason) => {
      if (window.__ltdfNativeAutomationReady) return true;
      const target = ltdfFindMainGameTarget();
      if (!target) {
        window.__ltdfSpeedDebug.nativeAutomationWaiting = true;
        return false;
      }
      window.__ltdfNativeAutomationReady = true;
      window.__ltdfSpeedDebug.nativeAutomationWaiting = false;
      window.__ltdfSpeedDebug.nativeAutomationReady = true;
      window.__ltdfSpeedDebug.nativeAutomationReadySource = reason || source || "ready";
      window.__ltdfSpeedDebug.nativeAutomationReadyTarget = { tag: target.tagName || "", cls: String(target.className || "") };
      try { if (window.__ltdfNativeBootstrapObserver) window.__ltdfNativeBootstrapObserver.disconnect(); } catch (_) {}
      window.__ltdfNativeBootstrapObserver = null;
      ltdfEnsureNativeActiveLoops();
      ltdfRunNativeAutomationTick(reason || "bootstrap_ready");
      return true;
    };

    try {
      let pending = false;
      let observer = null;
      const reobserve = () => {
        if (window.__ltdfNativeAutomationReady || !observer) return;
        try { observer.observe(ltdfGetPersistentObserverRoot(), { childList:true, subtree:true }); } catch (_) {}
      };
      const scheduleTryStart = (reason, delay) => {
        if (pending || window.__ltdfNativeAutomationReady) return;
        pending = true;
        nativeClock.setTimeout(() => {
          try {
            tryStart(reason);
          } finally {
            pending = false;
            if (!window.__ltdfNativeAutomationReady) {
              nativeClock.setTimeout(reobserve, 260);
            }
          }
        }, typeof delay === "number" ? delay : 0);
      };
      observer = new MutationObserver(() => {
        try { observer.disconnect(); } catch (_) {}
        scheduleTryStart("bootstrap_mutation", 150);
      });
      observer.observe(ltdfGetPersistentObserverRoot(), { childList:true, subtree:true });
      window.__ltdfNativeBootstrapObserver = observer;
      scheduleTryStart(source || "bootstrap_async_initial", 0);
    } catch (_) {}
    return false;
  }

  function installNativeAutomation(source) {
    try {
      ltdfClearNativeAutomationTimers();
      if (window.__ltdfNativeAutomationInstalled) {
        if (!window.__ltdfNativeAutomationReady && window.__ltdfNativeBootstrapArmed && !window.__ltdfNativeBootstrapObserver) {
          window.__ltdfNativeBootstrapArmed = false;
        }
        if (!window.__ltdfNativeAutomationReady) return ltdfArmNativeBootstrap(source || "reapply");
        ltdfScheduleNativeAutomationTick(source || "reapply", 80);
        return true;
      }
      window.__ltdfNativeAutomationInstalled = true;
      if (typeof window.WebSocket === "function" && !window.__ltdfNativeWebSocket) {
        window.__ltdfNativeWebSocket = window.WebSocket;
        window.WebSocket = new Proxy(window.__ltdfNativeWebSocket, {
          construct(target, args) {
            const socket = Reflect.construct(target, args);
            try {
              socket.addEventListener("message", () => {
                window.__ltdfLastServerPayloadAt = Date.now();
                ltdfScheduleReadyClick("ws_payload");
              });
            } catch (_) {}
            return socket;
          },
        });
      }
      window.__ltdfSpeedDebug.nativeAutomationInstalled = true;
      window.__ltdfSpeedDebug.nativeAutomationSource = source || "install";
      return ltdfArmNativeBootstrap(source || "install");
    } catch (_) {
      return false;
    }
  }

  window.__ltdfActivateNativeTurbo = ltdfActivateNativeTurbo;
  window.__ltdfClickActionWhenReady = ltdfClickActionWhenReady;
  window.__ltdfHasMainGameInterface = ltdfHasMainGameInterface;
  window.__ltdfGetPersistentObserverRoot = ltdfGetPersistentObserverRoot;

  window.__ltdfApplySpeedConfig = function(config, source) {
    const next = normalizeConfig(config || {});
    rebaseTimeOrigin();
    window._ltdfSpeedConfig = next;
    window.setSpeedConfig(next.enabled ? next.speed : 1.0);
    installTimeProxies(source || "apply");
    installRafProxy(source || "apply");
    installWorkerProxy(source || "apply");
    installWasmProxy(source || "apply");
    installNativeAutomation(source || "apply");
    window.__ltdfSpeedDebug.ready = window._ltdfReady;
    window.__ltdfSpeedDebug.speed = window._ltdfSpeed;
    window.__ltdfSpeedDebug.targetSpeed = window._ltdfTargetSpeed;
    window.__ltdfSpeedDebug.userActivated = window._ltdfUserActivated;
    window.__ltdfSpeedDebug.configCount += 1;
    window.__ltdfSpeedDebug.source = source || "direct";
    return {
      ok: true,
      mode: "time-proxy",
      speed: window._ltdfSpeed,
      targetSpeed: window._ltdfTargetSpeed,
      ready: window._ltdfReady,
      userActivated: window._ltdfUserActivated,
      config: window._ltdfSpeedConfig,
    };
  };

  function ltdfLateSpeedReapply(source) {
    try {
      const cfg = window._ltdfSpeedConfig || window.__ltdfSpeedInitialConfig || { enabled: false, speed: 1.0 };
      if (typeof window.__ltdfApplySpeedConfig === "function") {
        window.__ltdfApplySpeedConfig(cfg, source || "late_reapply");
      }
      installTimeProxies(source || "late_reapply");
      installRafProxy(source || "late_reapply");
      installWorkerProxy(source || "late_reapply");
      installWasmProxy(source || "late_reapply");
      installNativeAutomation(source || "late_reapply");
    } catch (_) {}
  }

  window.__ltdfForceSpeedReapply = ltdfLateSpeedReapply;
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => ltdfLateSpeedReapply("dom_content_loaded"), { once: true });
  } else {
    setTimeout(() => ltdfLateSpeedReapply("document_already_ready"), 0);
  }
  nativeClock.setTimeout(() => ltdfLateSpeedReapply("late_250ms"), 250);
  nativeClock.setTimeout(() => ltdfLateSpeedReapply("late_1000ms"), 1000);
  const rafFallbackStartedAt = nativeClock.dateNow ? nativeClock.dateNow() : Date.now();
  const rafFallbackTimer = nativeClock.setInterval(() => {
    ltdfLateSpeedReapply("raf_interval_guard");
    const now = nativeClock.dateNow ? nativeClock.dateNow() : Date.now();
    if (now - rafFallbackStartedAt > 5000) nativeClock.clearInterval(rafFallbackTimer);
  }, 200);

  window.addEventListener("message", (event) => {
    if (event.source !== window || !event.data) return;
    if (event.data.command === "setSpeedConfig") {
      window.__ltdfApplySpeedConfig(event.data.config || {}, "postMessage");
      return;
    }
    if (event.data.command === "getSpeedConfig") {
      window.postMessage({ command: "currentSpeedConfig", config: window._ltdfSpeedConfig, __ltdfSpeed__: true }, "*");
    }
  });
})();
""".strip()
SPEED_BRIDGE_JS = r"""
(function () {
  if (window.__ltdfSpeedBridgeInstalled) return;
  window.__ltdfSpeedBridgeInstalled = true;

  const DEFAULT_SPEED_CONFIG = __DEFAULT_SPEED_CONFIG__;

  function normalizeSpeedConfig(config) {
    const speed = Math.max(0.1, Math.min(4, Number(config?.speed || 1) || 1));
    return {
      enabled: !!config?.enabled,
      speed,
      cbSetIntervalChecked: config?.cbSetIntervalChecked !== false,
      cbSetTimeoutChecked: config?.cbSetTimeoutChecked !== false,
      cbPerformanceNowChecked: config?.cbPerformanceNowChecked !== false,
      cbDateNowChecked: config?.cbDateNowChecked !== false,
      cbRequestAnimationFrameChecked: config?.cbRequestAnimationFrameChecked !== false,
    };
  }

  function postConfig(config) {
    try {
      window.postMessage({ command: 'setSpeedConfig', config: normalizeSpeedConfig(config || DEFAULT_SPEED_CONFIG) }, '*');
    } catch (_) {}
  }

  try {
    chrome.storage.local.get(['ltdf_speed_config'], (stored) => {
      postConfig(stored?.ltdf_speed_config || DEFAULT_SPEED_CONFIG);
    });
  } catch (_) {
    postConfig(DEFAULT_SPEED_CONFIG);
  }

  try {
    chrome.runtime.onMessage.addListener((msg) => {
      if (msg && msg.action === 'speed_config_update') {
        postConfig(msg.config || DEFAULT_SPEED_CONFIG);
      }
    });
  } catch (_) {}
})();
""".strip()

BACKGROUND_JS_TEMPLATE = r"""
const WS_URLS = __WS_URLS__;
const SESSION_TOKEN = '__SESSION_ID__';
const ROLE_MODE = '__ROLE_MODE__';
const DEFAULT_SPEED_CONFIG = __DEFAULT_SPEED_CONFIG__;
let SESSION_ID = SESSION_TOKEN && SESSION_TOKEN !== '__SESSION_ID__' && SESSION_TOKEN !== '__AUTO__'
    ? SESSION_TOKEN
    : null;
let ws = null;
const _pending = [];
let _masterTabId = null;
const _installSourceByHost = {};
let _connectIndex = 0;
let _activeWsUrl = null;
let _reconnectTimer = null;
let _speedConfig = DEFAULT_SPEED_CONFIG;

function ensureSessionId() {
    return new Promise((resolve) => {
        if (SESSION_ID) {
            chrome.storage.local.set({ ltdf_session_id: SESSION_ID }, () => resolve(SESSION_ID));
            return;
        }
        chrome.storage.local.get(['ltdf_session_id'], (stored) => {
            SESSION_ID = stored?.ltdf_session_id;
            if (!SESSION_ID) {
                SESSION_ID = (crypto?.randomUUID && crypto.randomUUID()) || `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
                chrome.storage.local.set({ ltdf_session_id: SESSION_ID }, () => resolve(SESSION_ID));
                return;
            }
            resolve(SESSION_ID);
        });
    });
}

function normalizeSpeedConfig(config) {
    const speed = Math.max(0.1, Math.min(4, Number(config?.speed || 1) || 1));
    return {
        enabled: !!config?.enabled,
        speed,
        cbSetIntervalChecked: config?.cbSetIntervalChecked !== false,
        cbSetTimeoutChecked: config?.cbSetTimeoutChecked !== false,
        cbPerformanceNowChecked: config?.cbPerformanceNowChecked !== false,
        cbDateNowChecked: config?.cbDateNowChecked !== false,
        cbRequestAnimationFrameChecked: config?.cbRequestAnimationFrameChecked !== false,
    };
}

function serializePayload(obj) {
    if (obj && typeof obj === 'object' && !obj.session_id) obj = { ...obj, session_id: SESSION_ID || 'default' };
    return typeof obj === 'string' ? obj : JSON.stringify(obj);
}

function registerSession() {
    return sendWS({ action: 'register_session', domain: 'extension-background', role: ROLE_MODE === 'slave_only' ? 'slave' : 'master' });
}

function sendHeartbeat() {
    const speed = Math.max(1, Number(_speedConfig?.speed || 1) || 1);
    return sendWS({
        action: 'heartbeat',
        domain: 'extension-background',
        role: ROLE_MODE === 'slave_only' ? 'slave' : 'master',
        activeUrl: _activeWsUrl,
        speed,
        ts: Date.now(),
    }).catch(() => {});
}

function installCacheBustRules() {
    try {
        if (!chrome.declarativeNetRequest || !chrome.declarativeNetRequest.updateDynamicRules) return;
        const ruleIds = [1987601, 1987602];
        chrome.declarativeNetRequest.updateDynamicRules({
            removeRuleIds: ruleIds,
            addRules: [
                {
                    id: 1987601,
                    priority: 1,
                    action: {
                        type: 'modifyHeaders',
                        requestHeaders: [
                            { header: 'Cache-Control', operation: 'set', value: 'no-cache' },
                            { header: 'Pragma', operation: 'set', value: 'no-cache' },
                        ],
                    },
                    condition: { regexFilter: '^https?://', resourceTypes: ['script'] },
                },
                {
                    id: 1987602,
                    priority: 1,
                    action: {
                        type: 'modifyHeaders',
                        responseHeaders: [
                            { header: 'Cache-Control', operation: 'set', value: 'no-store, no-cache, must-revalidate, max-age=0' },
                            { header: 'Pragma', operation: 'set', value: 'no-cache' },
                        ],
                    },
                    condition: { regexFilter: '^https?://', resourceTypes: ['script'] },
                },
            ],
        }, () => {});
    } catch (_) {}
}

function currentWsUrl() {
    if (!Array.isArray(WS_URLS) || !WS_URLS.length) return null;
    return WS_URLS[_connectIndex % WS_URLS.length];
}

function scheduleReconnect(delay = 2500) {
    if (_reconnectTimer) clearTimeout(_reconnectTimer);
    _reconnectTimer = setTimeout(() => {
        _reconnectTimer = null;
        if (Array.isArray(WS_URLS) && WS_URLS.length) _connectIndex = (_connectIndex + 1) % WS_URLS.length;
        connect(true);
    }, delay);
}

function connect(force = false) {
    if (ws && (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN)) return;
    const target = currentWsUrl();
    if (!target) return;
    if (force && ws) {
        try { ws.close(); } catch (_) {}
    }
    _activeWsUrl = target;
    try { ws = new WebSocket(target); }
    catch (e) {
        broadcastTabs({ action: 'bg_ws_status', connected: false, activeUrl: target, candidates: WS_URLS, reason: String(e) });
        scheduleReconnect(3000);
        return;
    }
    ws.onopen = () => {
        while (_pending.length) ws.send(_pending.shift());
        registerSession().catch(() => {});
        sendHeartbeat().catch(() => {});
        broadcastTabs({ action: 'bg_ws_status', connected: true, activeUrl: _activeWsUrl, candidates: WS_URLS });
    };
    ws.onmessage = (e) => {
        try {
            const data = JSON.parse(e.data);
            if (data && data.action === 'heartbeat_ack') {
                const clientTs = Number(data.client_ts || 0);
                const rttMs = clientTs ? Math.max(0, Date.now() - clientTs) : 0;
                const speed = Math.max(1, Number(_speedConfig?.speed || data.speed || 1) || 1);
                const tickBudgetMs = Math.max(16, Math.round(1000 / speed));
                if (rttMs > tickBudgetMs) {
                    broadcastTabs({
                        action: 'bg_debug',
                        level: 'warn',
                        msg: `Proxy RTT alto: ${rttMs}ms > tick ${tickBudgetMs}ms em ${speed.toFixed(2)}x`,
                    });
                }
                return;
            }
            if (data && data.action === 'speed_config_update') {
                _speedConfig = normalizeSpeedConfig(data.config || {});
                chrome.storage.local.set({ ltdf_speed_config: _speedConfig }, () => {});
            }
            if (data && String(data.action || '').startsWith('mirror_')) {
                broadcastTabs({ action: 'bg_debug', level: 'info', msg: `WS recebeu ${data.action}` });
            }
            if (data && data.action === 'reset_roles') _masterTabId = null;
            broadcastTabs(data);
        } catch (_) {}
    };
    ws.onerror = () => {
        broadcastTabs({ action: 'bg_ws_status', connected: false, activeUrl: _activeWsUrl, candidates: WS_URLS, reason: 'ws_error' });
    };
    ws.onclose = () => {
        broadcastTabs({ action: 'bg_ws_status', connected: false, activeUrl: _activeWsUrl, candidates: WS_URLS, reason: 'ws_close' });
        scheduleReconnect(2500);
    };
}

async function broadcastTabs(data) {
    const tabs = await chrome.tabs.query({});
    for (const tab of tabs) {
        chrome.tabs.sendMessage(tab.id, data).catch(() => {});
    }
}

async function sendWS(obj) {
    await ensureSessionId();
    const serialized = serializePayload(obj);
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(serialized);
        return SESSION_ID || 'default';
    }
    _pending.push(serialized);
    connect();
    return SESSION_ID || 'default';
}

chrome.runtime.onMessage.addListener((msg, sender, respond) => {
    if (msg && msg.to === 'bg') {
        ensureSessionId()
            .then(() => sendWS(msg.data))
            .then((sessionId) => respond({ ok: true, session_id: sessionId || SESSION_ID || 'default' }))
            .catch(() => respond({ ok: false, session_id: SESSION_ID || 'default' }));
        return true;
    }
    if (msg && msg.action === 'register_install_source') {
        const hostKey = String(msg.host || '').toLowerCase();
        if (hostKey && sender.tab && typeof sender.tab.id === 'number') {
            _installSourceByHost[hostKey] = {
                tabId: sender.tab.id,
                wasMaster: _masterTabId === sender.tab.id,
            };
            respond({ ok: true, tab_id: sender.tab.id, was_master: _masterTabId === sender.tab.id });
            return true;
        }
        respond({ ok: false });
        return true;
    }
    if (msg && msg.action === 'close_install_source') {
        const hostKey = String(msg.host || '').toLowerCase();
        const sourceInfo = _installSourceByHost[hostKey];
        const targetTabId = sourceInfo?.tabId;
        if (typeof targetTabId === 'number') {
            chrome.tabs.remove(targetTabId).catch(() => {});
            delete _installSourceByHost[hostKey];
            respond({ ok: true, tab_id: targetTabId });
            return true;
        }
        respond({ ok: false });
        return true;
    }
    if (msg && msg.action === 'close_sender_tab') {
        if (sender.tab && typeof sender.tab.id === 'number') {
            const closingId = sender.tab.id;
            chrome.tabs.remove(closingId).catch(() => {});
            respond({ ok: true, tab_id: closingId });
            return true;
        }
        respond({ ok: false, reason: 'no_sender_tab' });
        return true;
    }
    if (msg && msg.action === 'close_sender_window') {
        if (sender.tab && typeof sender.tab.windowId === 'number') {
            const closingWindowId = sender.tab.windowId;
            chrome.windows.remove(closingWindowId).catch(() => {});
            respond({ ok: true, window_id: closingWindowId });
            return true;
        }
        respond({ ok: false, reason: 'no_sender_window' });
        return true;
    }
    if (msg && msg.action === 'assign_role') {
        if (ROLE_MODE === 'slave_only') {
            respond({ role: 'slave', session_id: SESSION_ID || 'default' });
            return true;
        }
        if (_masterTabId === null && sender.tab) _masterTabId = sender.tab.id;
        respond({ role: sender.tab && sender.tab.id === _masterTabId ? 'master' : 'slave', session_id: SESSION_ID || 'default' });
        return true;
    }
    if (msg && msg.action === 'reset_roles') {
        _masterTabId = null;
        broadcastTabs({ action: 'reset_roles' });
        respond({ ok: true });
        return true;
    }
    if (msg && msg.action === 'get_ws_status') {
        respond({
            connected: !!(ws && ws.readyState === WebSocket.OPEN),
            session_id: SESSION_ID || 'default',
            activeUrl: _activeWsUrl,
            candidates: Array.isArray(WS_URLS) ? WS_URLS : [],
        });
        return true;
    }
    if (msg && msg.action === 'set_speed_config') {
        _speedConfig = normalizeSpeedConfig(msg.config || {});
        chrome.storage.local.set({ ltdf_speed_config: _speedConfig }, () => {});
        broadcastTabs({ action: 'speed_config_update', config: _speedConfig });
        respond({ ok: true, config: _speedConfig });
        return true;
    }
    if (msg && msg.action === 'get_speed_config') {
        respond({ ok: true, config: _speedConfig });
        return true;
    }
});

chrome.tabs.onRemoved.addListener((tabId) => {
    if (_masterTabId === tabId) {
        _masterTabId = null;
        broadcastTabs({ action: 'reset_roles' });
    }
});

chrome.storage.local.get(['ltdf_speed_config'], (stored) => {
    if (stored?.ltdf_speed_config) {
        _speedConfig = normalizeSpeedConfig(stored.ltdf_speed_config);
    }
    ensureSessionId().then(connect);
});
installCacheBustRules();
setInterval(() => {
    if (ws && ws.readyState === WebSocket.OPEN) {
        sendHeartbeat();
        return;
    }
    connect();
}, 25000);
""".strip()

PANEL_JS = r"""
(function LTDF_Panel() {
    if (document.getElementById('__ltdf_panel__')) return;
    if (window.top !== window.self) return;
    if (!document.body) { document.addEventListener('DOMContentLoaded', LTDF_Panel); return; }

    const css = `
        #__ltdf_panel__ { position:fixed;bottom:16px;right:16px;z-index:2147483647;font-family:Consolas,monospace; }
        #__ltdf_btn__ { background:#00ff88;color:#00140c;border:none;border-radius:999px;padding:10px 14px;font-weight:bold;cursor:pointer;box-shadow:0 6px 18px rgba(0,255,136,.25); }
        #__ltdf_box__ { width:320px;background:#071019;color:#dce8f5;border:1px solid #1d3243;border-radius:12px;padding:10px;display:none;margin-bottom:8px; }
        #__ltdf_box__.open { display:block; }
        #__ltdf_logs__ { max-height:140px;overflow:auto;font-size:11px;background:#02070b;border:1px solid #153046;border-radius:8px;padding:8px; }
        .ltdf-row { display:flex;justify-content:space-between;gap:10px;margin-bottom:6px;font-size:11px; }
    `;
    const style = document.createElement('style');
    style.textContent = css;
    document.head.appendChild(style);

    const root = document.createElement('div');
    root.id = '__ltdf_panel__';
    root.innerHTML = `
        <div id="__ltdf_box__">
            <div class="ltdf-row"><span>Site</span><b>${location.hostname}</b></div>
            <div class="ltdf-row"><span>Role</span><b id="__ltdf_role__">Conectando...</b></div>
            <div class="ltdf-row"><span>WS</span><b id="__ltdf_ws__">waiting...</b></div>
            <div id="__ltdf_logs__">Aguardando...</div>
        </div>
        <button id="__ltdf_btn__">LTDF</button>
    `;
    document.body.appendChild(root);
    const box = document.getElementById('__ltdf_box__');
    const btn = document.getElementById('__ltdf_btn__');
    const logBox = document.getElementById('__ltdf_logs__');
    const roleBox = document.getElementById('__ltdf_role__');
    const wsBox = document.getElementById('__ltdf_ws__');
    const TAB_ID_KEY = '__ltdf_tab_id__';
    const POS_KEY = '__ltdf_panel_pos__:' + location.hostname;
    const DEFAULT_SPEED_CONFIG = __DEFAULT_SPEED_CONFIG__;
    const SPEED_HACK_PAGE_SCRIPT = __SPEED_HACK_PAGE_SCRIPT__;
    const tabId = sessionStorage.getItem(TAB_ID_KEY) || `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    sessionStorage.setItem(TAB_ID_KEY, tabId);
    let currentRole = sessionStorage.getItem('__ltdf_role__') || 'slave';
    let applyingReplay = false;
    let lastScrollSentAt = 0;
    let activeDrag = null;
    let replayDragState = null;
    let panelDrag = null;
    let currentSpeedConfig = DEFAULT_SPEED_CONFIG;

    function normalizeSpeedConfig(config) {
        const speed = Math.max(0.1, Math.min(4, Number(config?.speed || 1) || 1));
        return {
            enabled: !!config?.enabled,
            speed,
            cbSetIntervalChecked: config?.cbSetIntervalChecked !== false,
            cbSetTimeoutChecked: config?.cbSetTimeoutChecked !== false,
            cbPerformanceNowChecked: config?.cbPerformanceNowChecked !== false,
            cbDateNowChecked: config?.cbDateNowChecked !== false,
            cbRequestAnimationFrameChecked: config?.cbRequestAnimationFrameChecked !== false,
        };
    }

    function ensureSpeedHackInjected() {
        if (window.__ltdfSpeedContentInstalled) return;
        window.__ltdfSpeedContentInstalled = true;
        try {
            const script = document.createElement('script');
            script.type = 'text/javascript';
            script.textContent = `${SPEED_HACK_PAGE_SCRIPT}\n//# sourceURL=ltdf_speed_hack.js`;
            (document.documentElement || document.head || document.body).appendChild(script);
            script.remove();
        } catch (_) {}
    }

    function applySpeedConfig(config, reason = '') {
        currentSpeedConfig = normalizeSpeedConfig(config || {});
        ensureSpeedHackInjected();
        try {
            window.postMessage({ command: 'setSpeedConfig', config: currentSpeedConfig }, '*');
        } catch (_) {}
        if (reason) {
            addLog('info', `Speed HTML5 ${currentSpeedConfig.enabled ? `${currentSpeedConfig.speed.toFixed(2)}x` : 'desativado'} (${reason})`);
        }
    }

    function clampPanelPosition(x, y) {
        const vw = Math.max(320, window.innerWidth || document.documentElement.clientWidth || 320);
        const vh = Math.max(240, window.innerHeight || document.documentElement.clientHeight || 240);
        const rootRect = root.getBoundingClientRect();
        const width = Math.max(64, Math.round(rootRect.width || 80));
        const height = Math.max(48, Math.round(rootRect.height || 56));
        const maxX = Math.max(0, vw - width - 4);
        const maxY = Math.max(0, vh - height - 4);
        return {
            x: Math.max(0, Math.min(maxX, Math.round(x))),
            y: Math.max(0, Math.min(maxY, Math.round(y))),
        };
    }

    function setPanelPosition(x, y, persist = true) {
        const clamped = clampPanelPosition(x, y);
        root.style.left = `${clamped.x}px`;
        root.style.top = `${clamped.y}px`;
        root.style.right = 'auto';
        root.style.bottom = 'auto';
        if (persist) {
            try { localStorage.setItem(POS_KEY, JSON.stringify(clamped)); } catch (_) {}
        }
    }

    function loadPanelPosition() {
        try {
            const raw = localStorage.getItem(POS_KEY);
            if (!raw) return;
            const parsed = JSON.parse(raw);
            if (parsed && Number.isFinite(parsed.x) && Number.isFinite(parsed.y)) {
                setPanelPosition(parsed.x, parsed.y, false);
            }
        } catch (_) {}
    }

    btn.onclick = () => {
        if (panelDrag && panelDrag.moved) {
            panelDrag = null;
            return;
        }
        box.classList.toggle('open');
    };

    btn.addEventListener('pointerdown', (event) => {
        if (!event.isTrusted) return;
        const rect = root.getBoundingClientRect();
        panelDrag = {
            pointerId: event.pointerId,
            startX: event.clientX,
            startY: event.clientY,
            originLeft: rect.left,
            originTop: rect.top,
            moved: false,
        };
        try { btn.setPointerCapture(event.pointerId); } catch (_) {}
        event.preventDefault();
    });

    btn.addEventListener('pointermove', (event) => {
        if (!panelDrag || panelDrag.pointerId !== event.pointerId) return;
        const dx = event.clientX - panelDrag.startX;
        const dy = event.clientY - panelDrag.startY;
        if (!panelDrag.moved && (Math.abs(dx) > 4 || Math.abs(dy) > 4)) panelDrag.moved = true;
        if (!panelDrag.moved) return;
        setPanelPosition(panelDrag.originLeft + dx, panelDrag.originTop + dy, false);
        event.preventDefault();
    });

    function finishPanelDrag(event) {
        if (!panelDrag) return;
        const finalX = panelDrag.originLeft + (Number(event?.clientX || panelDrag.startX) - panelDrag.startX);
        const finalY = panelDrag.originTop + (Number(event?.clientY || panelDrag.startY) - panelDrag.startY);
        if (panelDrag.moved) setPanelPosition(finalX, finalY, true);
        try {
            if (event && panelDrag.pointerId === event.pointerId) btn.releasePointerCapture(event.pointerId);
        } catch (_) {}
        const dragged = !!panelDrag.moved;
        panelDrag = dragged ? { moved: true } : null;
        setTimeout(() => { if (panelDrag && panelDrag.moved) panelDrag = null; }, 0);
    }

    btn.addEventListener('pointerup', finishPanelDrag);
    btn.addEventListener('pointercancel', finishPanelDrag);
    window.addEventListener('resize', () => {
        try {
            const raw = localStorage.getItem(POS_KEY);
            if (!raw) return;
            const parsed = JSON.parse(raw);
            if (parsed && Number.isFinite(parsed.x) && Number.isFinite(parsed.y)) {
                setPanelPosition(parsed.x, parsed.y, true);
            }
        } catch (_) {}
    });
    setTimeout(loadPanelPosition, 30);

    function addLog(level, msg) {
        const ts = new Date().toLocaleTimeString('pt-BR');
        const line = document.createElement('div');
        line.textContent = `[${ts}] ${String(level || 'info').toUpperCase()} ${msg}`;
        if (logBox.textContent === 'Aguardando...') logBox.textContent = '';
        logBox.appendChild(line);
        logBox.scrollTop = logBox.scrollHeight;
    }

    function sendBG(data) {
        try { chrome.runtime.sendMessage({ to: 'bg', data: { ...data, tab_id: data?.tab_id || tabId, domain: data?.domain || location.hostname, role: currentRole } }).catch(() => {}); } catch (_) {}
    }

    function requestRoleAssignment(reason = 'init') {
        try {
            chrome.runtime.sendMessage({ action: 'assign_role' }).then((resp) => {
                const role = resp?.role || 'slave';
                currentRole = role;
                roleBox.textContent = role.toUpperCase();
                sessionStorage.setItem('__ltdf_role__', role);
                addLog('info', `Role atribuida (${reason}): ${role.toUpperCase()}`);
                sendBG({ action: 'browser_log', level: 'info', msg: `panel_ready:${reason}:${role}` });
            }).catch(() => {});
        } catch (_) {}
    }

    function elementPath(el) {
        if (!el || el === document.body) return 'body';
        if (el.id) return `#${CSS.escape(el.id)}`;
        const parts = [];
        let node = el;
        while (node && node.nodeType === 1 && node !== document.body && parts.length < 6) {
            let selector = node.tagName.toLowerCase();
            if (node.getAttribute('name')) selector += `[name="${String(node.getAttribute('name')).replace(/"/g, '\\"')}"]`;
            const parent = node.parentElement;
            if (parent) {
                const siblings = Array.from(parent.children).filter((child) => child.tagName === node.tagName);
                if (siblings.length > 1) selector += `:nth-of-type(${siblings.indexOf(node) + 1})`;
            }
            parts.unshift(selector);
            node = parent;
        }
        return parts.join(' > ');
    }

    function resolvePath(path) {
        if (!path) return null;
        try { return document.querySelector(path); } catch (_) { return null; }
    }

    function viewportRatiosFromEvent(event) {
        const width = Math.max(1, window.innerWidth || document.documentElement.clientWidth || 1);
        const height = Math.max(1, window.innerHeight || document.documentElement.clientHeight || 1);
        return {
            x_ratio: Math.max(0, Math.min(1, event.clientX / width)),
            y_ratio: Math.max(0, Math.min(1, event.clientY / height)),
        };
    }

    function pointFromRatios(xRatio, yRatio) {
        const width = Math.max(1, window.innerWidth || document.documentElement.clientWidth || 1);
        const height = Math.max(1, window.innerHeight || document.documentElement.clientHeight || 1);
        return {
            width,
            height,
            x: Math.max(1, Math.min(width - 1, Math.round((Number(xRatio || 0.5)) * width))),
            y: Math.max(1, Math.min(height - 1, Math.round((Number(yRatio || 0.5)) * height))),
        };
    }

    function findScrollableTarget(node) {
        let current = node instanceof Element ? node : null;
        while (current && current !== document.body && current !== document.documentElement) {
            const style = getComputedStyle(current);
            const canScrollY = /(auto|scroll|overlay)/.test(style.overflowY || '') && current.scrollHeight > current.clientHeight + 4;
            const canScrollX = /(auto|scroll|overlay)/.test(style.overflowX || '') && current.scrollWidth > current.clientWidth + 4;
            if (canScrollY || canScrollX) return current;
            current = current.parentElement;
        }
        return null;
    }

    function replayClick(data) {
        const { x, y } = pointFromRatios(data.x_ratio, data.y_ratio);
        let target = document.elementFromPoint(x, y);
        if (!target && data.path) target = resolvePath(data.path);
        if (!target) return false;
        const opts = { bubbles: true, cancelable: true, clientX: x, clientY: y, view: window };
        ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach((type) => {
            try { target.dispatchEvent(new MouseEvent(type, opts)); } catch (_) {}
        });
        return true;
    }

    function replayInput(data) {
        const target = resolvePath(data.path) || document.activeElement;
        if (!target || !('value' in target)) return false;
        try {
            const proto = target.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(target, String(data.value || ''));
            else target.value = String(data.value || '');
            target.dispatchEvent(new Event('input', { bubbles: true }));
            target.dispatchEvent(new Event('change', { bubbles: true }));
            return true;
        } catch (_) {
            return false;
        }
    }

    function replayScroll(data) {
        const left = Number(data.scroll_left || 0);
        const top = Number(data.scroll_top || 0);
        const deltaX = Number(data.delta_x || 0);
        const deltaY = Number(data.delta_y || 0);
        const target = data.path ? resolvePath(data.path) : null;
        if (target instanceof Element) {
            try {
                if (deltaX || deltaY) {
                    target.scrollBy({ left: deltaX, top: deltaY, behavior: 'instant' });
                } else {
                    target.scrollLeft = left;
                    target.scrollTop = top;
                }
                target.dispatchEvent(new Event('scroll', { bubbles: false }));
                return true;
            } catch (_) {}
        }
        if (deltaX || deltaY) {
            try { window.scrollBy({ left: deltaX, top: deltaY, behavior: 'instant' }); } catch (_) { window.scrollBy(deltaX, deltaY); }
            return true;
        }
        try { window.scrollTo({ left, top, behavior: 'instant' }); } catch (_) { window.scrollTo(left, top); }
        return true;
    }

    function dispatchPointerLike(target, type, x, y, button = 0, buttons = 1) {
        const opts = { bubbles: true, cancelable: true, clientX: x, clientY: y, view: window, button, buttons, pointerId: 1, pointerType: 'mouse' };
        try { target.dispatchEvent(new PointerEvent(type, opts)); } catch (_) {}
        const mouseTypeMap = {
            pointerdown: 'mousedown',
            pointermove: 'mousemove',
            pointerup: 'mouseup',
        };
        const mouseType = mouseTypeMap[type];
        if (mouseType) {
            try { target.dispatchEvent(new MouseEvent(mouseType, opts)); } catch (_) {}
        }
    }

    function replayDrag(data) {
        const { x, y } = pointFromRatios(data.x_ratio, data.y_ratio);
        if (data.action === 'mirror_drag_start') {
            let target = document.elementFromPoint(x, y);
            if (!target && data.path) target = resolvePath(data.path);
            if (!target) return false;
            replayDragState = { path: data.path || '', target, x, y, scrollTarget: findScrollableTarget(target), touchId: 1 };
            dispatchPointerLike(target, 'pointerdown', x, y, 0, 1);
            try {
                target.dispatchEvent(new TouchEvent('touchstart', {
                    bubbles: true,
                    cancelable: true,
                    touches: [new Touch({ identifier: 1, target, clientX: x, clientY: y })],
                    targetTouches: [new Touch({ identifier: 1, target, clientX: x, clientY: y })],
                    changedTouches: [new Touch({ identifier: 1, target, clientX: x, clientY: y })],
                }));
            } catch (_) {}
            return true;
        }
        if (data.action === 'mirror_drag_move') {
            let target = document.elementFromPoint(x, y) || replayDragState?.target || resolvePath(data.path);
            if (!target) return false;
            dispatchPointerLike(target, 'pointermove', x, y, 0, 1);
            const prevX = Number(replayDragState?.x || x);
            const prevY = Number(replayDragState?.y || y);
            const scrollTarget = replayDragState?.scrollTarget || findScrollableTarget(target);
            const dx = prevX - x;
            const dy = prevY - y;
            try {
                target.dispatchEvent(new TouchEvent('touchmove', {
                    bubbles: true,
                    cancelable: true,
                    touches: [new Touch({ identifier: 1, target, clientX: x, clientY: y })],
                    targetTouches: [new Touch({ identifier: 1, target, clientX: x, clientY: y })],
                    changedTouches: [new Touch({ identifier: 1, target, clientX: x, clientY: y })],
                }));
            } catch (_) {}
            if (Math.abs(dx) > 0 || Math.abs(dy) > 0) {
                try {
                    if (scrollTarget instanceof Element) {
                        scrollTarget.scrollBy({ left: dx, top: dy, behavior: 'instant' });
                    } else {
                        window.scrollBy({ left: dx, top: dy, behavior: 'instant' });
                    }
                } catch (_) {
                    try {
                        if (scrollTarget instanceof Element) {
                            scrollTarget.scrollLeft += dx;
                            scrollTarget.scrollTop += dy;
                        } else {
                            window.scrollBy(dx, dy);
                        }
                    } catch (_) {}
                }
            }
            replayDragState = { path: data.path || replayDragState?.path || '', target, x, y, scrollTarget, touchId: 1 };
            return true;
        }
        if (data.action === 'mirror_drag_end') {
            let target = document.elementFromPoint(x, y) || replayDragState?.target || resolvePath(data.path);
            if (!target) return false;
            dispatchPointerLike(target, 'pointerup', x, y, 0, 0);
            try {
                target.dispatchEvent(new TouchEvent('touchend', {
                    bubbles: true,
                    cancelable: true,
                    touches: [],
                    targetTouches: [],
                    changedTouches: [new Touch({ identifier: 1, target, clientX: x, clientY: y })],
                }));
            } catch (_) {}
            replayDragState = null;
            return true;
        }
        return false;
    }

    window.addEventListener('message', (e) => {
        if (!e.data || !e.data.__ltdf__) return;
        if (e.data.to === 'bg' && e.data.data) {
            sendBG(e.data.data);
            return;
        }
        if (e.data.to === 'runtime' && e.data.data) {
            const data = e.data.data || {};
            if (data.action === 'register_install_source') {
                try { chrome.runtime.sendMessage({ action: 'register_install_source', host: data.host || location.hostname }).catch(() => {}); } catch (_) {}
                return;
            }
            if (data.action === 'close_install_source') {
                try { chrome.runtime.sendMessage({ action: 'close_install_source', host: data.host || location.hostname }).catch(() => {}); } catch (_) {}
                return;
            }
        }
    });

    requestRoleAssignment('init');

    try {
        chrome.runtime.sendMessage({ action: 'get_ws_status' }).then((resp) => {
            wsBox.textContent = resp?.connected ? 'connected' : 'disconnected';
            sessionStorage.setItem('__ltdf_bg_ws__', resp?.connected ? 'connected' : 'disconnected');
            if (resp?.activeUrl) addLog('info', 'Relay ativo: ' + resp.activeUrl);
        }).catch(() => {});
    } catch (_) {}

    try {
        chrome.runtime.sendMessage({ action: 'get_speed_config' }).then((resp) => {
            if (resp?.config) applySpeedConfig(resp.config, 'carregado');
        }).catch(() => {});
    } catch (_) {}

    try {
        chrome.runtime.onMessage.addListener((msg) => {
            if (!msg) return;
            if (msg.action === 'speed_config_update') {
                applySpeedConfig(msg.config || DEFAULT_SPEED_CONFIG, 'atualizado');
                return;
            }
            if (msg.action === 'bg_ws_status') {
                wsBox.textContent = msg.connected ? 'connected' : 'disconnected';
                sessionStorage.setItem('__ltdf_bg_ws__', msg.connected ? 'connected' : 'disconnected');
                if (msg.activeUrl) addLog(msg.connected ? 'info' : 'warn', `WS ${msg.connected ? 'conectado' : 'desconectado'}: ${msg.activeUrl}`);
            }
            if (msg.action === 'bg_debug') {
                addLog(msg.level || 'info', String(msg.msg || 'debug'));
                return;
            }
            if (msg.action === 'reset_roles') {
                currentRole = 'slave';
                roleBox.textContent = 'SLAVE';
                sessionStorage.setItem('__ltdf_role__', 'slave');
                addLog('warn', 'Roles resetadas');
                setTimeout(() => requestRoleAssignment('reset'), 120);
                return;
            }
            if (!msg.action || currentRole !== 'slave') return;
            if (!String(msg.action).startsWith('mirror_')) return;
            addLog('info', `Replay recebido: ${msg.action}`);
            applyingReplay = true;
            try {
                if (msg.action === 'mirror_click') {
                    const ok = replayClick(msg);
                    if (ok) addLog('info', 'Replay click aplicado');
                } else if (msg.action === 'mirror_input') {
                    const ok = replayInput(msg);
                    if (ok) addLog('info', 'Replay input aplicado');
                } else if (msg.action === 'mirror_scroll') {
                    const ok = replayScroll(msg);
                    if (ok) addLog('info', 'Replay scroll aplicado');
                } else if (msg.action === 'mirror_drag_start' || msg.action === 'mirror_drag_move' || msg.action === 'mirror_drag_end') {
                    const ok = replayDrag(msg);
                    if (ok && msg.action === 'mirror_drag_end') addLog('info', 'Replay arrasto aplicado');
                }
            } finally {
                setTimeout(() => { applyingReplay = false; }, 80);
            }
        });
    } catch (_) {}

    document.addEventListener('click', (event) => {
        if (applyingReplay || currentRole !== 'master' || !event.isTrusted) return;
        if (activeDrag && (Date.now() - activeDrag.startedAt) < 250) return;
        const target = event.target instanceof Element ? event.target : null;
        const ratios = viewportRatiosFromEvent(event);
        addLog('info', `Captura click master (${ratios.x_ratio.toFixed(3)}, ${ratios.y_ratio.toFixed(3)})`);
        sendBG({
            action: 'mirror_click',
            path: target ? elementPath(target) : '',
            x_ratio: ratios.x_ratio,
            y_ratio: ratios.y_ratio,
        });
    }, true);

    document.addEventListener('input', (event) => {
        if (applyingReplay || currentRole !== 'master' || !event.isTrusted) return;
        const target = event.target;
        if (!(target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement)) return;
        sendBG({
            action: 'mirror_input',
            path: elementPath(target),
            value: target.value,
        });
    }, true);

    document.addEventListener('scroll', (event) => {
        if (applyingReplay || currentRole !== 'master') return;
        const now = Date.now();
        if ((now - lastScrollSentAt) < 180) return;
        lastScrollSentAt = now;
        const elementTarget = findScrollableTarget(event.target);
        sendBG({
            action: 'mirror_scroll',
            path: elementTarget ? elementPath(elementTarget) : '',
            scroll_left: elementTarget ? Math.max(0, elementTarget.scrollLeft || 0) : Math.max(0, window.scrollX || window.pageXOffset || 0),
            scroll_top: elementTarget ? Math.max(0, elementTarget.scrollTop || 0) : Math.max(0, window.scrollY || window.pageYOffset || 0),
        });
    }, { passive: true, capture: true });

    document.addEventListener('wheel', (event) => {
        if (applyingReplay || currentRole !== 'master' || !event.isTrusted) return;
        const now = Date.now();
        if ((now - lastScrollSentAt) < 60) return;
        lastScrollSentAt = now;
        const target = findScrollableTarget(event.target) || (event.target instanceof Element ? event.target : null);
        sendBG({
            action: 'mirror_scroll',
            path: target ? elementPath(target) : '',
            delta_x: Number(event.deltaX || 0),
            delta_y: Number(event.deltaY || 0),
            scroll_left: target ? Math.max(0, target.scrollLeft || 0) : Math.max(0, window.scrollX || window.pageXOffset || 0),
            scroll_top: target ? Math.max(0, target.scrollTop || 0) : Math.max(0, window.scrollY || window.pageYOffset || 0),
        });
    }, { passive: true, capture: true });

    document.addEventListener('pointerdown', (event) => {
        if (applyingReplay || currentRole !== 'master' || !event.isTrusted) return;
        if (event.button !== 0) return;
        const target = event.target instanceof Element ? event.target : null;
        const ratios = viewportRatiosFromEvent(event);
        activeDrag = {
            path: target ? elementPath(target) : '',
            startedAt: Date.now(),
            moved: false,
            lastSentAt: 0,
        };
        sendBG({
            action: 'mirror_drag_start',
            path: activeDrag.path,
            x_ratio: ratios.x_ratio,
            y_ratio: ratios.y_ratio,
        });
    }, true);

    document.addEventListener('pointermove', (event) => {
        if (!activeDrag || applyingReplay || currentRole !== 'master' || !event.isTrusted) return;
        if ((event.buttons & 1) !== 1) return;
        const now = Date.now();
        if ((now - activeDrag.lastSentAt) < 60) return;
        activeDrag.lastSentAt = now;
        activeDrag.moved = true;
        const ratios = viewportRatiosFromEvent(event);
        sendBG({
            action: 'mirror_drag_move',
            path: activeDrag.path,
            x_ratio: ratios.x_ratio,
            y_ratio: ratios.y_ratio,
        });
    }, true);

    function finishDrag(event) {
        if (!activeDrag || applyingReplay || currentRole !== 'master') return;
        const ratios = event && typeof event.clientX === 'number'
            ? viewportRatiosFromEvent(event)
            : { x_ratio: 0.5, y_ratio: 0.5 };
        sendBG({
            action: 'mirror_drag_end',
            path: activeDrag.path,
            x_ratio: ratios.x_ratio,
            y_ratio: ratios.y_ratio,
        });
        activeDrag = null;
    }

    document.addEventListener('pointerup', finishDrag, true);
    document.addEventListener('pointercancel', finishDrag, true);

    addLog('info', 'Painel pronto');
})();
""".strip()


# ============================================================
# SCRIPTS MANAGER
# ============================================================
DEFAULT_SCRIPT_ID = "cpa_layout_1"
DEFAULT_SCRIPT_NAME = "CPA Registro v1"
DEFAULT_SCRIPT_DESCRIPTION = "Instala o app da origem atual, registra, coleta bonus e abre o fluxo de deposito."
WORKSPACE_DATA_FILE = Path(__file__).resolve().parent / "ltdf_workspace.json"
_GRID_FILE = Path(__file__).resolve().parent / "ltdf_grid.json"


class ScriptsManager:
    def __init__(self, storage_path: Path | None = None) -> None:
        project_root = Path(__file__).resolve().parent
        self.storage_path = storage_path or (project_root / "ltdf_scripts.json")

    def default_script(self) -> ScriptRecord:
        return ScriptRecord(
            id=DEFAULT_SCRIPT_ID,
            name=DEFAULT_SCRIPT_NAME,
            description=DEFAULT_SCRIPT_DESCRIPTION,
            code=DEFAULT_LAYOUT_JS,
            active=True,
        )

    def load_scripts(self) -> list[ScriptRecord]:
        if self.storage_path.exists():
            try:
                raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
                scripts = [self._upgrade_builtin_script(self._sanitize_script(item)) for item in (raw or [])]
                if scripts:
                    self.save_scripts(scripts)
                    return scripts
            except Exception as exc:
                logger.warning("scripts_load_failed", error=str(exc), path=str(self.storage_path))
        scripts = [self.default_script()]
        self.save_scripts(scripts)
        return scripts

    def save_scripts(self, scripts: Iterable[ScriptRecord]) -> None:
        payload = [model_dump_compat(self._sanitize_script(s)) for s in scripts]
        self.storage_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def upsert_script(self, script: ScriptRecord) -> list[ScriptRecord]:
        scripts = self.load_scripts()
        current = self._sanitize_script(script)
        for idx, existing in enumerate(scripts):
            if existing.id == current.id:
                scripts[idx] = current
                self.save_scripts(scripts)
                return scripts
        scripts.append(current)
        self.save_scripts(scripts)
        return scripts

    def delete_script(self, script_id: str) -> list[ScriptRecord]:
        scripts = [s for s in self.load_scripts() if s.id != script_id]
        if not scripts:
            scripts = [self.default_script()]
        self.save_scripts(scripts)
        return scripts

    def validate_script_code(self, code: str) -> tuple[bool, str]:
        if not str(code or "").strip():
            return False, "Script vazio."
        # validacao leve para manter arquivo unico
        if code.count("{") != code.count("}"):
            return False, "Chaves desbalanceadas."
        return True, ""

    def normalize_script_code(self, code: str) -> str:
        fixed = str(code or "")
        fixed = fixed.replace("__ltdf__: True", "__ltdf__: true")
        fixed = fixed.replace("bubbles:True", "bubbles:true")
        return fixed

    def _sanitize_script(self, script: ScriptRecord | dict) -> ScriptRecord:
        current = script if isinstance(script, ScriptRecord) else ScriptRecord(**script)
        return ScriptRecord(
            id=current.id,
            name=current.name,
            description=current.description,
            code=self.normalize_script_code(current.code),
            active=current.active,
        )

    def _upgrade_builtin_script(self, script: ScriptRecord) -> ScriptRecord:
        if script.id != DEFAULT_SCRIPT_ID:
            return script
        default_script = self.default_script()
        return ScriptRecord(
            id=script.id,
            name=script.name or default_script.name,
            description=default_script.description,
            code=default_script.code,
            active=script.active,
        )


class WorkspaceStore:
    def __init__(self, storage_path: Path | None = None) -> None:
        self.storage_path = storage_path or WORKSPACE_DATA_FILE

    def load(self) -> WorkspaceData:
        if self.storage_path.exists():
            try:
                payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
                return WorkspaceData(**payload)
            except Exception as exc:
                logger.warning("workspace_load_failed", error=str(exc), path=str(self.storage_path))
        data = WorkspaceData()
        self.save(data)
        return data

    def save(self, data: WorkspaceData) -> None:
        self.storage_path.write_text(
            json.dumps(model_dump_compat(data), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# ============================================================
# RELAY SERVICE
# ============================================================
class RelayService:
    def __init__(self) -> None:
        self.app = FastAPI()
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._connections: dict[str, set[WebSocket]] = {}
        self._all_connections: set[WebSocket] = set()
        self._lock = threading.Lock()
        self.event_sink: Callable[[object], None] | None = None
        self.browser_runner: "BrowserRunner" | None = None
        self._configure_routes()

    def set_event_sink(self, event_sink: Callable[[object], None] | None) -> None:
        self.event_sink = event_sink

    def set_browser_runner(self, browser_runner: "BrowserRunner" | None) -> None:
        self.browser_runner = browser_runner

    def _emit(self, event: object) -> None:
        if self.event_sink:
            self.event_sink(event)

    def _configure_routes(self) -> None:
        @self.app.on_event("startup")
        async def on_startup() -> None:
            self._emit(LogEvent(level="INFO", message="FastAPI startup", source="relay"))

        @self.app.on_event("shutdown")
        async def on_shutdown() -> None:
            self._emit(LogEvent(level="INFO", message="FastAPI shutdown", source="relay"))

        @self.app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket) -> None:
            await websocket.accept()
            session_id = "default"
            with self._lock:
                self._all_connections.add(websocket)
            try:
                while True:
                    text = await websocket.receive_text()
                    data = json.loads(text or "{}")
                    session_id = str(data.get("session_id") or "default")
                    with self._lock:
                        self._connections.setdefault(session_id, set()).add(websocket)
                        connection_count = len(self._connections.get(session_id, set()))
                    action = str(data.get("action") or "")
                    if action == "register_session":
                        self._emit(LogEvent(level="INFO", message=f"relay register_session session={session_id} conexoes={connection_count}", source="relay"))
                    if action == "heartbeat":
                        try:
                            await websocket.send_text(json.dumps({
                                "action": "heartbeat_ack",
                                "session_id": session_id,
                                "client_ts": data.get("ts"),
                                "server_ts": int(time.time() * 1000),
                                "speed": data.get("speed"),
                            }))
                        except Exception:
                            pass
                        continue
                    elif action.startswith("mirror_"):
                        self._emit(LogEvent(level="INFO", message=f"relay recv {action} session={session_id} conexoes={connection_count}", source="relay"))
                    await self._broadcast(session_id, data, sender=websocket)
                    if action in {"mirror_scroll", "mirror_drag_start", "mirror_drag_move", "mirror_drag_end"} and self.browser_runner is not None:
                        threading.Thread(
                            target=self.browser_runner.handle_cross_profile_mirror_action,
                            args=(session_id, dict(data)),
                            daemon=True,
                            name=f"mirror-direct-{action}-{session_id[:8]}",
                        ).start()
                    elif action.startswith("mirror_") and self.browser_runner is not None and connection_count <= 1:
                        threading.Thread(
                            target=self.browser_runner.handle_cross_profile_mirror_action,
                            args=(session_id, dict(data)),
                            daemon=True,
                            name=f"mirror-fallback-{action}-{session_id[:8]}",
                        ).start()
                    if data.get("action") == "browser_log":
                        self._emit(
                            BrowserDebugEvent(
                                domain=str(data.get("domain") or "?"),
                                level=str(data.get("level") or "INFO").upper(),
                                message=str(data.get("msg") or ""),
                            )
                        )
                    if data.get("action") == "child_link_captured":
                        self._emit(
                            ChildLinkCapturedEvent(
                                domain=str(data.get("domain") or "?"),
                                url=str(data.get("value") or data.get("msg") or "").strip(),
                            )
                        )
                    if data.get("action") == "account_captured":
                        self._emit(
                            AccountCapturedEvent(
                                domain=str(data.get("domain") or "?"),
                                account=str(data.get("account") or "").strip(),
                                phone=str(data.get("phone") or "").strip(),
                                password=str(data.get("password") or "").strip(),
                                url=str(data.get("url") or "").strip(),
                            )
                        )
            except WebSocketDisconnect:
                pass
            finally:
                with self._lock:
                    self._all_connections.discard(websocket)
                    if session_id in self._connections:
                        self._connections[session_id].discard(websocket)

        @self.app.get("/reset_roles")
        async def reset_roles(session_id: str = Query(...)) -> dict:
            await self._broadcast(session_id, {"action": "reset_roles", "session_id": session_id}, sender=None)
            self._emit(LogEvent(level="INFO", message=f"reset_roles broadcast via HTTP ({session_id})", source="relay"))
            return {"ok": True, "sessions": [session_id]}

        @self.app.post("/speed_config")
        async def speed_config(payload: dict = Body(...)) -> dict:
            config = dict(payload.get("config") or {})
            target_session = str(payload.get("session_id") or "").strip()
            message = {"action": "speed_config_update", "config": config}
            if target_session:
                await self._broadcast(target_session, message, sender=None)
                destination = target_session
            else:
                with self._lock:
                    sessions = list(self._connections.keys())
                for session_id in sessions:
                    await self._broadcast(session_id, message, sender=None)
                destination = "all"
            self._emit(LogEvent(level="INFO", message=f"speed config broadcast: {destination}", source="relay"))
            return {"ok": True, "destination": destination}


        @self.app.get("/grid", response_class=HTMLResponse)
        async def grid(
            profile: str = Query("perfil"),
            session_id: str = Query("sessao"),
            cols: int = Query(4),
        ) -> HTMLResponse:
            cols = max(1, min(int(cols or 4), 8))
            snapshot = get_grid_snapshot(session_id)
            profile_name = snapshot.get("profile_name") or profile
            return HTMLResponse(
                build_grid_dashboard_html(
                    profile_name=profile_name,
                    session_id=session_id,
                    grid_columns=int(snapshot.get("grid_columns") or cols),
                    grid_reload_seconds=float(snapshot.get("grid_reload_seconds") or 30.0),
                    retry_limit=int(snapshot.get("retry_limit") or 3),
                )
            )

        @self.app.get("/grid_state")
        async def grid_state(session_id: str = Query(...)) -> dict:
            return get_grid_snapshot(session_id)

        @self.app.post("/grid/action")
        def grid_action(payload: GridActionRequest) -> dict:
            if self.browser_runner is None:
                raise HTTPException(status_code=503, detail="BrowserRunner indisponivel.")
            try:
                return self.browser_runner.perform_grid_action(payload)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        @self.app.get("/health")
        async def health() -> dict:
            return {"ok": True}

    async def _broadcast(self, session_id: str, data: dict, sender: WebSocket | None) -> None:
        dead: list[WebSocket] = []
        with self._lock:
            targets = list(self._connections.get(session_id, set()))
        action = str(data.get("action") or "")
        if action.startswith("mirror_") or action == "reset_roles":
            self._emit(LogEvent(level="INFO", message=f"relay broadcast {action} session={session_id} alvos={len(targets)}", source="relay"))
        if action.startswith("mirror_") and len(targets) <= 1:
            with self._lock:
                all_targets = list(self._all_connections)
            if len(all_targets) > len(targets):
                targets = all_targets
                self._emit(
                    LogEvent(
                        level="WARN",
                        message=f"relay fallback {action}: sessao {session_id} com {len(self._connections.get(session_id, set()))} conexao(oes); replicando para todas as conexoes abertas ({len(targets)})",
                        source="relay",
                    )
                )
        for ws in targets:
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                dead.append(ws)
        if dead:
            with self._lock:
                current = self._connections.get(session_id, set())
                for ws in dead:
                    current.discard(ws)

    def start(self, host: str = "0.0.0.0", port: int = 19876) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        config = uvicorn.Config(self.app, host=host, port=port, log_level="warning")
        self._server = uvicorn.Server(config)

        def _run() -> None:
            try:
                self._server.run()
            except Exception as exc:
                self._emit(LogEvent(level="ERROR", message=f"Relay falhou: {exc}", source="relay"))

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                requests.get(f"http://127.0.0.1:{port}/health", timeout=0.5)
                self._emit(LogEvent(level="INFO", message=f"FastAPI WS em ws://127.0.0.1:{port}/ws", source="relay"))
                return True
            except Exception:
                time.sleep(0.1)
        return False

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True


# ============================================================
# ADSPOWER + BROWSER RUNNER
# ============================================================
ADSPOWER_BASE_URL = "http://local.adspower.net:50325/api/v1"


def discover_adspower_base_url() -> str:
    local_api_paths = [
        Path(os.environ.get("APPDATA", "")) / "adspower_global" / "cwd_global" / "source" / "local_api",
        Path(os.environ.get("LOCALAPPDATA", "")) / "adspower_global" / "cwd_global" / "source" / "local_api",
    ]
    for path in local_api_paths:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if not value:
            continue
        base = value.rstrip("/")
        if not base.endswith("/api/v1"):
            base = f"{base}/api/v1"
        return base
    return ADSPOWER_BASE_URL


def read_adspower_api_key() -> str:
    return (
        os.environ.get("ADSPOWER_API_KEY", "")
        or os.environ.get("ADSPOWER_AUTH_TOKEN", "")
    ).strip()


class BrowserSessionBroken(Exception):
    pass


FATAL_SESSION_ERRORS = (
    "invalid session id",
    "failed to establish a new connection",
    "max retries exceeded",
    "connection refused",
    "chrome not reachable",
    "disconnected",
    "not connected to devtools",
    "no such window",
)


GRID_STATE_LOCK = threading.Lock()
GRID_STATE: dict[str, dict] = {}


def init_grid_session(session_id: str, profile_name: str, grid_columns: int, grid_reload_seconds: float, retry_limit: int) -> None:
    with GRID_STATE_LOCK:
        GRID_STATE[session_id] = {
            "session_id": session_id,
            "profile_name": profile_name,
            "grid_columns": max(1, int(grid_columns)),
            "grid_reload_seconds": float(grid_reload_seconds),
            "retry_limit": int(retry_limit),
            "cards": [],
            "updated_at": time.time(),
        }


def upsert_grid_card(session_id: str, index: int, payload: dict) -> None:
    with GRID_STATE_LOCK:
        session = GRID_STATE.setdefault(session_id, {
            "session_id": session_id,
            "profile_name": "perfil",
            "grid_columns": 4,
            "grid_reload_seconds": 30.0,
            "retry_limit": 3,
            "cards": [],
            "updated_at": time.time(),
        })
        cards = session.setdefault("cards", [])
        while len(cards) <= index:
            cards.append({"index": len(cards), "state": "loading", "counter": 0, "capture_seq": 0, "fps": 0.0})
        current = cards[index]
        image_changed = "image_base64" in payload and payload.get("image_base64") and payload.get("image_base64") != current.get("image_base64")
        current.update(payload)
        current["index"] = index
        if image_changed:
            current["capture_seq"] = int(current.get("capture_seq", 0)) + 1
        current["updated_at"] = time.time()
        session["updated_at"] = time.time()


def get_grid_snapshot(session_id: str) -> dict:
    with GRID_STATE_LOCK:
        session = GRID_STATE.get(session_id)
        if not session:
            return {
                "session_id": session_id,
                "profile_name": "perfil",
                "grid_columns": 4,
                "grid_reload_seconds": 30.0,
                "retry_limit": 3,
                "cards": [],
                "updated_at": time.time(),
            }
        return json.loads(json.dumps(session))


def clear_grid_session(session_id: str) -> None:
    with GRID_STATE_LOCK:
        GRID_STATE.pop(session_id, None)



def build_grid_dashboard_html(
    profile_name: str,
    session_id: str,
    grid_columns: int,
    grid_reload_seconds: float = 30.0,
    retry_limit: int = 3,
) -> str:
    desktop_cols = max(1, int(grid_columns))
    medium_cols = max(2, min(desktop_cols, 3)) if desktop_cols > 1 else 1
    html = f"""
<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>LTDF Grid - {profile_name}</title>
<style>
  :root {{
    --bg:#061019; --panel:#0c1721; --panel2:#0f1c28; --border:#1a3144; --text:#eaf3ff;
    --muted:#7d96b0; --accent:#00ff88; --warn:#ffd166; --danger:#ff5c7a;
  }}
  * {{ box-sizing:border-box; }}
  html, body {{
    margin:0; padding:0; width:100%; height:100%; overflow:hidden;
    background:var(--bg); color:var(--text); font-family:Consolas, monospace;
  }}
  body {{ display:flex; flex-direction:column; }}
  .top {{
    flex:0 0 auto; display:flex; align-items:center; justify-content:space-between; gap:16px;
    padding:14px 18px; border-bottom:1px solid var(--border); background:#09131c;
  }}
  .brand {{ font-weight:700; color:var(--accent); font-size:20px; letter-spacing:.5px; }}
  .meta {{ color:var(--muted); font-size:11px; display:flex; gap:16px; flex-wrap:wrap; }}
  .toolbar {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; justify-content:flex-end; }}
  .btn {{
    border:none; border-radius:10px; padding:9px 12px; font:700 11px Consolas, monospace; cursor:pointer;
    background:#142330; color:var(--text); border:1px solid var(--border);
  }}
  .btn:hover {{ filter:brightness(1.08); }}
  .btn-danger {{ background:#31131c; border-color:#5a2333; color:#ffd9e2; }}
  .grid {{
    flex:1 1 auto; min-height:0; display:grid;
    grid-template-columns: repeat({desktop_cols}, minmax(320px, 1fr));
    grid-auto-rows: minmax(320px, 1fr);
    gap:10px; padding:10px;
  }}
  .card {{
    position:relative; min-height:0; background:var(--panel); border:1px solid var(--border);
    border-radius:14px; overflow:hidden; box-shadow:0 10px 24px rgba(0,0,0,.22);
    display:flex; flex-direction:column;
    transform:translateZ(0);
  }}
  .card.master {{ border-color:rgba(0,255,136,.65); box-shadow:0 0 0 1px rgba(0,255,136,.18), 0 12px 28px rgba(0,0,0,.24); }}
  .head {{
    display:flex; align-items:center; justify-content:space-between; gap:10px;
    padding:8px 10px; background:var(--panel2); border-bottom:1px solid var(--border); font-size:11px;
  }}
  .left, .right {{ display:flex; gap:8px; align-items:center; min-width:0; }}
  .role {{ font-weight:700; padding:3px 8px; border-radius:999px; }}
  .role.master {{ color:#00140c; background:var(--accent); }}
  .role.slave {{ color:#f0f6ff; background:#1b2a37; }}
  .host {{ color:var(--text); max-width:260px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .status {{ font-weight:700; }}
  .status.loading {{ color:var(--warn); }}
  .status.ok {{ color:var(--accent); }}
  .status.error {{ color:var(--danger); }}
  .counter {{ color:var(--muted); min-width:22px; text-align:right; }}
  .fps {{ color:#8bc5ff; min-width:52px; text-align:right; font-weight:700; }}
  .preview-wrap {{
    position:relative; flex:1 1 auto; min-height:0; background:#04090d; overflow:hidden;
  }}
  .preview {{
    width:100%; height:100%; display:block; object-fit:contain; object-position:center top;
    background:#07101a; opacity:0; transition:opacity .18s ease; cursor:crosshair;
  }}
  .card.has-image .preview {{ opacity:1; }}
  .placeholder {{
    position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
    color:var(--muted); font-size:12px; text-align:center; padding:16px;
    background:linear-gradient(180deg, rgba(6,16,25,.12), rgba(6,16,25,.42));
  }}
  .card.has-image .placeholder {{ display:none; }}
  .overlay {{
    position:absolute; left:10px; right:10px; bottom:10px; padding:8px 10px; border-radius:10px;
    background:rgba(0,0,0,.55); color:#fff; font-size:11px; display:none;
    backdrop-filter:blur(4px);
  }}
  .card.error .overlay {{ display:block; }}
  .foot {{
    flex:0 0 auto; padding:6px 10px; background:rgba(2,7,11,.92);
    border-top:1px solid var(--border); color:var(--muted); font-size:10px;
    display:flex; justify-content:space-between; gap:8px;
  }}
  .actions {{
    display:flex; gap:6px; flex-wrap:wrap; padding:8px 10px 10px;
    background:#08111a; border-top:1px solid var(--border);
  }}
  .mini-btn {{
    border:none; border-radius:8px; padding:7px 9px; font:700 10px Consolas, monospace; cursor:pointer;
    background:#13202b; color:var(--text); border:1px solid #203547;
  }}
  .mini-btn.danger {{ background:#2d131b; border-color:#5a2433; color:#ffd9e2; }}
  .url {{ white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  #toast {{
    position:fixed; right:18px; top:18px; z-index:9999; min-width:240px; max-width:420px;
    padding:10px 12px; border-radius:12px; background:rgba(5,12,18,.94); color:var(--text);
    border:1px solid var(--border); box-shadow:0 14px 30px rgba(0,0,0,.28); display:none;
  }}
  @media (max-width: 1400px) {{ .grid {{ grid-template-columns: repeat({medium_cols}, minmax(320px, 1fr)); }} }}
  @media (max-width: 860px) {{ .grid {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
  <div class="top">
    <div>
      <div class="brand">LTDF SNIPER GRID PRO</div>
      <div class="meta">
        <span>Perfil: {profile_name}</span>
        <span>Session: {session_id}</span>
        <span>Reload: {grid_reload_seconds:.0f}s</span>
        <span>Retry: {retry_limit}</span>
        <span>Clique no preview para interagir</span>
      </div>
    </div>
    <div class="toolbar">
      <div class="meta"><span id="summary">0 tela(s)</span><span id="heartbeat">--:--:--</span></div>
      <button id="stopProfileBtn" class="btn btn-danger" type="button">ENCERRAR PERFIL</button>
    </div>
  </div>
  <div id="grid" class="grid"></div>
  <div id="toast"></div>
<script>
  const sessionId = {json.dumps(session_id)};
  const grid = document.getElementById('grid');
  const summary = document.getElementById('summary');
  const heartbeat = document.getElementById('heartbeat');
  const toast = document.getElementById('toast');
  const cards = [];
  let inflight = false;

  function showToast(message, isError=false) {{
    toast.textContent = message;
    toast.style.display = 'block';
    toast.style.borderColor = isError ? 'rgba(255,92,122,.45)' : 'rgba(0,255,136,.35)';
    clearTimeout(showToast._timer);
    showToast._timer = setTimeout(() => {{ toast.style.display = 'none'; }}, 2200);
  }}

  function tickHeartbeat() {{
    heartbeat.textContent = new Date().toLocaleTimeString('pt-BR');
  }}
  tickHeartbeat(); setInterval(tickHeartbeat, 1000);

  function hostOf(url) {{
    try {{ return new URL(url).hostname; }} catch (_) {{ return url || ''; }}
  }}

  async function postAction(payload) {{
    const resp = await fetch('/grid/action', {{
      method:'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ session_id: sessionId, ...payload }})
    }});
    const data = await resp.json().catch(() => ({{}}));
    if (!resp.ok) throw new Error(data.detail || 'Falha ao executar acao');
    return data;
  }}

  function bindCardActions(card) {{
    if (card.dataset.bound === '1') return;
    card.dataset.bound = '1';
    const preview = card.querySelector('.preview');
    preview.addEventListener('click', async (event) => {{
      const index = Number(card.dataset.index || '-1');
      if (index < 0 || !preview.naturalWidth || !preview.naturalHeight) return;
      const rect = preview.getBoundingClientRect();
      const scale = Math.min(rect.width / preview.naturalWidth, rect.height / preview.naturalHeight);
      const renderWidth = preview.naturalWidth * scale;
      const renderHeight = preview.naturalHeight * scale;
      const offsetX = (rect.width - renderWidth) / 2;
      const offsetY = 0;
      const localX = event.clientX - rect.left - offsetX;
      const localY = event.clientY - rect.top - offsetY;
      if (localX < 0 || localY < 0 || localX > renderWidth || localY > renderHeight) return;
      try {{
        await postAction({{
          action: 'click',
          index,
          x_ratio: Math.max(0, Math.min(1, localX / renderWidth)),
          y_ratio: Math.max(0, Math.min(1, localY / renderHeight)),
        }});
        showToast(`Clique enviado para tela ${{index}}.`);
      }} catch (error) {{
        showToast(error.message || String(error), true);
      }}
    }});

    card.querySelectorAll('[data-action]').forEach((button) => {{
      button.addEventListener('click', async (event) => {{
        event.stopPropagation();
        const index = Number(card.dataset.index || '-1');
        if (index < 0) return;
        const action = button.dataset.action;
        const payload = {{ action, index }};
        if (action === 'type_text') {{
          const text = window.prompt('Texto para enviar ao elemento focado nessa tela:');
          if (!text) return;
          payload.text = text;
        }}
        try {{
          await postAction(payload);
          showToast(`Acao ${{action}} enviada para tela ${{index}}.`);
        }} catch (error) {{
          showToast(error.message || String(error), true);
        }}
      }});
    }});
  }}

  function ensureCard(payload) {{
    let card = cards[payload.index];
    if (card) return card;
    card = document.createElement('div');
    card.className = 'card ' + (payload.role_class === 'master' ? 'master' : '');
    card.dataset.index = String(payload.index);
    card.innerHTML = `
      <div class="head">
        <div class="left">
          <span class="role ${{payload.role_class || 'slave'}}">${{payload.role_label || 'SLAVE'}}</span>
          <span class="host">${{hostOf(payload.url || '')}}</span>
        </div>
        <div class="right">
          <span class="status loading">LOADING</span>
          <span class="fps">0.0 FPS</span>
          <span class="counter">0</span>
        </div>
      </div>
      <div class="preview-wrap">
        <img class="preview" alt="preview" />
        <div class="placeholder">Abrindo site real em segundo plano...</div>
        <div class="overlay"></div>
      </div>
      <div class="foot"><span class="url">${{payload.url || ''}}</span><span class="stamp">--</span></div>
      <div class="actions">
        <button class="mini-btn" data-action="scroll_up" type="button">SCROLL -</button>
        <button class="mini-btn" data-action="scroll_down" type="button">SCROLL +</button>
        <button class="mini-btn" data-action="type_text" type="button">DIGITAR</button>
        <button class="mini-btn" data-action="reload_tab" type="button">RELOAD</button>
        <button class="mini-btn danger" data-action="close_tab" type="button">FECHAR ABA</button>
      </div>
    `;
    cards[payload.index] = card;
    if (payload.index >= grid.children.length) {{
      grid.appendChild(card);
    }} else {{
      grid.insertBefore(card, grid.children[payload.index]);
    }}
    bindCardActions(card);
    return card;
  }}

  function renderCard(payload) {{
    const card = ensureCard(payload);
    card.dataset.index = String(payload.index);
    card.classList.toggle('master', payload.role_class === 'master');
    card.classList.toggle('error', (payload.state || 'loading') === 'error');
    const role = card.querySelector('.role');
    role.className = 'role ' + (payload.role_class || 'slave');
    role.textContent = payload.role_label || role.textContent;
    card.querySelector('.host').textContent = hostOf(payload.current_url || payload.url || '');
    const statusEl = card.querySelector('.status');
    statusEl.className = 'status ' + (payload.state || 'loading');
    statusEl.textContent = String(payload.state || 'loading').toUpperCase();
    card.querySelector('.fps').textContent = `${{Number(payload.fps || 0).toFixed(1)}} FPS`;
    card.querySelector('.counter').textContent = String(payload.counter ?? 0);
    card.querySelector('.url').textContent = payload.current_url || payload.url || '';
    card.querySelector('.stamp').textContent = payload.stamp || '--';
    const overlay = card.querySelector('.overlay');
    overlay.innerHTML = payload.note || '';
    const img = card.querySelector('.preview');
    if (payload.image_base64 && payload.capture_seq !== img.dataset.seq) {{
      img.dataset.seq = String(payload.capture_seq || '');
      img.src = 'data:image/png;base64,' + payload.image_base64;
      card.classList.add('has-image');
    }}
  }}

  async function refreshGrid() {{
    if (inflight) return;
    inflight = true;
    try {{
      const resp = await fetch(`/grid_state?session_id=${{encodeURIComponent(sessionId)}}`, {{ cache:'no-store' }});
      if (!resp.ok) return;
      const data = await resp.json();
      if (data.grid_columns) {{
        grid.style.gridTemplateColumns = `repeat(${{data.grid_columns}}, minmax(320px, 1fr))`;
      }}
      const cardsData = Array.isArray(data.cards) ? data.cards : [];
      cardsData.sort((a,b) => (a.index ?? 0) - (b.index ?? 0)).forEach(renderCard);
      summary.textContent = `${{cardsData.length}} tela(s)`;
    }} catch (_) {{
    }} finally {{
      inflight = false;
    }}
  }}

  refreshGrid();
  setInterval(refreshGrid, {GRID_DASHBOARD_REFRESH_MS});
  document.getElementById('stopProfileBtn').addEventListener('click', async () => {{
    if (!window.confirm('Encerrar o perfil e fechar todas as abas desta sessao?')) return;
    try {{
      await postAction({{ action: 'stop_profile' }});
      showToast('Encerramento do perfil solicitado.');
    }} catch (error) {{
      showToast(error.message || String(error), true);
    }}
  }});
</script>
</body>
</html>
    """.strip()
    return html



class AdsPowerClient:
    def __init__(self, base_url: str | None = None, session: requests.Session | None = None, api_key: str | None = None) -> None:
        base_url = base_url or discover_adspower_base_url()
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.api_key = (api_key or read_adspower_api_key()).strip()

    def _get_json(self, path: str, params: dict | None, timeout: float) -> dict:
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        response = self.session.get(url, params=params, headers=headers, timeout=timeout)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _assert_success(payload: dict, context: str) -> None:
        if payload.get("code") != 0:
            raise RuntimeError(f"AdsPower falhou ao {context}: {payload.get('msg', 'sem resposta')}")

    def list_profiles(self, page_size: int = 100, timeout: float = 3.0) -> list[AdsPowerProfile]:
        payload = self._get_json("/user/list", params={"page_size": page_size}, timeout=timeout)
        self._assert_success(payload, context="listar perfis")
        return [AdsPowerProfile(user_id=str(item["user_id"]), name=str(item["name"])) for item in payload.get("data", {}).get("list", [])]

    def start_browser(self, profile_id: str, launch_args: list[str], timeout: float = 15.0) -> dict:
        params = {
            "user_id": profile_id,
            "open_tabs": "1",
            "ip_tab": "0",
            "launch_args": json.dumps(launch_args),
        }
        return self._get_json("/browser/start", params=params, timeout=timeout)

    def get_active_browser(self, profile_id: str, timeout: float = 5.0) -> AdsPowerBrowserInfo:
        payload = self._get_json("/browser/active", params={"user_id": profile_id}, timeout=timeout)
        self._assert_success(payload, context="obter browser ativo")
        data = payload.get("data", {})
        return AdsPowerBrowserInfo(debug_port=data.get("debug_port"), webdriver=data.get("webdriver") or "")

    def stop_browser(self, profile_id: str, timeout: float = 5.0) -> dict:
        return self._get_json("/browser/stop", params={"user_id": profile_id}, timeout=timeout)


@dataclass
class SiteTabState:
    index: int
    url: str
    handle: str = ""
    placement: WindowPlacement | None = None
    role_label: str = ""
    role_class: str = "slave"
    retries: int = 0
    last_reload_at: float = 0.0
    last_capture_at: float = 0.0
    capture_count: int = 0
    last_error: str = ""
    title: str = ""
    current_url: str = ""


@dataclass
class ProfileRuntime:
    profile_id: str
    profile_name: str
    session_id: str = ""
    role_mode: str = "auto"
    state: str = "OPEN"
    closing: bool = False
    dashboard_handle: str = ""
    background_handle: str = ""
    site_tabs: list[SiteTabState] = field(default_factory=list)
    preview_stop: threading.Event = field(default_factory=threading.Event)
    preview_thread: threading.Thread | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class BrowserRunner:
    def __init__(
        self,
        adspower_client: AdsPowerClient,
        event_sink: Callable[[object], None] | None = None,
        temp_root: Path | None = None,
    ) -> None:
        self.adspower_client = adspower_client
        self.event_sink = event_sink
        self.temp_root = temp_root or Path(tempfile.gettempdir())
        self.workspace_data = WorkspaceData()
        self.calibrated_cell: tuple[int, int, int, int] | None = None
        self.calibrated_cells: dict[str, tuple[int, int, int, int]] = {}
        self._drivers: dict[str, webdriver.Chrome] = {}
        self._active_profiles: dict[str, str] = {}
        self._stop_flags: dict[str, threading.Event] = {}
        self._runtimes: dict[str, ProfileRuntime] = {}
        self._drivers_lock = threading.Lock()
        self._closing_lock = threading.Lock()
        self._closing_profiles: set[str] = set()
        self._native_install_lock = threading.Lock()
        self._native_install_done: set[tuple[str, str]] = set()
        self._mirror_drag_state: dict[tuple[str, str], tuple[int, int, str]] = {}

    def set_event_sink(self, event_sink: Callable[[object], None] | None) -> None:
        self.event_sink = event_sink

    def set_workspace_data(self, data: WorkspaceData | None) -> None:
        self.workspace_data = data or WorkspaceData()

    def apply_html5_speed_config(self, config: dict) -> None:
        targets: list[tuple[str, webdriver.Chrome, ProfileRuntime, SiteTabState]] = []
        with self._drivers_lock:
            for profile_id, runtime in list(self._runtimes.items()):
                driver = self._drivers.get(profile_id)
                if not driver:
                    continue
                for site in list(runtime.site_tabs):
                    targets.append((profile_id, driver, runtime, site))
        applied = 0
        for profile_id, driver, runtime, site in targets:
            try:
                if not runtime.lock.acquire(timeout=0.5):
                    self._log(f"[{runtime.profile_name}] Speed HTML5 ignorado: driver ocupado nesta aba.", level="WARN")
                    continue
                try:
                    previous_handle = None
                    try:
                        previous_handle = driver.current_window_handle
                    except Exception:
                        previous_handle = None
                    if site.handle:
                        self._switch_to_handle(driver, site.handle)
                    self._apply_html5_speed_on_driver(driver, config)
                    if previous_handle:
                        self._switch_to_handle(driver, previous_handle)
                finally:
                    runtime.lock.release()
                applied += 1
            except Exception as exc:
                self._log(f"[{runtime.profile_name}] Falha ao aplicar speed HTML5 em {site.current_url or site.url}: {exc}", level="WARN")
        if applied:
            status = "ativado" if config.get("enabled") else "desativado"
            self._log(f"Speed HTML5 {status} aplicado em {applied} aba(s).", level="INFO")

    def _apply_html5_speed_on_driver(self, driver: webdriver.Chrome, config: dict) -> None:
        try:
            self._log(
                f"Speed HTML5 driver etapa: iniciar apply enabled={bool(config.get('enabled'))} speed={float(config.get('speed', 1.0) or 1.0):.2f}",
                level="INFO",
            )
        except Exception:
            pass
        result = driver.execute_script(
            """
            const cfg = arguments[0];
            window.__ltdfSpeedInitialConfig = cfg;
            const directSpeed = cfg && cfg.enabled ? Number(cfg.speed || 1) || 1 : 1.0;
            const directSpeedUpdatedAt = Date.now();
            try {
                window.localStorage.setItem('ltdf_speed_value', String(directSpeed));
                window.localStorage.setItem('ltdf_speed_updated_at', String(directSpeedUpdatedAt));
            } catch (_) {}
            try {
                let input = document.getElementById('ltdf-speed-value');
                if (!input && document.documentElement) {
                    input = document.createElement('input');
                    input.type = 'hidden';
                    input.id = 'ltdf-speed-value';
                    input.setAttribute('data-ltdf-speed', '1');
                    document.documentElement.appendChild(input);
                }
                if (input) input.value = String(directSpeed);
            } catch (_) {}
            if (typeof window.setSpeedConfig === 'function') {
                window.setSpeedConfig(directSpeed);
            } else {
                window._ltdfSpeed = directSpeed;
            }
            if (typeof window.__ltdfApplySpeedConfig === 'function') {
                window.__ltdfApplySpeedConfig(cfg, 'execute_script');
            } else {
                window._ltdfSpeedConfig = cfg;
                window._ltdfSpeed = cfg && cfg.enabled ? Number(cfg.speed || 1) || 1 : 1.0;
            }
            window.postMessage({ command: 'setSpeedConfig', config: cfg }, '*');
            return {
                speed: window._ltdfSpeed,
                targetSpeed: window._ltdfTargetSpeed,
                userActivated: !!window._ltdfUserActivated,
                ready: !!window._ltdfReady,
                rampStartSpeed: window._ltdfRampStartSpeed,
                rampTargetSpeed: window._ltdfRampTargetSpeed,
                rampStartedAt: window._ltdfRampStartedAt,
                rampDurationMs: window._ltdfRampDurationMs,
                hasGlobalSetSpeedConfig: typeof window.setSpeedConfig === 'function',
                hasApply: typeof window.__ltdfApplySpeedConfig === 'function',
                needsInjection: typeof window.setSpeedConfig !== 'function' || typeof window.__ltdfApplySpeedConfig !== 'function',
                localStorageSpeed: (function () { try { return window.localStorage.getItem('ltdf_speed_value'); } catch (_) { return null; } })(),
                inputSpeed: (document.getElementById('ltdf-speed-value') || {}).value || null,
                debug: window.__ltdfSpeedDebug || null,
                config: window._ltdfSpeedConfig || null
            };
            """,
            config,
        )
        if bool((result or {}).get("needsInjection")):
            self._log("Speed HTML5: injetor ausente na aba, instalando via CDP para evitar CSP.", level="WARN")
            try:
                driver.execute_cdp_cmd(
                    "Runtime.evaluate",
                    {
                        "expression": SPEED_HACK_PAGE_SCRIPT + "\n//# sourceURL=ltdf_speed_hack_runner.js",
                        "awaitPromise": False,
                    },
                )
            except Exception:
                driver.execute_script(
                    """
                    const source = arguments[0];
                    const script = document.createElement('script');
                    script.type = 'text/javascript';
                    script.textContent = `${source}\n//# sourceURL=ltdf_speed_hack_runner.js`;
                    (document.documentElement || document.head || document.body).appendChild(script);
                    script.remove();
                    """,
                    SPEED_HACK_PAGE_SCRIPT,
                )
            result = driver.execute_script(
                """
                const cfg = arguments[0];
                const directSpeed = cfg && cfg.enabled ? Number(cfg.speed || 1) || 1 : 1.0;
                if (typeof window.setSpeedConfig === 'function') window.setSpeedConfig(directSpeed);
                if (typeof window.__ltdfApplySpeedConfig === 'function') window.__ltdfApplySpeedConfig(cfg, 'execute_script');
                return {
                    speed: window._ltdfSpeed,
                    targetSpeed: window._ltdfTargetSpeed,
                    userActivated: !!window._ltdfUserActivated,
                    ready: !!window._ltdfReady,
                    hasGlobalSetSpeedConfig: typeof window.setSpeedConfig === 'function',
                    hasApply: typeof window.__ltdfApplySpeedConfig === 'function',
                    localStorageSpeed: (function () { try { return window.localStorage.getItem('ltdf_speed_value'); } catch (_) { return null; } })(),
                    inputSpeed: (document.getElementById('ltdf-speed-value') || {}).value || null,
                    debug: window.__ltdfSpeedDebug || null,
                    config: window._ltdfSpeedConfig || null
                };
                """,
                config,
            )
        try:
            self._log("Speed HTML5 driver etapa: tentando turbo nativo e clique pronto.", level="INFO")
            native = self._activate_native_turbo_on_driver(driver)
            if native.get("ok"):
                self._log(
                    f"Turbo nativo acionado: turbo={native.get('turbo')} ready_click={native.get('ready_click')}",
                    level="INFO",
                )
            elif native.get("reason") == "native_ui_not_ready":
                self._log("Turbo nativo em espera: interface principal ainda nao carregou.", level="INFO")
            else:
                self._log(f"Turbo nativo nao confirmado, fallback JS grafico: {native.get('reason')}", level="WARN")
                native = self._apply_js_game_speed_on_driver(driver, config)
            if native.get("attempted"):
                self._log(
                    f"Speed HTML5 JS grafico: alvo={native.get('target', 1)} clicks={native.get('clicks', 0)} rect={native.get('rect')}",
                    level="INFO",
                )
        except Exception as exc:
            self._log(f"Turbo nativo / Speed HTML5 JS grafico falhou: {exc}", level="WARN")
        try:
            debug = (result or {}).get("debug") or {}
            self._log(
                f"Speed HTML5 execute_script confirmou speed={float((result or {}).get('speed', 1.0)):.2f} "
                f"target={float((result or {}).get('targetSpeed', 1.0)):.2f} "
                f"userActivated={bool((result or {}).get('userActivated'))} "
                f"ready={bool((result or {}).get('ready'))} "
                f"hasSetSpeed={bool((result or {}).get('hasGlobalSetSpeedConfig'))} "
                f"hasApply={bool((result or {}).get('hasApply'))} "
                f"ls={str((result or {}).get('localStorageSpeed'))} "
                f"input={str((result or {}).get('inputSpeed'))} "
                f"debugSpeed={float(debug.get('speed', 1.0)):.2f} "
                f"debugTarget={float(debug.get('targetSpeed', 1.0)):.2f}",
                level="INFO",
            )
        except Exception:
            pass
    @staticmethod
    def _find_game_rect_in_current_context(driver: webdriver.Chrome) -> dict:
        return driver.execute_script(
            """
            const candidates = Array.from(document.querySelectorAll('canvas, iframe, game, [id*=game], [class*=game]'))
              .map((el) => {
                const r = el.getBoundingClientRect();
                const area = Math.max(1, r.width * r.height);
                const aspect = r.width / Math.max(1, r.height);
                const mobileScore = (r.height >= 300 ? 1000 : 0)
                  + (r.width <= 520 ? 700 : 0)
                  + (aspect <= 0.85 ? 500 : 0)
                  - Math.abs(aspect - 0.46) * 300
                  - Math.max(0, r.width - 520)
                  + Math.min(300, area / 2000);
                return { x: r.left, y: r.top, width: r.width, height: r.height, tag: el.tagName, aspect, score: mobileScore };
              })
              .filter((r) => r.width >= 60 && r.height >= 160 && r.x < innerWidth && r.y < innerHeight);
            candidates.sort((a, b) => b.score - a.score);
            if (candidates.length) return candidates[0];
            return { x: 0, y: 0, width: innerWidth || 430, height: innerHeight || 932, tag: 'viewport', score: 0 };
            """
        ) or {}

    @staticmethod
    def _dispatch_js_click_on_driver(driver: webdriver.Chrome, x: float, y: float, selector: str = "") -> dict:
        return driver.execute_script(
            """
            const x = Math.max(1, Math.floor(Number(arguments[0]) || 1));
            const y = Math.max(1, Math.floor(Number(arguments[1]) || 1));
            const selector = String(arguments[2] || '');
            const target = document.elementFromPoint(x, y) || (selector ? document.querySelector(selector) : null) || document.body;
            if (!target) return { ok:false, reason:'no_target', x, y };
            const common = { bubbles:true, cancelable:true, composed:true, clientX:x, clientY:y, screenX:x, screenY:y, view:window };
            const pointer = { ...common, pointerId:1, pointerType:'touch', isPrimary:true, button:0, buttons:1 };
            const mouseDown = { ...common, button:0, buttons:1 };
            const mouseUp = { ...common, button:0, buttons:0 };
            try { if (typeof target.focus === 'function') target.focus({ preventScroll:true }); } catch (_) {}
            const send = (Ctor, type, opts) => {
                if (typeof Ctor !== 'function') return false;
                try { target.dispatchEvent(new Ctor(type, opts)); return true; } catch (_) { return false; }
            };
            send(window.PointerEvent, 'pointerover', pointer);
            send(window.PointerEvent, 'pointerenter', pointer);
            send(window.PointerEvent, 'pointermove', pointer);
            send(window.PointerEvent, 'pointerdown', pointer);
            send(window.MouseEvent, 'mouseover', mouseDown);
            send(window.MouseEvent, 'mousemove', mouseDown);
            send(window.MouseEvent, 'mousedown', mouseDown);
            try {
                const touch = new Touch({ identifier:1, target, clientX:x, clientY:y, screenX:x, screenY:y, pageX:x + scrollX, pageY:y + scrollY });
                target.dispatchEvent(new TouchEvent('touchstart', { bubbles:true, cancelable:true, composed:true, touches:[touch], targetTouches:[touch], changedTouches:[touch] }));
                target.dispatchEvent(new TouchEvent('touchend', { bubbles:true, cancelable:true, composed:true, touches:[], targetTouches:[], changedTouches:[touch] }));
            } catch (_) {}
            send(window.PointerEvent, 'pointerup', { ...pointer, buttons:0 });
            send(window.MouseEvent, 'mouseup', mouseUp);
            send(window.MouseEvent, 'click', mouseUp);
            try {
                if (typeof target.click === 'function' && !/^(canvas|html|body)$/i.test(target.tagName || '')) target.click();
            } catch (_) {}
            return { ok:true, x, y, tag:(target.tagName || '').toLowerCase(), id:target.id || '', cls:String(target.className || '') };
            """,
            float(x),
            float(y),
            selector,
        ) or {}

    def _build_game_speed_click_points(self, rect: dict) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        width = float(rect.get("width") or 0)
        height = float(rect.get("height") or 0)
        left = float(rect.get("x") or 0)
        top = float(rect.get("y") or 0)
        scan_y = (0.80, 0.82, 0.85, 0.885, 0.905, 0.93)
        minus_x = (0.38, 0.41, 0.46)
        plus_x = (0.88, 0.90, 0.93, 0.95)
        minus_points = [(left + (width * x_ratio), top + (height * y_ratio)) for y_ratio in scan_y for x_ratio in minus_x]
        plus_points = [(left + (width * x_ratio), top + (height * y_ratio)) for y_ratio in scan_y for x_ratio in plus_x]
        return minus_points, plus_points

    def _fire_game_speed_clicks(self, driver: webdriver.Chrome, minus_points: list[tuple[float, float]], plus_points: list[tuple[float, float]], target_steps: int) -> int:
        def js_click(x: float, y: float) -> None:
            self._dispatch_js_click_on_driver(driver, x, y)
            time.sleep(NETWORK_PROXY_MIN_CLICK_INTERVAL_SECONDS)

        for _ in range(4):
            for point in minus_points:
                js_click(*point)

        step_count = max(0, target_steps - 1)
        fired = 0
        for _ in range(step_count):
            for point in plus_points:
                js_click(*point)
                fired += 1
        return fired

    def _apply_js_game_speed_on_driver(self, driver: webdriver.Chrome, config: dict) -> dict:
        """Use the game's own speed control through JavaScript DOM clicks.

        This avoids clock/timer hooks and also avoids native mouse
        movement. Coordinates are relative to the largest visible canvas/iframe,
        matching the +/- control shown near the lower edge of the mobile viewport.
        """
        try:
            enabled = bool(config.get("enabled"))
            target_speed = float(config.get("speed", 1.0) or 1.0) if enabled else 1.0
        except Exception:
            target_speed = 1.0
        target_steps = int(max(1, min(HTML5_SPEED_MAX_MULTIPLIER, round(target_speed))))
        if target_steps <= 1:
            return {"attempted": False, "target": target_steps, "clicks": 0, "rect": None}

        self._focus_game_iframe_after_load(driver, wait_seconds=0)
        rect = self._find_game_rect_in_current_context(driver)
        width = float(rect.get("width") or 0)
        height = float(rect.get("height") or 0)
        if width <= 0 or height <= 0:
            return {"attempted": False, "target": target_steps, "clicks": 0, "rect": rect}

        minus_points, plus_points = self._build_game_speed_click_points(rect)
        fired = self._fire_game_speed_clicks(driver, minus_points, plus_points, target_steps)
        return {"attempted": True, "target": target_steps, "clicks": fired, "rect": rect}

    def _activate_native_turbo_on_driver(self, driver: webdriver.Chrome) -> dict:
        result = driver.execute_script(
            """
            const ready = typeof window.__ltdfHasMainGameInterface === 'function'
              ? window.__ltdfHasMainGameInterface()
              : false;
            if (!ready) {
              return { ok:false, reason:'native_ui_not_ready', ready:false, debug: window.__ltdfSpeedDebug || null };
            }
            const turbo = typeof window.__ltdfActivateNativeTurbo === 'function'
              ? window.__ltdfActivateNativeTurbo('python_apply')
              : { ok:false, reason:'turbo_helper_missing' };
            const readyClick = typeof window.__ltdfClickActionWhenReady === 'function'
              ? window.__ltdfClickActionWhenReady('python_ready_probe')
              : { ok:false, reason:'ready_helper_missing' };
            return { ok: !!(turbo && turbo.ok), ready:true, turbo, ready_click: readyClick, debug: window.__ltdfSpeedDebug || null };
            """
        ) or {}
        return dict(result or {})
    def set_calibrated_cell(self, cell: tuple[int, int, int, int] | None) -> None:
        self.calibrated_cell = cell
        if cell is not None:
            monitor = detect_monitor_from_cell(cell)
            self.calibrated_cells[monitor_storage_key(monitor)] = tuple(int(v) for v in cell)

    def set_calibrated_cells(self, cells: dict[str, tuple[int, int, int, int]] | None) -> None:
        normalized: dict[str, tuple[int, int, int, int]] = {}
        for key, value in (cells or {}).items():
            if value is None:
                continue
            normalized[str(key)] = tuple(int(v) for v in value)
        self.calibrated_cells = normalized

    def _emit(self, event: object) -> None:
        if self.event_sink:
            self.event_sink(event)

    def _log(self, message: str, level: str = "INFO") -> None:
        self._emit(LogEvent(level=level, message=message, source="browser_runner"))

    def _is_fatal_session_error(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(marker in msg for marker in FATAL_SESSION_ERRORS)

    def request_native_install_confirm(self, session_id: str, domain: str = "", reason: str = "") -> None:
        domain_key = str(domain or "").lower()
        if domain_key:
            self._native_install_done.discard((session_id, domain_key))
        thread = threading.Thread(
            target=self._native_install_confirm_worker,
            args=(session_id, domain, reason),
            daemon=True,
            name=f"native-install-{session_id[:8]}",
        )
        thread.start()

    def request_native_popup_download_click(self, session_id: str, domain: str = "", x_ratio: float = 0.5, y_ratio: float = 0.5) -> None:
        thread = threading.Thread(
            target=self._native_popup_download_click_worker,
            args=(session_id, domain, x_ratio, y_ratio),
            daemon=True,
            name=f"native-popup-click-{session_id[:8]}",
        )
        thread.start()

    def handle_pwa_installed(self, session_id: str, domain: str = "") -> None:
        domain_key = str(domain or "").lower()
        if domain_key:
            self._native_install_done.add((session_id, domain_key))
            self._log(f"[{domain or '?'}] Instalacao marcada como concluida; iniciando troca da origem pela instalada.", level="INFO")
            thread = threading.Thread(
                target=self._adopt_installed_window_after_install_worker,
                args=(session_id, domain),
                daemon=True,
                name=f"adopt-installed-{session_id[:8]}",
            )
            thread.start()
            close_thread = threading.Thread(
                target=self._close_origin_after_install_worker,
                args=(session_id, domain),
                daemon=True,
                name=f"close-origin-{session_id[:8]}",
            )
            close_thread.start()

    def _native_popup_download_click_worker(self, session_id: str, domain: str = "", x_ratio: float = 0.5, y_ratio: float = 0.5) -> None:
        if sys.platform != "win32":
            self._log(f"[{domain or '?'}] Clique nativo do popup indisponivel fora do Windows.", level="WARN")
            return
        try:
            runtime = self._find_runtime_by_session(session_id)
            if runtime is None:
                runtime = self._find_runtime_by_domain(domain=domain, preferred_session=session_id)
            driver = self._drivers.get(runtime.profile_id) if runtime else None
            if runtime is None or driver is None:
                self._log(f"[{domain or '?'}] Clique nativo do popup cancelado: runtime indisponivel.", level="WARN")
                return
            with runtime.lock:
                target_site = None
                normalized = str(domain or "").lower()
                for site in list(runtime.site_tabs):
                    current = str(site.current_url or site.url or "").lower()
                    if normalized and normalized in current:
                        target_site = site
                        break
                if target_site and target_site.handle:
                    self._switch_to_handle(driver, target_site.handle)
                try:
                    driver.execute_cdp_cmd("Page.bringToFront", {})
                except Exception:
                    pass
                try:
                    driver.execute_script("window.focus();")
                except Exception:
                    pass
                screen_x, screen_y = self._viewport_point_to_screen(driver, float(x_ratio), float(y_ratio))
            time.sleep(0.15)
            self._native_mouse_click(screen_x, screen_y)
            self._log(f"[{domain or '?'}] Clique nativo no botao do popup executado em ({screen_x},{screen_y}).", level="INFO")
        except Exception as exc:
            self._log(f"[{domain or '?'}] Falha ao clicar nativamente no botao do popup: {exc}", level="WARN")

    def _native_install_confirm_worker(self, session_id: str, domain: str = "", reason: str = "") -> None:
        if sys.platform != "win32":
            self._log(f"[{domain or '?'}] Confirmacao nativa de PWA indisponivel fora do Windows.", level="WARN")
            return
        domain_key = str(domain or "").lower()
        self._log(
            f"[{domain or '?'}] Tentando confirmar prompt nativo de instalacao ({reason or 'sem motivo'})",
            level="INFO",
        )
        try:
            runtime = self._find_runtime_by_session(session_id)
            if runtime is None:
                runtime = self._find_runtime_by_domain(domain=domain, preferred_session=session_id)
            driver = self._drivers.get(runtime.profile_id) if runtime else None
            if runtime is None or driver is None:
                self._log(f"[{domain or '?'}] Confirmacao nativa cancelada: runtime indisponivel.", level="INFO")
                return
            with self._native_install_lock:
                normalized = domain_key
                for attempt in range(1, 7):
                    if normalized and (session_id, normalized) in self._native_install_done:
                        self._log(f"[{domain or '?'}] Confirmacao nativa encerrada: instalacao ja confirmada.", level="INFO")
                        return
                    install_handle = None
                    if driver and runtime and domain:
                        try:
                            with runtime.lock:
                                current_handle = driver.current_window_handle
                                for handle in list(driver.window_handles):
                                    try:
                                        self._switch_to_handle(driver, handle)
                                        current_url = str(driver.current_url or "").lower()
                                        if "__ltdf_install__=1" in current_url and normalized in current_url:
                                            install_handle = handle
                                            break
                                    except Exception:
                                        continue
                                if install_handle:
                                    self._switch_to_handle(driver, install_handle)
                                    try:
                                        driver.execute_cdp_cmd("Page.bringToFront", {})
                                    except Exception:
                                        pass
                                    try:
                                        driver.execute_script("window.focus();")
                                    except Exception:
                                        pass
                                else:
                                    self._switch_to_handle(driver, current_handle)
                        except Exception as exc:
                            self._log(f"[{domain or '?'}] Falha ao trazer janela de instalacao para frente: {exc}", level="WARN")
                    time.sleep(0.8)
                    if normalized and (session_id, normalized) in self._native_install_done:
                        self._log(f"[{domain or '?'}] Confirmacao nativa encerrada antes da tentativa {attempt}: instalacao ja confirmada.", level="INFO")
                        return
                    clicked = False
                    if domain:
                        try:
                            clicked = self._click_install_button_ui(domain)
                        except Exception as exc:
                            self._log(f"[{domain or '?'}] Falha na automacao UI do botao Instalar: {exc}", level="WARN")
                    if clicked:
                        self._log(f"[{domain or '?'}] Clique por UI Automation no botao Instalar (tentativa {attempt}).", level="INFO")
                    if driver and install_handle:
                        try:
                            if not clicked:
                                with runtime.lock:
                                    self._switch_to_handle(driver, install_handle)
                                    rect = driver.get_window_rect()
                                clicked = self._click_install_button_native(rect)
                        except Exception as exc:
                            self._log(f"[{domain or '?'}] Falha no clique nativo do botao Instalar: {exc}", level="WARN")
                    if clicked:
                        self._log(f"[{domain or '?'}] Clique nativo no botao Instalar enviado ao prompt PWA (tentativa {attempt}).", level="INFO")
                    else:
                        for _ in range(2):
                            self._send_vk_combo(0x10, 0x09)  # Shift+Tab
                            time.sleep(0.12)
                            self._send_vk(0x0D)  # Enter
                            time.sleep(0.18)
                            self._send_vk(0x25)  # Left
                            time.sleep(0.10)
                            self._send_vk(0x0D)  # Enter
                            time.sleep(0.18)
                            self._send_vk(0x09)  # Tab
                            time.sleep(0.10)
                            self._send_vk(0x0D)  # Enter
                            time.sleep(0.25)
                        self._log(f"[{domain or '?'}] Sequencia nativa de confirmacao enviada ao prompt PWA (tentativa {attempt}).", level="INFO")
                    time.sleep(2.0)
        except Exception as exc:
            self._log(f"[{domain or '?'}] Falha ao confirmar prompt nativo de instalacao: {exc}", level="WARN")

    def _close_origin_after_install_worker(self, session_id: str, domain: str = "") -> None:
        runtime = self._find_runtime_by_session(session_id)
        if runtime is None:
            runtime = self._find_runtime_by_domain(domain=domain, preferred_session=session_id)
        if not runtime or not domain:
            return
        driver = self._drivers.get(runtime.profile_id)
        if driver is None:
            return
        normalized = str(domain).lower()
        try:
            for attempt in range(1, 5):
                with runtime.lock:
                    target_site = None
                    for site in list(runtime.site_tabs):
                        current = str(site.current_url or site.url or "").lower()
                        if normalized in current and "__ltdf_install__=1" not in current:
                            target_site = site
                            break
                    if not target_site:
                        self._log(f"[{domain}] Nenhuma janela de origem encontrada para fechar apos instalacao.", level="WARN")
                        return
                    self._switch_to_handle(driver, target_site.handle)
                    driver.close()
                    runtime.site_tabs = [site for site in runtime.site_tabs if site.handle != target_site.handle]
                    self._log(f"[{domain}] Janela de origem fechada apos confirmacao da instalacao PWA (tentativa {attempt}).", level="INFO")
                    return
                time.sleep(0.4)
        except Exception as exc:
            self._log(f"[{domain}] Falha ao fechar janela de origem apos instalacao: {exc}", level="WARN")

    def _adopt_installed_window_after_install_worker(self, session_id: str, domain: str = "") -> None:
        runtime = self._find_runtime_by_session(session_id)
        if runtime is None:
            runtime = self._find_runtime_by_domain(domain=domain, preferred_session=session_id)
        if not runtime or not domain:
            return
        driver = self._drivers.get(runtime.profile_id)
        if driver is None:
            return
        normalized = str(domain).lower()
        time.sleep(1.2)
        try:
            self._log(f"[{domain}] Iniciando adocao da janela instalada apos confirmacao.", level="INFO")
            with runtime.lock:
                handles = list(driver.window_handles)
                origin_site = None
                for site in list(runtime.site_tabs):
                    current = str(site.current_url or site.url or "").lower()
                    if normalized in current and site.role_class == "master":
                        origin_site = site
                        break
                if origin_site is None:
                    for site in list(runtime.site_tabs):
                        current = str(site.current_url or site.url or "").lower()
                        if normalized in current:
                            origin_site = site
                            break
                if origin_site is None:
                    self._log(f"[{domain}] Nao encontrei janela de origem para adotar a instalada.", level="WARN")
                    return

                install_handle = None
                install_url = ""
                workspace_handles: list[str] = []
                for handle in handles:
                    try:
                        self._switch_to_handle(driver, handle)
                        current_url = str(driver.current_url or "")
                        current_url_lower = current_url.lower()
                        standalone = False
                        try:
                            standalone = bool(driver.execute_script(
                                "return !!((window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) || window.navigator.standalone);"
                            ))
                        except Exception:
                            standalone = False
                        if "workspace.google.com" in current_url_lower:
                            workspace_handles.append(handle)
                        if handle != origin_site.handle and (standalone or normalized in current_url_lower):
                            install_handle = handle
                            install_url = current_url
                            break
                    except Exception:
                        continue

                if install_handle and origin_site.placement is not None:
                    self._switch_to_handle(driver, install_handle)
                    self._apply_site_window_layout(driver, origin_site.placement)
                    origin_handle = origin_site.handle
                    origin_site.handle = install_handle
                    origin_site.current_url = install_url or origin_site.current_url
                    self._log(
                        f"[{domain}] Janela instalada adotada no slot da grade x={origin_site.placement.x} y={origin_site.placement.y} w={origin_site.placement.width} h={origin_site.placement.height}.",
                        level="INFO",
                    )
                    if origin_handle and origin_handle != install_handle:
                        try:
                            self._switch_to_handle(driver, origin_handle)
                            driver.close()
                            runtime.site_tabs = [site for site in runtime.site_tabs if site.handle != origin_handle]
                            self._log(f"[{domain}] Aba nativa/origem fechada apos instalacao.", level="INFO")
                        except Exception as exc:
                            self._log(f"[{domain}] Falha ao fechar aba nativa/origem: {exc}", level="WARN")
                    for workspace_handle in workspace_handles:
                        if workspace_handle in {install_handle, origin_site.handle}:
                            continue
                        try:
                            self._switch_to_handle(driver, workspace_handle)
                            driver.close()
                            self._log(f"[{domain}] Janela auxiliar workspace.google.com fechada.", level="INFO")
                        except Exception as exc:
                            self._log(f"[{domain}] Falha ao fechar janela auxiliar workspace.google.com: {exc}", level="WARN")
                    self._close_external_workspace_windows()
                elif origin_site.placement is not None and self._move_external_installed_window_to_placement(normalized, origin_site.placement):
                    self._log(
                        f"[{domain}] Janela instalada externa movida para o slot da grade x={origin_site.placement.x} y={origin_site.placement.y} w={origin_site.placement.width} h={origin_site.placement.height}.",
                        level="INFO",
                    )
                    origin_handle = origin_site.handle
                    if origin_handle:
                        try:
                            self._switch_to_handle(driver, origin_handle)
                            driver.close()
                            runtime.site_tabs = [site for site in runtime.site_tabs if site.handle != origin_handle]
                            self._log(f"[{domain}] Aba nativa/origem fechada apos instalacao.", level="INFO")
                        except Exception as exc:
                            self._log(f"[{domain}] Falha ao fechar aba nativa/origem: {exc}", level="WARN")
                    for workspace_handle in workspace_handles:
                        try:
                            self._switch_to_handle(driver, workspace_handle)
                            driver.close()
                            self._log(f"[{domain}] Janela auxiliar workspace.google.com fechada.", level="INFO")
                        except Exception as exc:
                            self._log(f"[{domain}] Falha ao fechar janela auxiliar workspace.google.com: {exc}", level="WARN")
                    self._close_external_workspace_windows()
                else:
                    self._log(f"[{domain}] Janela instalada nao encontrada para adocao do slot.", level="WARN")
                    self._close_external_workspace_windows()
        except Exception as exc:
            self._log(f"[{domain}] Falha ao adotar janela instalada apos confirmacao: {exc}", level="WARN")

    def _close_external_workspace_windows(self) -> None:
        if sys.platform != "win32" or Desktop is None:
            return
        try:
            desktop = Desktop(backend="uia")
            for window in desktop.windows():
                try:
                    title = str(window.window_text() or "")
                except Exception:
                    continue
                if not title:
                    continue
                lower = title.lower()
                if "workspace.google.com" not in lower and "404 page" not in lower and "google workspace" not in lower:
                    continue
                try:
                    window.close()
                    self._log("Janela externa workspace/google fechada.", level="INFO")
                except Exception as exc:
                    self._log(f"Falha ao fechar janela externa workspace/google: {exc}", level="WARN")
        except Exception as exc:
            self._log(f"Falha ao varrer janelas externas workspace/google: {exc}", level="WARN")

    @staticmethod
    def _foreground_window_title() -> tuple[int, str]:
        if sys.platform != "win32":
            return 0, ""
        user32 = ctypes.windll.user32
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return 0, ""
        length = int(user32.GetWindowTextLengthW(hwnd) or 0)
        buffer = ctypes.create_unicode_buffer(max(256, length + 4))
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return hwnd, str(buffer.value or "")

    def _move_external_installed_window_to_placement(self, domain: str, placement: WindowPlacement) -> bool:
        if sys.platform != "win32":
            return False
        user32 = ctypes.windll.user32
        hwnd, title = self._foreground_window_title()
        domain = str(domain or "").lower()
        if hwnd and title and domain and domain in title.lower():
            try:
                user32.MoveWindow(
                    int(hwnd),
                    int(placement.x),
                    int(placement.y),
                    int(placement.width),
                    int(placement.height),
                    True,
                )
                return True
            except Exception:
                pass
        if Desktop is None:
            return False
        try:
            desktop = Desktop(backend="uia")
            candidates = []
            for window in desktop.windows():
                try:
                    title = str(window.window_text() or "")
                except Exception:
                    continue
                if not title:
                    continue
                lower = title.lower()
                if domain and domain not in lower:
                    continue
                if "workspace" in lower or "404" in lower or "ltdf sniper" in lower:
                    continue
                candidates.append(window)
            for window in candidates:
                try:
                    window.set_focus()
                except Exception:
                    pass
                try:
                    rect = window.rectangle()
                    hwnd = int(window.handle)
                    user32.MoveWindow(
                        hwnd,
                        int(placement.x),
                        int(placement.y),
                        int(placement.width),
                        int(placement.height),
                        True,
                    )
                    return True
                except Exception:
                    continue
        except Exception:
            return False
        return False

    @staticmethod
    def _send_vk(vk_code: int) -> None:
        user32 = ctypes.windll.user32
        user32.keybd_event(vk_code, 0, 0, 0)
        time.sleep(0.04)
        user32.keybd_event(vk_code, 0, 2, 0)

    @classmethod
    def _send_vk_combo(cls, modifier_vk: int, key_vk: int) -> None:
        user32 = ctypes.windll.user32
        user32.keybd_event(modifier_vk, 0, 0, 0)
        time.sleep(0.03)
        user32.keybd_event(key_vk, 0, 0, 0)
        time.sleep(0.04)
        user32.keybd_event(key_vk, 0, 2, 0)
        time.sleep(0.03)
        user32.keybd_event(modifier_vk, 0, 2, 0)

    @staticmethod
    def _native_mouse_click(screen_x: int, screen_y: int) -> None:
        user32 = ctypes.windll.user32
        user32.SetCursorPos(int(screen_x), int(screen_y))
        time.sleep(0.05)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.03)
        user32.mouse_event(0x0004, 0, 0, 0, 0)

    @staticmethod
    def _native_mouse_wheel(screen_x: int, screen_y: int, delta_y: float) -> None:
        user32 = ctypes.windll.user32
        user32.SetCursorPos(int(screen_x), int(screen_y))
        time.sleep(0.04)
        wheel_delta = int(max(-1200, min(1200, -delta_y * 2.0)))
        if wheel_delta == 0:
            wheel_delta = -360
        user32.mouse_event(0x0800, 0, 0, wheel_delta, 0)

    @staticmethod
    def _native_drag_swipe(start_x: int, start_y: int, end_x: int, end_y: int, steps: int = 8) -> None:
        user32 = ctypes.windll.user32
        user32.SetCursorPos(int(start_x), int(start_y))
        time.sleep(0.05)
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        time.sleep(0.03)
        for step in range(1, max(2, steps) + 1):
            current_x = int(start_x + ((end_x - start_x) * step / max(2, steps)))
            current_y = int(start_y + ((end_y - start_y) * step / max(2, steps)))
            user32.SetCursorPos(current_x, current_y)
            time.sleep(0.02)
        user32.mouse_event(0x0004, 0, 0, 0, 0)

    @staticmethod
    def _viewport_point_to_screen(driver: webdriver.Chrome, x_ratio: float, y_ratio: float) -> tuple[int, int]:
        metrics = driver.execute_script(
            """
            return {
                screenX: window.screenX || window.screenLeft || 0,
                screenY: window.screenY || window.screenTop || 0,
                outerWidth: window.outerWidth || 0,
                outerHeight: window.outerHeight || 0,
                innerWidth: window.innerWidth || document.documentElement.clientWidth || 430,
                innerHeight: window.innerHeight || document.documentElement.clientHeight || 932,
            };
            """
        ) or {}
        screen_x = int(metrics.get("screenX") or 0)
        screen_y = int(metrics.get("screenY") or 0)
        outer_width = max(1, int(metrics.get("outerWidth") or 430))
        outer_height = max(1, int(metrics.get("outerHeight") or 932))
        inner_width = max(1, int(metrics.get("innerWidth") or 430))
        inner_height = max(1, int(metrics.get("innerHeight") or 932))
        border_x = max(0, int((outer_width - inner_width) / 2))
        top_chrome = max(0, int(outer_height - inner_height - border_x))
        point_x = screen_x + border_x + max(1, min(inner_width - 1, int(inner_width * x_ratio)))
        point_y = screen_y + top_chrome + max(1, min(inner_height - 1, int(inner_height * y_ratio)))
        return int(point_x), int(point_y)


    @staticmethod
    def _click_install_button_ui(domain: str = "") -> bool:
        if Desktop is None:
            return False
        title_patterns = [
            re.compile(r".*Instale o app.*", re.IGNORECASE),
            re.compile(r".*Instalar la app.*", re.IGNORECASE),
            re.compile(r".*Install app.*", re.IGNORECASE),
        ]
        button_patterns = [
            re.compile(r"^Instalar$", re.IGNORECASE),
            re.compile(r"^Install$", re.IGNORECASE),
        ]
        domain = str(domain or "").lower()
        try:
            desktop = Desktop(backend="uia")
            for window in desktop.windows():
                try:
                    title = str(window.window_text() or "")
                except Exception:
                    continue
                if not any(pattern.match(title) for pattern in title_patterns):
                    continue
                try:
                    texts = " ".join(filter(None, [title, window.element_info.name or ""])).lower()
                    if domain and domain not in texts:
                        try:
                            descendants = window.descendants(control_type="Text")
                            texts = " ".join(str(item.window_text() or "") for item in descendants).lower()
                        except Exception:
                            texts = title.lower()
                    if domain and domain not in texts:
                        continue
                    window.set_focus()
                    for button in window.descendants(control_type="Button"):
                        try:
                            label = str(button.window_text() or "").strip()
                        except Exception:
                            continue
                        if any(pattern.match(label) for pattern in button_patterns):
                            try:
                                button.click_input()
                            except Exception:
                                try:
                                    button.invoke()
                                except Exception:
                                    continue
                            return True
                except Exception:
                    continue
        except Exception:
            return False
        return False

    @staticmethod
    def _click_install_button_native(rect: dict) -> bool:
        left = int(rect.get("x", 0))
        top = int(rect.get("y", 0))
        width = int(rect.get("width", 0))
        height = int(rect.get("height", 0))
        if width <= 0 or height <= 0:
            return False
        user32 = ctypes.windll.user32
        # The SunBrowser PWA prompt places the action buttons in the lower-middle
        # area of the window, with "Instalar" left of "Cancelar".
        candidate_points = [
            (0.64, 0.46),
            (0.62, 0.48),
            (0.60, 0.50),
        ]
        for x_ratio, y_ratio in candidate_points:
            click_x = left + int(width * x_ratio)
            click_y = top + int(height * y_ratio)
            user32.SetCursorPos(click_x, click_y)
            time.sleep(0.08)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            time.sleep(0.04)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            time.sleep(0.14)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            time.sleep(0.04)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            time.sleep(0.18)
        return True

    def _slugify(self, value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_-]+", "_", str(value)).strip("_") or "default"

    def _write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _create_stop_flag(self, profile_id: str) -> threading.Event:
        with self._drivers_lock:
            event = self._stop_flags.get(profile_id)
            if event is None:
                event = threading.Event()
                self._stop_flags[profile_id] = event
            else:
                event.clear()
            return event

    def _mark_stop_requested(self, profile_id: str) -> None:
        with self._drivers_lock:
            event = self._stop_flags.get(profile_id)
            if event:
                event.set()

    def _clear_stop_flag(self, profile_id: str) -> None:
        with self._drivers_lock:
            self._stop_flags.pop(profile_id, None)

    def _find_runtime_by_session(self, session_id: str) -> ProfileRuntime | None:
        for runtime in self._runtimes.values():
            if runtime.session_id == session_id:
                return runtime
        return None

    def _find_runtime_by_domain(self, domain: str = "", preferred_session: str = "") -> ProfileRuntime | None:
        normalized = str(domain or "").strip().lower()
        session_value = str(preferred_session or "").strip()
        candidates: list[ProfileRuntime] = []
        for runtime in self._runtimes.values():
            if runtime.state in {"BROKEN", "CLOSING", "CLOSED"}:
                continue
            if session_value and runtime.session_id == session_value:
                candidates.append(runtime)
                continue
            if not normalized:
                candidates.append(runtime)
                continue
            for site in list(runtime.site_tabs):
                current = str(site.current_url or site.url or "").lower()
                if normalized and normalized in current:
                    candidates.append(runtime)
                    break
        if candidates:
            candidates.sort(key=lambda rt: (0 if rt.role_mode != "slave_only" else 1, rt.profile_name.lower()))
            return candidates[0]
        return None

    def _find_runtimes_by_session(self, session_id: str) -> list[ProfileRuntime]:
        return [runtime for runtime in self._runtimes.values() if runtime.session_id == session_id]

    def _resolve_target_monitors(self, settings: VolumeSettings) -> list[DesktopBounds]:
        monitors = list_desktop_monitors()
        choice = str(getattr(settings, "monitor_choice", "") or "Todos os monitores").strip()
        if not monitors:
            return [detect_desktop_bounds()]
        if choice == "Todos os monitores":
            return monitors
        labels = [format_monitor_label(index, monitor) for index, monitor in enumerate(monitors)]
        try:
            idx = labels.index(choice)
            return [monitors[idx]]
        except ValueError:
            pass
        for monitor in monitors:
            if (
                int(settings.window_origin_x) == int(monitor.x)
                and int(settings.window_origin_y) == int(monitor.y)
                and int(settings.window_area_width) == int(monitor.width)
                and int(settings.window_area_height) == int(monitor.height)
            ):
                return [monitor]
        return monitors

    def _get_effective_calibrated_cells(self, settings: VolumeSettings) -> dict[str, tuple[int, int, int, int]]:
        selected = self._resolve_target_monitors(settings)
        result: dict[str, tuple[int, int, int, int]] = {}
        for monitor in selected:
            key = monitor_storage_key(monitor)
            if key in self.calibrated_cells:
                result[key] = self.calibrated_cells[key]
        return result

    def _has_calibration_for_settings(self, settings: VolumeSettings) -> bool:
        return bool(self._get_effective_calibrated_cells(settings))

    def handle_cross_profile_mirror_action(self, session_id: str, data: dict) -> None:
        action = str(data.get("action") or "")
        if not action.startswith("mirror_"):
            return
        runtimes = self._find_runtimes_by_session(session_id)
        target_runtimes = [runtime for runtime in runtimes if runtime.role_mode == "slave_only"]
        if not target_runtimes:
            target_runtimes = [
                runtime for runtime in self._runtimes.values()
                if runtime.role_mode == "slave_only" and runtime.state not in {"BROKEN", "CLOSING", "CLOSED"}
            ]
            self._log(
                f"Espelho direto {action}: fallback global com {len(target_runtimes)} slave(s) ativas.",
                level="WARN",
            )
        else:
            self._log(
                f"Espelho direto {action}: {len(target_runtimes)} slave(s) na sessao {session_id}.",
                level="INFO",
            )
        for runtime in target_runtimes:
            with self._drivers_lock:
                driver = self._drivers.get(runtime.profile_id)
            if driver is None:
                self._log(f"[{runtime.profile_name}] Espelho direto {action}: driver ausente.", level="WARN")
                continue
            site = runtime.site_tabs[0] if runtime.site_tabs else None
            if site is None:
                self._log(f"[{runtime.profile_name}] Espelho direto {action}: slave sem site_tab.", level="WARN")
                continue
            try:
                self._log(
                    f"[{runtime.profile_name}] Espelho direto {action} -> slave {site.index}",
                    level="INFO",
                )
                with runtime.lock:
                    self._switch_to_handle(driver, site.handle)
                    self._apply_site_mobile_view(driver)
                    self._apply_mobile_mirror_action_on_driver(driver, runtime, site, data)
                    self._capture_site_preview_unlocked(driver, runtime, site)
            except Exception as exc:
                self._log(
                    f"[{runtime.profile_name}] Espelho direto {action} falhou na slave {site.index}: {exc}",
                    level="WARN",
                )

    def _apply_mobile_mirror_action_on_driver(
        self,
        driver: webdriver.Chrome,
        runtime: ProfileRuntime,
        site: SiteTabState,
        data: dict,
    ) -> None:
        action = str(data.get("action") or "")
        x_ratio = float(data.get("x_ratio") or 0.5)
        y_ratio = float(data.get("y_ratio") or 0.5)
        delta_x = float(data.get("delta_x") or 0.0)
        delta_y = float(data.get("delta_y") or 0.0)
        scroll_left = float(data.get("scroll_left") or 0.0)
        scroll_top = float(data.get("scroll_top") or 0.0)
        path = str(data.get("path") or "")
        state_key = (runtime.profile_id, site.handle)

        viewport = driver.execute_script(
            "return {width: window.innerWidth || document.documentElement.clientWidth || 430, height: window.innerHeight || document.documentElement.clientHeight || 932};"
        ) or {}
        width = max(1, int(viewport.get("width") or 430))
        height = max(1, int(viewport.get("height") or 932))
        x = max(1, min(width - 1, int(width * x_ratio)))
        y = max(1, min(height - 1, int(height * y_ratio)))

        def _cdp_touch(event_type: str, points: list[dict]) -> None:
            driver.execute_cdp_cmd(
                "Input.dispatchTouchEvent",
                {
                    "type": event_type,
                    "touchPoints": points,
                    "modifiers": 0,
                },
            )

        def _cdp_tap(px: int, py: int) -> None:
            try:
                driver.execute_cdp_cmd(
                    "Input.synthesizeTapGesture",
                    {
                        "x": int(px),
                        "y": int(py),
                        "duration": 60,
                        "tapCount": 1,
                        "gestureSourceType": "touch",
                    },
                )
                return
            except Exception:
                pass
            _touch_tap(px, py)

        def _cdp_scroll_gesture(start_x: int, start_y: int, x_distance: float, y_distance: float) -> None:
            driver.execute_cdp_cmd(
                "Input.synthesizeScrollGesture",
                {
                    "x": int(start_x),
                    "y": int(start_y),
                    "xDistance": float(x_distance),
                    "yDistance": float(y_distance),
                    "speed": 900,
                    "gestureSourceType": "touch",
                    "repeatCount": 0,
                    "repeatDelayMs": 0,
                },
            )

        def _touch_tap(px: int, py: int) -> None:
            point = {"x": int(px), "y": int(py), "radiusX": 1, "radiusY": 1, "force": 1, "id": 1}
            _cdp_touch("touchStart", [point])
            time.sleep(0.03)
            _cdp_touch("touchEnd", [])

        def _touch_swipe(start_x: int, start_y: int, end_x: int, end_y: int, steps: int = 6) -> None:
            start = {"x": int(start_x), "y": int(start_y), "radiusX": 1, "radiusY": 1, "force": 1, "id": 1}
            _cdp_touch("touchStart", [start])
            time.sleep(0.02)
            for step in range(1, max(2, steps) + 1):
                current_x = int(start_x + ((end_x - start_x) * step / max(2, steps)))
                current_y = int(start_y + ((end_y - start_y) * step / max(2, steps)))
                move = {"x": current_x, "y": current_y, "radiusX": 1, "radiusY": 1, "force": 1, "id": 1}
                _cdp_touch("touchMove", [move])
                time.sleep(0.02)
            _cdp_touch("touchEnd", [])

        if action == "mirror_click":
            self._dispatch_js_click_on_driver(driver, x, y, path)
            return

        if action == "mirror_input":
            driver.execute_script(
                """
                const selector = arguments[0] || '';
                const value = String(arguments[1] || '');
                const target = (selector ? document.querySelector(selector) : null) || document.activeElement;
                if (!target || !('value' in target)) return false;
                target.value = value;
                target.dispatchEvent(new Event('input', { bubbles:true }));
                target.dispatchEvent(new Event('change', { bubbles:true }));
                return true;
                """,
                path,
                str(data.get("value") or ""),
            )
            return

        if action == "mirror_scroll":
            effective_dx = delta_x
            effective_dy = delta_y
            if not effective_dx and not effective_dy:
                effective_dy = scroll_top or 0.0
            if effective_dx and abs(effective_dx) < 90:
                effective_dx = 180 if effective_dx > 0 else -180
            if effective_dy and abs(effective_dy) < 90:
                effective_dy = 240 if effective_dy > 0 else -240
            swipe_distance = int(max(60, min(height * 0.45, abs(effective_dy or 120))))
            end_y = y - swipe_distance if effective_dy > 0 else y + swipe_distance
            end_y = max(1, min(height - 1, end_y))
            try:
                gesture_dx = -effective_dx if effective_dx else 0
                gesture_dy = -effective_dy if effective_dy else (y - end_y)
                _cdp_scroll_gesture(x, y, gesture_dx, gesture_dy)
            except Exception:
                try:
                    _touch_swipe(x, y, x, end_y, steps=8)
                except Exception:
                    pass
            driver.execute_script(
                """
                const selector = arguments[0] || '';
                const left = Number(arguments[1] || 0);
                const top = Number(arguments[2] || 0);
                const dx = Number(arguments[3] || 0);
                const dy = Number(arguments[4] || 0);
                const resolveScrollable = (node) => {
                    let current = node instanceof Element ? node : null;
                    while (current && current !== document.body && current !== document.documentElement) {
                        const style = getComputedStyle(current);
                        const canY = /(auto|scroll|overlay)/.test(style.overflowY || '') && current.scrollHeight > current.clientHeight + 4;
                        const canX = /(auto|scroll|overlay)/.test(style.overflowX || '') && current.scrollWidth > current.clientWidth + 4;
                        if (canY || canX) return current;
                        current = current.parentElement;
                    }
                    return null;
                };
                const node = selector ? document.querySelector(selector) : null;
                const target = resolveScrollable(node) || node;
                if (target instanceof Element) {
                    if (dx || dy) {
                        try { target.scrollBy({ left: dx, top: dy, behavior: 'instant' }); } catch (_) { target.scrollLeft += dx; target.scrollTop += dy; }
                    } else {
                        target.scrollLeft = left;
                        target.scrollTop = top;
                    }
                    return true;
                }
                if (dx || dy) {
                    try { window.scrollBy({ left: dx, top: dy, behavior: 'instant' }); } catch (_) { window.scrollBy(dx, dy); }
                } else {
                    try { window.scrollTo({ left, top, behavior: 'instant' }); } catch (_) { window.scrollTo(left, top); }
                }
                return true;
                """,
                path,
                scroll_left,
                scroll_top,
                effective_dx,
                effective_dy,
            )
            if sys.platform == "win32":
                try:
                    screen_x, screen_y = self._viewport_point_to_screen(driver, x_ratio, y_ratio)
                    self._native_mouse_wheel(screen_x, screen_y, effective_dy or -240)
                except Exception:
                    pass
            return

        if action == "mirror_drag_start":
            self._mirror_drag_state[state_key] = (x, y, path)
            try:
                point = {"x": int(x), "y": int(y), "radiusX": 1, "radiusY": 1, "force": 1, "id": 1}
                _cdp_touch("touchStart", [point])
            except Exception:
                pass
            driver.execute_script(
                """
                const x = Number(arguments[0]);
                const y = Number(arguments[1]);
                const el = document.elementFromPoint(x, y) || (arguments[2] ? document.querySelector(arguments[2]) : null);
                if (!el) return false;
                window.__ltdfReplayDrag = { x, y, path: arguments[2] || '' };
                const opts = { bubbles:true, cancelable:true, clientX:x, clientY:y, view:window, button:0, buttons:1, pointerId:1, pointerType:'touch' };
                try { el.dispatchEvent(new PointerEvent('pointerdown', opts)); } catch (_) {}
                try {
                    const touch = new Touch({ identifier: 1, target: el, clientX: x, clientY: y });
                    el.dispatchEvent(new TouchEvent('touchstart', { bubbles:true, cancelable:true, touches:[touch], targetTouches:[touch], changedTouches:[touch] }));
                } catch (_) {}
                return true;
                """,
                x,
                y,
                path,
            )
            return

        if action == "mirror_drag_move":
            prev_x, prev_y, prev_path = self._mirror_drag_state.get(state_key, (x, y, path))
            self._mirror_drag_state[state_key] = (x, y, path or prev_path)
            try:
                dx = prev_x - x
                dy = prev_y - y
                if dx and abs(dx) < 80:
                    dx = 140 if dx > 0 else -140
                if dy and abs(dy) < 80:
                    dy = 180 if dy > 0 else -180
                _cdp_scroll_gesture(prev_x, prev_y, dx, dy)
            except Exception:
                try:
                    point = {"x": int(x), "y": int(y), "radiusX": 1, "radiusY": 1, "force": 1, "id": 1}
                    _cdp_touch("touchMove", [point])
                except Exception:
                    pass
            driver.execute_script(
                """
                const x = Number(arguments[0]);
                const y = Number(arguments[1]);
                const prevX = Number(arguments[2] || 0);
                const prevY = Number(arguments[3] || 0);
                const selector = arguments[4] || '';
                const el = document.elementFromPoint(x, y) || (selector ? document.querySelector(selector) : null) || document.body;
                const dx = prevX - x;
                const dy = prevY - y;
                const findScrollable = (node) => {
                    let current = node instanceof Element ? node : null;
                    while (current && current !== document.body && current !== document.documentElement) {
                        const style = getComputedStyle(current);
                        const canY = /(auto|scroll|overlay)/.test(style.overflowY || '') && current.scrollHeight > current.clientHeight + 4;
                        const canX = /(auto|scroll|overlay)/.test(style.overflowX || '') && current.scrollWidth > current.clientWidth + 4;
                        if (canY || canX) return current;
                        current = current.parentElement;
                    }
                    return null;
                };
                const scrollTarget = findScrollable(el);
                const opts = { bubbles:true, cancelable:true, clientX:x, clientY:y, view:window, button:0, buttons:1, pointerId:1, pointerType:'touch' };
                try { el.dispatchEvent(new PointerEvent('pointermove', opts)); } catch (_) {}
                try {
                    const touch = new Touch({ identifier: 1, target: el, clientX: x, clientY: y });
                    el.dispatchEvent(new TouchEvent('touchmove', { bubbles:true, cancelable:true, touches:[touch], targetTouches:[touch], changedTouches:[touch] }));
                } catch (_) {}
                if (Math.abs(dx) > 0 || Math.abs(dy) > 0) {
                    try {
                        if (scrollTarget) {
                            scrollTarget.scrollBy({ left: dx, top: dy, behavior: 'instant' });
                        } else {
                            window.scrollBy({ left: dx, top: dy, behavior: 'instant' });
                        }
                    } catch (_) {
                        if (scrollTarget) {
                            scrollTarget.scrollLeft += dx;
                            scrollTarget.scrollTop += dy;
                        } else {
                            window.scrollBy(dx, dy);
                        }
                    }
                }
                return true;
                """,
                x,
                y,
                prev_x,
                prev_y,
                path or prev_path,
            )
            return

        if action == "mirror_drag_end":
            prev_x, prev_y, prev_path = self._mirror_drag_state.pop(state_key, (x, y, path))
            try:
                _cdp_touch("touchEnd", [])
            except Exception:
                pass
            driver.execute_script(
                """
                const x = Number(arguments[0]);
                const y = Number(arguments[1]);
                const selector = arguments[2] || '';
                const el = document.elementFromPoint(x, y) || (selector ? document.querySelector(selector) : null) || document.body;
                const opts = { bubbles:true, cancelable:true, clientX:x, clientY:y, view:window, button:0, buttons:0, pointerId:1, pointerType:'touch' };
                try { el.dispatchEvent(new PointerEvent('pointerup', opts)); } catch (_) {}
                try {
                    const touch = new Touch({ identifier: 1, target: el, clientX: x, clientY: y });
                    el.dispatchEvent(new TouchEvent('touchend', { bubbles:true, cancelable:true, touches:[], targetTouches:[], changedTouches:[touch] }));
                } catch (_) {}
                return true;
                """,
                x,
                y,
                path or prev_path,
            )
            if sys.platform == "win32":
                try:
                    start_screen_x, start_screen_y = self._viewport_point_to_screen(
                        driver,
                        max(0.0, min(1.0, prev_x / max(1, width))),
                        max(0.0, min(1.0, prev_y / max(1, height))),
                    )
                    end_screen_x, end_screen_y = self._viewport_point_to_screen(driver, x_ratio, y_ratio)
                    self._native_drag_swipe(start_screen_x, start_screen_y, end_screen_x, end_screen_y)
                except Exception:
                    pass
            return

    @staticmethod
    def _find_site_by_index(runtime: ProfileRuntime, index: int) -> SiteTabState | None:
        for site in runtime.site_tabs:
            if site.index == index:
                return site
        return None

    @staticmethod
    def _should_mirror_grid_action(action: str, site: SiteTabState) -> bool:
        return site.index == 0 and action in {"click", "type_text", "scroll_up", "scroll_down", "reload_tab"}

    def _apply_grid_action_to_targets(
        self,
        runtime: ProfileRuntime,
        targets: list[SiteTabState],
        action_name: str,
        runner: Callable[[SiteTabState], object],
        mirrored: bool,
    ) -> tuple[list[dict], list[dict]]:
        successes: list[dict] = []
        failures: list[dict] = []
        for current_site in targets:
            try:
                result = runner(current_site)
                successes.append({"site_index": current_site.index, "result": result})
            except Exception as exc:
                failures.append({"site_index": current_site.index, "error": str(exc)})
                self._log(
                    f"[{runtime.profile_name}] Grid {action_name} falhou na tela {current_site.index}: {exc}",
                    level="ERROR" if current_site.index == 0 else "WARN",
                )
        if mirrored:
            ok_sites = ",".join(str(item["site_index"]) for item in successes) or "-"
            fail_sites = ",".join(str(item["site_index"]) for item in failures) or "-"
            self._log(
                f"[{runtime.profile_name}] Grid {action_name} espelho => ok:[{ok_sites}] fail:[{fail_sites}]",
                level="INFO" if successes else "WARN",
            )
        return successes, failures

    def get_active_profile_ids(self) -> list[str]:
        with self._drivers_lock:
            return list(self._active_profiles.keys())

    def perform_grid_action(self, payload: GridActionRequest) -> dict:
        runtime = self._find_runtime_by_session(payload.session_id)
        if runtime is None:
            raise ValueError(f"Sessao {payload.session_id} nao encontrada.")

        if payload.action == "stop_profile":
            threading.Thread(
                target=self._close_profile_safe,
                args=(runtime.profile_id, runtime.profile_name),
                daemon=True,
                name=f"grid-stop-{runtime.profile_id}",
            ).start()
            self._log(f"[{runtime.profile_name}] Encerramento solicitado via grid.", level="WARN")
            return {"ok": True, "action": payload.action, "profile_id": runtime.profile_id}

        with self._drivers_lock:
            driver = self._drivers.get(runtime.profile_id)
        if driver is None:
            raise RuntimeError("Driver do perfil nao encontrado.")

        index = int(payload.index if payload.index is not None else -1)
        site = self._find_site_by_index(runtime, index)
        if site is None:
            raise ValueError(f"Tela {index} nao encontrada.")

        with runtime.lock:
            target_sites = list(runtime.site_tabs) if self._should_mirror_grid_action(payload.action, site) else [site]
            mirrored = len(target_sites) > 1

            def _run_click(current_site: SiteTabState) -> dict:
                self._switch_to_handle(driver, current_site.handle)
                self._apply_site_mobile_view(driver)
                self._install_hush_plus_cdp_hooks(driver)
                viewport = driver.execute_script("return {width: window.innerWidth || 430, height: window.innerHeight || 932};") or {}
                width = max(1, int(viewport.get("width") or 430))
                height = max(1, int(viewport.get("height") or 932))
                x_ratio = 0.5 if payload.x_ratio is None else float(payload.x_ratio)
                y_ratio = 0.5 if payload.y_ratio is None else float(payload.y_ratio)
                x = max(1, min(width - 1, int(width * x_ratio)))
                y = max(1, min(height - 1, int(height * y_ratio)))
                result = self._dispatch_js_click_on_driver(driver, x, y)
                time.sleep(0.15)
                self._capture_site_preview_unlocked(driver, runtime, current_site)
                return {"site_index": current_site.index, "x": x, "y": y, "result": result}

            def _run_type_text(current_site: SiteTabState) -> dict:
                text = str(payload.text or "")
                if not text:
                    raise RuntimeError("Texto vazio.")
                self._switch_to_handle(driver, current_site.handle)
                self._apply_site_mobile_view(driver)
                result = driver.execute_script(
                    """
                    const text = String(arguments[0] || '');
                    const el = document.activeElement;
                    if (!el) return { ok:false, reason:'no_active_element' };
                    try { el.focus({ preventScroll:true }); } catch (_) {}
                    if (el.isContentEditable) {
                        document.execCommand('insertText', false, text);
                        return { ok:true, tag:(el.tagName || '').toLowerCase(), mode:'contenteditable' };
                    }
                    if ('value' in el) {
                        const value = String(el.value || '');
                        const start = typeof el.selectionStart === 'number' ? el.selectionStart : value.length;
                        const end = typeof el.selectionEnd === 'number' ? el.selectionEnd : start;
                        el.value = value.slice(0, start) + text + value.slice(end);
                        if (typeof el.setSelectionRange === 'function') {
                            const pos = start + text.length;
                            el.setSelectionRange(pos, pos);
                        }
                        el.dispatchEvent(new Event('input', { bubbles:true }));
                        el.dispatchEvent(new Event('change', { bubbles:true }));
                        return { ok:true, tag:(el.tagName || '').toLowerCase(), mode:'value' };
                    }
                    return { ok:false, reason:'unsupported_element', tag:(el.tagName || '').toLowerCase() };
                    """,
                    text,
                )
                time.sleep(0.1)
                self._capture_site_preview_unlocked(driver, runtime, current_site)
                return {"site_index": current_site.index, "result": result}

            def _run_scroll(current_site: SiteTabState, delta: int) -> None:
                self._switch_to_handle(driver, current_site.handle)
                self._apply_site_mobile_view(driver)
                driver.execute_script(f"window.scrollBy({{ top: {delta}, behavior: 'instant' }});")
                self._capture_site_preview_unlocked(driver, runtime, current_site)

            def _run_reload(current_site: SiteTabState) -> None:
                self._switch_to_handle(driver, current_site.handle)
                self._apply_site_mobile_view(driver)
                driver.refresh()
                current_site.last_reload_at = time.time()
                time.sleep(0.3)
                self._capture_site_preview_unlocked(driver, runtime, current_site)

            self._ensure_driver_alive(driver)
            self._switch_to_handle(driver, site.handle)
            self._apply_site_mobile_view(driver)

            if payload.action == "click":
                successes, failures = self._apply_grid_action_to_targets(runtime, target_sites, "click", _run_click, mirrored)
                if not successes:
                    raise RuntimeError("Nenhuma tela recebeu o clique.")
                leader = successes[0]["result"]
                self._log(
                    f"[{runtime.profile_name}] Grid click tela {site.index} @ {leader['x']},{leader['y']}"
                    + (f" | replicado para {len(target_sites)-1} slave(s)" if mirrored else ""),
                    level="INFO",
                )
                return {
                    "ok": True,
                    "action": payload.action,
                    "result": leader["result"],
                    "targets": len(target_sites),
                    "mirrored": mirrored,
                    "successes": successes,
                    "failures": failures,
                }

            if payload.action == "type_text":
                successes, failures = self._apply_grid_action_to_targets(runtime, target_sites, "type_text", _run_type_text, mirrored)
                if not successes:
                    raise RuntimeError("Nenhuma tela recebeu a digitacao.")
                self._log(
                    f"[{runtime.profile_name}] Grid text tela {site.index}"
                    + (f" | replicado para {len(target_sites)-1} slave(s)" if mirrored else ""),
                    level="INFO",
                )
                return {
                    "ok": True,
                    "action": payload.action,
                    "result": successes[0]["result"],
                    "targets": len(target_sites),
                    "mirrored": mirrored,
                    "successes": successes,
                    "failures": failures,
                }

            if payload.action == "scroll_up":
                successes, failures = self._apply_grid_action_to_targets(
                    runtime,
                    target_sites,
                    "scroll_up",
                    lambda current_site: _run_scroll(current_site, -420),
                    mirrored,
                )
                if not successes:
                    raise RuntimeError("Nenhuma tela recebeu o scroll para cima.")
                if mirrored:
                    self._log(f"[{runtime.profile_name}] Grid scroll- tela {site.index} | replicado para {len(target_sites)-1} slave(s)", level="INFO")
                return {"ok": True, "action": payload.action, "targets": len(target_sites), "mirrored": mirrored, "successes": successes, "failures": failures}

            if payload.action == "scroll_down":
                successes, failures = self._apply_grid_action_to_targets(
                    runtime,
                    target_sites,
                    "scroll_down",
                    lambda current_site: _run_scroll(current_site, 420),
                    mirrored,
                )
                if not successes:
                    raise RuntimeError("Nenhuma tela recebeu o scroll para baixo.")
                if mirrored:
                    self._log(f"[{runtime.profile_name}] Grid scroll+ tela {site.index} | replicado para {len(target_sites)-1} slave(s)", level="INFO")
                return {"ok": True, "action": payload.action, "targets": len(target_sites), "mirrored": mirrored, "successes": successes, "failures": failures}

            if payload.action == "reload_tab":
                successes, failures = self._apply_grid_action_to_targets(runtime, target_sites, "reload_tab", _run_reload, mirrored)
                if not successes:
                    raise RuntimeError("Nenhuma tela recebeu o reload.")
                if mirrored:
                    self._log(f"[{runtime.profile_name}] Grid reload tela {site.index} | replicado para {len(target_sites)-1} slave(s)", level="INFO")
                return {"ok": True, "action": payload.action, "targets": len(target_sites), "mirrored": mirrored, "successes": successes, "failures": failures}

            if payload.action == "close_tab":
                try:
                    driver.close()
                finally:
                    runtime.site_tabs = [current for current in runtime.site_tabs if current.index != site.index]
                    self._grid_update_card(runtime, {
                        "index": site.index,
                        "state": "closed",
                        "counter": site.capture_count,
                        "stamp": time.strftime("%H:%M:%S"),
                        "current_url": site.current_url or site.url,
                        "note": "Tela encerrada pelo grid.",
                    })
                    fallback = runtime.background_handle or runtime.dashboard_handle
                    if fallback:
                        try:
                            self._switch_to_handle(driver, fallback)
                        except Exception:
                            pass
                self._log(f"[{runtime.profile_name}] Tela {site.index} encerrada via grid.", level="WARN")
                return {"ok": True, "action": payload.action}

        raise RuntimeError(f"Acao desconhecida: {payload.action}")

    def _resolve_profile_workspace(self, settings: VolumeSettings, job_index: int, total_profiles: int) -> DesktopBounds:
        if str(settings.display_mode).lower() == "grade_real" and self._has_calibration_for_settings(settings):
            placements, monitor = calculate_multi_monitor_grid(
                total_profiles,
                self._resolve_target_monitors(settings),
                self._get_effective_calibrated_cells(settings),
                settings.window_gap,
            )
            if placements:
                slot = placements[max(0, min(job_index - 1, len(placements) - 1))]
                return DesktopBounds(x=int(slot.x), y=int(slot.y), width=int(slot.width), height=int(slot.height))
            return monitor
        desktop = detect_desktop_bounds()
        base_x = int(settings.window_origin_x if settings.window_area_width or settings.window_origin_x else desktop.x)
        base_y = int(settings.window_origin_y if settings.window_area_height or settings.window_origin_y else desktop.y)
        base_width = int(settings.window_area_width or desktop.width)
        base_height = int(settings.window_area_height or desktop.height)
        gap = max(0, int(settings.window_gap))
        slots = max(1, total_profiles)
        slot_cols = max(1, math.ceil(math.sqrt(slots)))
        slot_rows = max(1, math.ceil(slots / slot_cols))
        slot_index = max(0, min(job_index - 1, slots - 1))
        slot_col = slot_index % slot_cols
        slot_row = slot_index // slot_cols
        slot_width = max(80, int((base_width - gap * (slot_cols - 1)) / slot_cols))
        slot_height = max(80, int((base_height - gap * (slot_rows - 1)) / slot_rows))
        return DesktopBounds(
            x=base_x + slot_col * (slot_width + gap),
            y=base_y + slot_row * (slot_height + gap),
            width=slot_width,
            height=slot_height,
        )

    def _pick_layout_columns(self, item_count: int, workspace: DesktopBounds, settings: VolumeSettings) -> int:
        if item_count <= 1:
            return 1
        if str(settings.display_mode).lower() == "grade_real" and int(settings.grid_columns or 0) > 0:
            return min(item_count, max(1, int(settings.grid_columns)))
        if settings.layout_columns > 0:
            return min(item_count, settings.layout_columns)
        best_cols = 1
        best_score = -1.0
        gap = max(0, int(settings.window_gap))
        for cols in range(1, item_count + 1):
            rows = math.ceil(item_count / cols)
            cell_width = (workspace.width - gap * (cols - 1)) / cols
            cell_height = (workspace.height - gap * (rows - 1)) / rows
            if cell_width <= 80 or cell_height <= 80:
                continue
            score = min(cell_width / MOBILE_VIEWPORT_WIDTH, cell_height / MOBILE_VIEWPORT_HEIGHT)
            if score > best_score:
                best_score = score
                best_cols = cols
        return max(1, best_cols)

    def _build_window_layout(self, item_count: int, settings: VolumeSettings, job_index: int, total_profiles: int) -> list[WindowPlacement]:
        workspace = self._resolve_profile_workspace(settings, job_index, total_profiles)
        if str(settings.display_mode).lower() == "grade_real" and self._has_calibration_for_settings(settings):
            base = WindowPlacement(x=int(workspace.x), y=int(workspace.y), width=int(workspace.width), height=int(workspace.height))
            return self._build_window_layout_from_base(item_count, base, workspace, settings)
        cols = self._pick_layout_columns(item_count, workspace, settings)
        rows = max(1, math.ceil(item_count / cols))
        gap = max(0, int(settings.window_gap))
        cell_width = max(80, int((workspace.width - gap * (cols - 1)) / cols))
        cell_height = max(80, int((workspace.height - gap * (rows - 1)) / rows))
        placements: list[WindowPlacement] = []
        for index in range(item_count):
            row = index // cols
            col = index % cols
            scale = max(0.15, min(cell_width / MOBILE_VIEWPORT_WIDTH, cell_height / MOBILE_VIEWPORT_HEIGHT))
            window_width = max(80, int(MOBILE_VIEWPORT_WIDTH * scale))
            window_height = max(80, int(MOBILE_VIEWPORT_HEIGHT * scale))
            offset_x = max(0, int((cell_width - window_width) / 2))
            offset_y = max(0, int((cell_height - window_height) / 2))
            placements.append(
                WindowPlacement(
                    x=workspace.x + col * (cell_width + gap) + offset_x,
                    y=workspace.y + row * (cell_height + gap) + offset_y,
                    width=window_width,
                    height=window_height,
                )
            )
        return placements

    def _build_window_layout_from_base(
        self,
        item_count: int,
        base: WindowPlacement,
        workspace: DesktopBounds,
        settings: VolumeSettings,
    ) -> list[WindowPlacement]:
        if item_count <= 0:
            return []
        gap = max(0, int(settings.window_gap))
        base_width = max(80, int(base.width))
        base_height = max(80, int(base.height))
        base_x = max(workspace.x, int(base.x))
        base_y = max(workspace.y, int(base.y))
        placements: list[WindowPlacement] = [WindowPlacement(x=base_x, y=base_y, width=base_width, height=base_height)]
        if item_count == 1:
            return placements

        current_x = base_x + base_width + gap
        current_y = base_y
        max_x = workspace.x + max(0, workspace.width - base_width)
        max_y = workspace.y + max(0, workspace.height - base_height)

        for _ in range(1, item_count):
            if current_x > max_x:
                current_x = workspace.x
                current_y += base_height + gap
            if current_y > max_y:
                current_y = workspace.y
            placements.append(
                WindowPlacement(
                    x=int(current_x),
                    y=int(current_y),
                    width=base_width,
                    height=base_height,
                )
            )
            current_x += base_width + gap
        return placements

    def _apply_site_window_layout(self, driver: webdriver.Chrome, placement: WindowPlacement) -> None:
        try:
            driver.set_window_rect(x=int(placement.x), y=int(placement.y), width=int(placement.width), height=int(placement.height))
        except Exception:
            pass
        mobile_mode = str(getattr(self.workspace_data, "mobile_mode", "") or "").lower()
        if "computador" in mobile_mode:
            self._apply_dashboard_desktop_view(driver)
        else:
            self._apply_site_mobile_view(driver)

    def _focus_game_iframe_after_load(self, driver: webdriver.Chrome, wait_seconds: float = 6.0) -> bool:
        if wait_seconds > 0:
            time.sleep(wait_seconds)
        try:
            driver.switch_to.default_content()
        except Exception:
            pass

        try:
            top_rect = self._find_game_rect_in_current_context(driver)
            if str(top_rect.get("tag") or "").lower() != "iframe" and float(top_rect.get("score") or 0) >= 900:
                print("Jogo encontrado no contexto principal.")
                return True
        except Exception:
            pass

        try:
            frames = driver.find_elements(By.TAG_NAME, "iframe")
        except Exception as e:
            print(f"Aviso: Nao encontrou iframe na pagina principal, rodando no contexto atual. {e}")
            return False

        for index in range(len(frames)):
            try:
                driver.switch_to.default_content()
                frames = driver.find_elements(By.TAG_NAME, "iframe")
                if index >= len(frames):
                    break
                driver.switch_to.frame(frames[index])
                rect = self._find_game_rect_in_current_context(driver)
                has_game = str(rect.get("tag") or "").lower() != "viewport" or float(rect.get("score") or 0) >= 900
                if has_game:
                    print(f"Foco alterado para o iframe do jogo com sucesso! iframe={index}")
                    break
            except Exception as e:
                print(f"Aviso: iframe {index} ignorado durante varredura. {e}")
                continue
        else:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
            print("Aviso: Nao encontrou iframe de jogo, rodando no contexto atual.")
            return False

        return True

    def _open_site_window(
        self,
        driver: webdriver.Chrome,
        runtime: ProfileRuntime,
        index: int,
        url: str,
        placement: WindowPlacement,
        reuse_current: bool = False,
        role_label: str | None = None,
        role_class: str | None = None,
    ) -> SiteTabState:
        if reuse_current:
            handle = runtime.dashboard_handle or driver.current_window_handle
            self._switch_to_handle(driver, handle)
        else:
            host_handle = runtime.dashboard_handle or driver.current_window_handle
            self._switch_to_handle(driver, host_handle)
            driver.switch_to.new_window("window")
            handle = driver.current_window_handle
        self._apply_site_window_layout(driver, placement)
        self._install_hush_plus_cdp_hooks(driver)
        try:
            driver.get(url)
            self._focus_game_iframe_after_load(driver)
        except Exception:
            raise
        try:
            self._install_hush_plus_cdp_hooks(driver)
        except Exception as exc:
            self._log(f"Falha ao reinjetar aceleracao no motor grafico apos abrir aba: {exc}", level="WARN")
        state = SiteTabState(
            index=index,
            url=url,
            handle=handle,
            placement=placement,
            role_label=role_label or ("MASTER" if index == 0 else f"SLAVE {index}"),
            role_class=role_class or ("master" if index == 0 else "slave"),
            last_reload_at=time.time(),
            current_url=url,
        )
        runtime.site_tabs.append(state)
        return state

    def _capture_frame_base64(self, driver: webdriver.Chrome) -> str:
        try:
            payload = driver.execute_cdp_cmd(
                "Page.captureScreenshot",
                {
                    "format": "jpeg",
                    "quality": GRID_CAPTURE_JPEG_QUALITY,
                    "fromSurface": True,
                    "captureBeyondViewport": False,
                },
            )
            data = str(payload.get("data") or "")
            if data:
                return data
        except Exception:
            pass
        return driver.get_screenshot_as_base64()

    def _capture_site_preview_unlocked(self, driver: webdriver.Chrome, runtime: ProfileRuntime, site: SiteTabState) -> None:
        self._switch_to_handle(driver, site.handle)
        title = ""
        current_url = site.url
        try:
            title = driver.title or ""
            current_url = driver.current_url or site.url
        except Exception:
            pass
        previous_capture_at = float(site.last_capture_at or 0.0)
        image_base64 = self._capture_frame_base64(driver)
        site.last_capture_at = time.time()
        delta = site.last_capture_at - previous_capture_at if previous_capture_at else 0.0
        fps = round(1.0 / delta, 1) if delta > 0 else 0.0
        site.capture_count += 1
        site.retries = 0
        site.last_error = ""
        site.title = title
        site.current_url = current_url
        self._grid_update_card(runtime, {
            "index": site.index,
            "state": "ok",
            "counter": site.capture_count,
            "fps": fps,
            "stamp": time.strftime("%H:%M:%S"),
            "image_base64": image_base64,
            "current_url": current_url,
            "note": f"<b>{title or 'Preview pronta'}</b>",
        })

    def _capture_grid_previews_once(self, driver: webdriver.Chrome, runtime: ProfileRuntime) -> None:
        for site in list(runtime.site_tabs):
            with runtime.lock:
                self._capture_site_preview_unlocked(driver, runtime, site)
            time.sleep(0.05)

    def build_mirror_extension(
        self,
        relay_config: RelayConfig,
        session_id: str | None = None,
        suffix: str | None = None,
        role_mode: str = "auto",
    ) -> str:
        extension_name = "ltdf_mirror" if not suffix else f"ltdf_mirror_{self._slugify(suffix)}"
        extension_dir = self.temp_root / extension_name
        if extension_dir.exists():
            shutil.rmtree(extension_dir, ignore_errors=True)
        extension_dir.mkdir(parents=True, exist_ok=True)
        relay_hosts = detect_local_ipv4_candidates(default=relay_config.host)
        extra_hosts = [relay_config.host, "127.0.0.1", "localhost"]
        for host in extra_hosts:
            if host and host not in relay_hosts:
                relay_hosts.append(host)
        ws_urls = [f"ws://{host}:{relay_config.port}/ws" for host in relay_hosts if host]
        host_permissions = ["<all_urls>"]
        for host in relay_hosts:
            host_permissions.append(f"http://{host}:{relay_config.port}/*")
            host_permissions.append(f"ws://{host}:{relay_config.port}/*")
        manifest = {
            "manifest_version": 3,
            "name": "LTDF Mirror",
            "version": "5.0",
            "description": "Mirror relay - tela unica em grade",
            "permissions": ["tabs", "storage", "declarativeNetRequest"],
            "host_permissions": host_permissions,
            "background": {"service_worker": "background.js"},
            "content_scripts": [
                {
                    "matches": ["<all_urls>"],
                    "js": ["speed.js"],
                    "run_at": "document_start",
                    "all_frames": True,
                    "match_about_blank": True,
                    "world": "MAIN",
                },
                {
                    "matches": ["<all_urls>"],
                    "js": ["speed_injector.js"],
                    "run_at": "document_start",
                    "all_frames": True,
                    "match_about_blank": True,
                },
                {
                    "matches": ["<all_urls>"],
                    "js": ["speed_late.js"],
                    "run_at": "document_end",
                    "all_frames": True,
                    "match_about_blank": True,
                    "world": "MAIN",
                },
                {
                    "matches": ["<all_urls>"],
                    "js": ["speed_bridge.js"],
                    "run_at": "document_start",
                    "all_frames": True,
                    "match_about_blank": True,
                },
                {
                    "matches": ["<all_urls>"],
                    "js": ["panel.js"],
                    "run_at": "document_end",
                    "all_frames": False,
                }
            ],
        }
        self._write_json(extension_dir / "manifest.json", manifest)
        background_js = (
            BACKGROUND_JS_TEMPLATE
            .replace("__WS_URLS__", json.dumps(ws_urls))
            .replace("__SESSION_ID__", session_id or "__AUTO__")
            .replace("__ROLE_MODE__", role_mode)
            .replace("__DEFAULT_SPEED_CONFIG__", json.dumps(build_html5_speed_config(self.workspace_data), ensure_ascii=False))
        )
        panel_js = (
            PANEL_JS
            .replace("__DEFAULT_SPEED_CONFIG__", json.dumps(build_html5_speed_config(self.workspace_data), ensure_ascii=False))
            .replace("__SPEED_HACK_PAGE_SCRIPT__", json.dumps(SPEED_HACK_PAGE_SCRIPT, ensure_ascii=False))
        )
        speed_js = (
            "window.__ltdfSpeedInitialConfig = "
            + json.dumps(build_html5_speed_config(self.workspace_data), ensure_ascii=False)
            + ";\n"
            + SPEED_HACK_PAGE_SCRIPT
        )
        speed_injector_js = (
            "const LTDF_SPEED_BOOTSTRAP = "
            + json.dumps(
                "window.__ltdfSpeedInitialConfig = "
                + json.dumps(build_html5_speed_config(self.workspace_data), ensure_ascii=False)
                + ";\n"
                + SPEED_HACK_PAGE_SCRIPT
                + "\n//# sourceURL=ltdf_speed_mainworld_fallback.js",
                ensure_ascii=False,
            )
            + ";\n"
            + """
(function () {
  if (window.__ltdfSpeedMainWorldTagRequested) return;
  window.__ltdfSpeedMainWorldTagRequested = true;
  function injectMainWorld() {
    try {
      const target = document.head || document.documentElement || document.body;
      if (!target) return false;
      const script = document.createElement('script');
      script.type = 'text/javascript';
      script.dataset.ltdfSpeedFallback = 'main-world';
      script.textContent = LTDF_SPEED_BOOTSTRAP;
      target.appendChild(script);
      script.remove();
      return true;
    } catch (_) {
      return false;
    }
  }
  if (injectMainWorld()) return;
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', injectMainWorld, { once: true });
  }
  const startedAt = Date.now();
  const timer = setInterval(() => {
    if (injectMainWorld() || Date.now() - startedAt > 3000) clearInterval(timer);
  }, 50);
})();
//# sourceURL=ltdf_speed_injector.js
            """.strip()
        )
        speed_late_js = (
            "window.__ltdfSpeedInitialConfig = "
            + json.dumps(build_html5_speed_config(self.workspace_data), ensure_ascii=False)
            + ";\n"
            + """
(function () {
  const cfg = window._ltdfSpeedConfig || window.__ltdfSpeedInitialConfig || { enabled:false, speed:1.0 };
  if (typeof window.__ltdfForceSpeedReapply === 'function') {
    window.__ltdfForceSpeedReapply('document_end_content_script');
    return;
  }
  if (typeof window.__ltdfApplySpeedConfig === 'function') {
    window.__ltdfApplySpeedConfig(cfg, 'document_end_content_script');
    return;
  }
  window._ltdfSpeedConfig = cfg;
  window._ltdfSpeed = cfg && cfg.enabled ? Number(cfg.speed || 1) || 1 : 1.0;
  window._ltdfTargetSpeed = window._ltdfSpeed;
})();
//# sourceURL=ltdf_speed_late.js
            """.strip()
        )
        speed_bridge_js = SPEED_BRIDGE_JS.replace(
            "__DEFAULT_SPEED_CONFIG__",
            json.dumps(build_html5_speed_config(self.workspace_data), ensure_ascii=False),
        )
        (extension_dir / "background.js").write_text(background_js, encoding="utf-8")
        (extension_dir / "panel.js").write_text(panel_js, encoding="utf-8")
        (extension_dir / "speed.js").write_text(speed_js, encoding="utf-8")
        (extension_dir / "speed_injector.js").write_text(speed_injector_js, encoding="utf-8")
        (extension_dir / "speed_late.js").write_text(speed_late_js, encoding="utf-8")
        (extension_dir / "speed_bridge.js").write_text(speed_bridge_js, encoding="utf-8")
        logger.info(
            "mirror_extension_built",
            path=str(extension_dir),
            session_id=session_id or "auto",
            role_mode=role_mode,
            relay_hosts=relay_hosts,
        )
        self._emit(MirrorGeneratedEvent(path=str(extension_dir)))
        return str(extension_dir)

    def build_layout_extension(self, script: ScriptRecord, suffix: str | None = None) -> str:
        safe_name = self._slugify(script.name)
        extension_name = f"ltdf_layout_{safe_name}" if not suffix else f"ltdf_layout_{safe_name}_{self._slugify(suffix)}"
        extension_dir = self.temp_root / extension_name
        if extension_dir.exists():
            shutil.rmtree(extension_dir, ignore_errors=True)
        extension_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "manifest_version": 3,
            "name": f"LTDF Layout - {script.name}",
            "version": "1.0",
            "description": f"Layout script: {script.name}",
            "permissions": [],
            "host_permissions": ["<all_urls>"],
            "content_scripts": [
                {
                    "matches": ["<all_urls>"],
                    "js": ["layout.js"],
                    "run_at": "document_end",
                    "all_frames": False,
                }
            ],
        }
        self._write_json(extension_dir / "manifest.json", manifest)
        workspace_bootstrap = "window.__ltdfWorkspace = " + json.dumps(model_dump_compat(self.workspace_data), ensure_ascii=False) + ";\n"
        (extension_dir / "layout.js").write_text(workspace_bootstrap + script.code, encoding="utf-8")
        logger.info("layout_extension_built", path=str(extension_dir), script=script.name)
        return str(extension_dir)

    def generate_mirror_extension(self, relay_config: RelayConfig) -> str:
        return self.build_mirror_extension(relay_config)

    def build_execution_plan(self, profiles: list[AdsPowerProfile], urls: list[str], slave_urls: list[str], settings: VolumeSettings) -> ExecutionPlan:
        profiles = list(profiles)
        clean_urls = [u.strip() for u in urls if u and u.strip()]
        clean_slave_urls = [u.strip() for u in slave_urls if u and u.strip()]
        plan = ExecutionPlan(
            assignment_mode=settings.url_assignment_mode,
            total_input_urls=len(clean_urls) + len(clean_slave_urls),
            total_profiles=len(profiles),
            max_concurrent_profiles=max(1, min(settings.max_concurrent_profiles, max(1, len(profiles) or 1))),
        )
        if not profiles or not clean_urls:
            return plan

        def chunked(items: list[str], size: int) -> list[list[str]]:
            size = max(1, size)
            return [items[i:i + size] for i in range(0, len(items), size)]

        max_tabs = max(1, settings.max_tabs_per_profile)
        if settings.url_assignment_mode == "replicate" or len(clean_urls) == 1:
            assigned = clean_urls[:max_tabs]
            skipped = clean_urls[max_tabs:]
            for profile in profiles:
                plan.entries.append(
                    ExecutionPlanEntry(
                        profile=profile,
                        urls=list(assigned),
                        slave_urls=list(clean_slave_urls),
                        batches=chunked(list(assigned), settings.url_batch_size),
                        skipped_urls=len(skipped),
                    )
                )
            plan.skipped_urls.extend(skipped)
            if len(clean_urls) == 1 and settings.url_assignment_mode != "replicate":
                plan.assignment_mode = "replicate"
                plan.warnings.append("Uma unica URL detectada: replicando automaticamente para todos os perfis selecionados.")
        else:
            per_profile_lists = [[] for _ in profiles]
            skipped: list[str] = []
            capacity = len(profiles) * max_tabs
            for idx, url in enumerate(clean_urls):
                if idx >= capacity:
                    skipped.append(url)
                    continue
                slot = idx % len(profiles)
                if len(per_profile_lists[slot]) >= max_tabs:
                    moved = False
                    for alt in range(len(profiles)):
                        if len(per_profile_lists[alt]) < max_tabs:
                            per_profile_lists[alt].append(url)
                            moved = True
                            break
                    if not moved:
                        skipped.append(url)
                else:
                    per_profile_lists[slot].append(url)
            for profile, assigned in zip(profiles, per_profile_lists):
                plan.entries.append(
                    ExecutionPlanEntry(
                        profile=profile,
                        urls=assigned,
                        slave_urls=list(clean_slave_urls),
                        batches=chunked(assigned, settings.url_batch_size),
                        skipped_urls=0,
                    )
                )
            plan.skipped_urls.extend(skipped)

        plan.entries = [entry for entry in plan.entries if entry.urls]

        plan.total_assigned_urls = sum(len(e.urls) + len(e.slave_urls) for e in plan.entries)
        plan.total_batches = sum(len(e.batches) for e in plan.entries)
        plan.execution_waves = math.ceil(max(1, len(plan.entries)) / max(1, plan.max_concurrent_profiles))
        plan.estimated_peak_tabs = min(plan.total_assigned_urls, len(plan.entries) * (max_tabs + len(clean_slave_urls)))
        plan.estimated_runtime_seconds = max(0.0, plan.total_assigned_urls * max(0.2, settings.tab_open_delay_seconds) + plan.total_batches * settings.batch_pause_seconds)
        if plan.skipped_urls:
            plan.warnings.append("Distribuicao atingiu o limite maximo por perfil; URLs excedentes foram ignoradas.")
        if clean_slave_urls:
            plan.warnings.append(f"Links da aba Filhas: {len(clean_slave_urls)} por perfil, abertos sem forcar /registered e sempre como SLAVE.")
        return plan

    def _normalize_settings(self, settings: VolumeSettings) -> VolumeSettings:
        data = model_dump_compat(settings)
        display_mode = str(data.get("display_mode", "windows")).lower()
        if display_mode not in {"windows", "grid", "grade_real"}:
            display_mode = "windows"
        data["display_mode"] = display_mode
        data["max_concurrent_profiles"] = max(1, int(data.get("max_concurrent_profiles", 1)))
        data["url_batch_size"] = max(1, int(data.get("url_batch_size", 8)))
        data["max_tabs_per_profile"] = max(1, int(data.get("max_tabs_per_profile", 24)))
        data["layout_columns"] = max(0, int(data.get("layout_columns", 0)))
        data["window_gap"] = max(0, int(data.get("window_gap", 6)))
        data["window_area_width"] = max(0, int(data.get("window_area_width", 0)))
        data["window_area_height"] = max(0, int(data.get("window_area_height", 0)))
        data["base_window_pause_seconds"] = max(0.0, float(data.get("base_window_pause_seconds", 5.0)))
        data["grid_columns"] = max(0, int(data.get("grid_columns", 0)))
        data["iframe_retry_limit"] = max(0, int(data.get("iframe_retry_limit", 3)))
        return VolumeSettings(**data)

    def run_profiles(self, profiles: list[AdsPowerProfile], urls: list[str], slave_urls: list[str], relay_config: RelayConfig, active_script: ScriptRecord, settings: VolumeSettings) -> None:
        settings = self._normalize_settings(settings)
        plan = self.build_execution_plan(profiles, urls, slave_urls, settings)
        self._emit(NetworkSnapshotEvent(pc_adapters=detect_network_adapters()))
        self._log(
            f"Plano: visual={settings.display_mode} modo={plan.assignment_mode} perfis={len(plan.entries)} urls={plan.total_input_urls} grid_items={plan.total_assigned_urls} "
            f"concorrencia={plan.max_concurrent_profiles} lote={settings.url_batch_size} limite={settings.max_tabs_per_profile} "
            f"delay={settings.tab_open_delay_seconds:.2f}s ondas={plan.execution_waves} pico={plan.estimated_peak_tabs}"
        )
        for warning in plan.warnings:
            self._log(warning, level="WARN")
        if not plan.entries:
            self._emit(BotStatusEvent(state="error", message="Nada para executar.", total_profiles=0))
            self._emit(BotStatusEvent(state="idle", message="", total_profiles=0))
            return

        total_profiles = len(plan.entries)
        self._emit(BotStatusEvent(state="running", message=f"Executando {total_profiles} perfil(is)...", running_profiles=0, total_profiles=total_profiles))
        errors: list[str] = []
        total_cards = 0
        operation_session_id = uuid.uuid4().hex[:10]
        with ThreadPoolExecutor(max_workers=plan.max_concurrent_profiles) as executor:
            futures = {
                executor.submit(
                    self._run_profile_job,
                    entry,
                    relay_config,
                    active_script,
                    settings,
                    idx,
                    total_profiles,
                    operation_session_id,
                ): entry.profile
                for idx, entry in enumerate(plan.entries, start=1)
            }
            completed = 0
            for future in as_completed(futures):
                profile = futures[future]
                try:
                    total_cards += future.result()
                except Exception as exc:
                    errors.append(f"{profile.name}: {exc}")
                    self._log(f"{profile.name}: {exc}", level="ERROR")
                completed += 1
                self._emit(BotStatusEvent(state="running", message=f"Perfis concluidos: {completed}/{total_profiles}", running_profiles=completed, total_profiles=total_profiles))
        if errors:
            self._emit(BotStatusEvent(state="error", message=f"Execucao concluida com falhas em {len(errors)} perfil(is).", opened_tabs=total_cards, running_profiles=total_profiles, total_profiles=total_profiles))
        else:
            self._emit(BotStatusEvent(state="completed", message=f"{total_cards} tela(s) ativas em grade em {total_profiles} perfil(is).", opened_tabs=total_cards, running_profiles=total_profiles, total_profiles=total_profiles))
        self._emit(BotStatusEvent(state="idle", message="", opened_tabs=total_cards, running_profiles=total_profiles, total_profiles=total_profiles))

    def _run_profile_job(
        self,
        entry: ExecutionPlanEntry,
        relay_config: RelayConfig,
        active_script: ScriptRecord,
        settings: VolumeSettings,
        job_index: int,
        total_profiles: int,
        operation_session_id: str,
    ) -> int:
        profile = entry.profile
        session_id = operation_session_id
        runtime = ProfileRuntime(profile_id=profile.user_id, profile_name=profile.name, session_id=session_id)
        self._runtimes[profile.user_id] = runtime
        stop_flag = self._create_stop_flag(profile.user_id)
        card_count = 0
        browser_started = False
        job_completed = False

        if job_index > 1 and settings.profile_launch_pause_seconds > 0:
            time.sleep(settings.profile_launch_pause_seconds * (job_index - 1))

        self._log(f"[{job_index}/{total_profiles}] Perfil {profile.name}: iniciando com {len(entry.urls)} URL(s).", level="INFO")
        self._emit(BotStatusEvent(state="running", message=f"[{profile.name}] Iniciando perfil...", profile_id=profile.user_id, profile_name=profile.name, session_id=session_id, total_profiles=total_profiles))

        try:
            layout_path = self.build_layout_extension(active_script, suffix=f"{profile.user_id}_{session_id}")
            role_mode = "auto" if job_index == 1 else "slave_only"
            mirror_path = self.build_mirror_extension(
                relay_config,
                session_id=session_id,
                suffix=f"{profile.user_id}_{job_index}_{session_id}",
                role_mode=role_mode,
            )
            extension_paths = [mirror_path, layout_path]
            relay_hosts = detect_local_ipv4_candidates(default=relay_config.host)
            proxy_bypass_entries = ["<-loopback>"]
            for host in relay_hosts:
                if host and host not in proxy_bypass_entries:
                    proxy_bypass_entries.append(host)
            proxy_bypass = ";".join(proxy_bypass_entries)
            self._log(f"[{profile.name}] Proxy bypass relay: {proxy_bypass}", level="INFO")
            start_payload = self.adspower_client.start_browser(
                profile.user_id,
                launch_args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--no-sandbox",
                    "--disable-popup-blocking",
                    "--disable-dev-shm-usage",
                    "--disable-background-networking",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-client-side-phishing-detection",
                    "--disable-connection-purpose-override",
                    "--disable-default-apps",
                    "--disable-http2",
                    "--disable-notifications",
                    "--disable-renderer-backgrounding",
                    "--disable-save-password-bubble",
                    "--disable-session-crashed-bubble",
                    "--disable-single-click-autofill",
                    "--disable-translate",
                    "--hide-crash-restore-bubble",
                    "--lang=pt-BR",
                    "--mute-audio",
                    "--no-default-browser-check",
                    "--no-first-run",
                    "--enable-tcp-fast-open",
                    f"--load-extension={','.join(extension_paths)}",
                    f"--proxy-bypass-list={proxy_bypass}",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-features=IsolateOrigins,site-per-process,TranslateUI,OptimizationHints,MediaRouter,AutofillServerCommunication,PasswordManagerEnabled",
                    "--disable-site-isolation-trials",
                    "--allow-running-insecure-content",
                    f"--unsafely-treat-insecure-origin-as-secure={relay_config.http_origin}",
                ],
            )
            if start_payload.get("code") != 0:
                raise RuntimeError(f"Erro ao iniciar browser: {start_payload.get('msg', '')}")
            browser_started = True

            wait_seconds = max(1.0, settings.browser_ready_seconds)
            self._log(f"[{profile.name}] Aguardando browser estabilizar ({wait_seconds:.1f}s)...")
            time.sleep(wait_seconds)

            browser_info = self._poll_active_browser(profile.user_id)
            if not browser_info.debug_port or not browser_info.webdriver:
                raise RuntimeError("Erro: debug port nao encontrado.")
            driver = self._connect_driver(profile.user_id, profile.name, browser_info)
            placements: list[WindowPlacement] = []
            if settings.display_mode == "grid":
                self._reset_roles(relay_config, session_id)
                time.sleep(0.2)
                init_grid_session(session_id, profile.name, settings.grid_columns, settings.grid_auto_reload_seconds, settings.iframe_retry_limit)
                self._launch_dashboard_window(relay_config=relay_config, profile=profile, session_id=session_id, settings=settings)
                self._position_worker_window(driver, runtime)
                try:
                    runtime.background_handle = self._create_background_window(driver, runtime.dashboard_handle)
                except Exception as exc:
                    self._log(f"[{profile.name}] Falha ao preparar workspace oculto: {exc}", level="WARN")
            else:
                self._reset_roles(relay_config, session_id)
                time.sleep(0.2)
                runtime.dashboard_handle = driver.current_window_handle
                placements = self._build_window_layout(len(entry.urls) + len(entry.slave_urls), settings, job_index, total_profiles)
                if placements:
                    workspace = self._resolve_profile_workspace(settings, job_index, total_profiles)
                    self._log(
                        f"[{profile.name}] Workspace janelas x={workspace.x} y={workspace.y} w={workspace.width} h={workspace.height} itens={len(placements)}",
                        level="INFO",
                    )

            for batch_index, batch in enumerate(entry.batches, start=1):
                if stop_flag.is_set():
                    self._log(f"[{profile.name}] Execucao interrompida antes do lote {batch_index}.", level="WARN")
                    break
                self._log(f"[{profile.name}] Lote {batch_index}/{len(entry.batches)} com {len(batch)} URL(s).", level="INFO")
                for url_index, raw_url in enumerate(batch, start=1):
                    if stop_flag.is_set():
                        self._log(f"[{profile.name}] Execucao interrompida antes da URL {url_index}/{len(batch)}.", level="WARN")
                        break
                    use_master_passthrough = bool(
                        settings.master_passthrough_first_url
                        and card_count == 0
                        and batch_index == 1
                        and url_index == 1
                    )
                    target_url = self._build_passthrough_url(raw_url) if use_master_passthrough else self._build_registered_url(raw_url)
                    label = "MASTER" if card_count == 0 else f"SLAVE {card_count}"
                    self._log(f"[{profile.name}] [{url_index}/{len(batch)}] {label} -> {target_url}", level="INFO")
                    try:
                        self._ensure_driver_alive(driver)
                        if settings.display_mode == "grid":
                            self._grid_add_site(runtime, profile, card_count, target_url, settings, role_label=label, role_class=("master" if card_count == 0 else "slave"))
                            self._open_site_tab(driver, runtime, card_count, target_url, role_label=label, role_class=("master" if card_count == 0 else "slave"))
                        else:
                            placement = placements[card_count] if card_count < len(placements) else WindowPlacement(0, 0, 1280, 720)
                            self._open_site_window(driver, runtime, card_count, target_url, placement, reuse_current=(card_count == 0), role_label=label, role_class=("master" if card_count == 0 else "slave"))
                            if settings.display_mode == "grade_real" and card_count == 0 and (len(entry.urls) + len(entry.slave_urls)) > 1 and not self._has_calibration_for_settings(settings):
                                pause_seconds = max(0.0, float(settings.base_window_pause_seconds))
                                if pause_seconds > 0:
                                    self._log(
                                        f"[{profile.name}] Janela base aberta. Posicione/redimensione em ate {pause_seconds:.1f}s...",
                                        level="INFO",
                                    )
                                    time.sleep(pause_seconds)
                                try:
                                    rect = driver.get_window_rect()
                                    workspace = self._resolve_profile_workspace(settings, job_index, total_profiles)
                                    base_placement = WindowPlacement(
                                        x=int(rect.get("x", placement.x)),
                                        y=int(rect.get("y", placement.y)),
                                        width=int(rect.get("width", placement.width)),
                                        height=int(rect.get("height", placement.height)),
                                    )
                                    placements = self._build_window_layout_from_base(len(entry.urls) + len(entry.slave_urls), base_placement, workspace, settings)
                                    self._log(
                                        f"[{profile.name}] Janela base capturada x={base_placement.x} y={base_placement.y} w={base_placement.width} h={base_placement.height}.",
                                        level="INFO",
                                    )
                                except Exception as exc:
                                    self._log(f"[{profile.name}] Falha ao capturar janela base: {exc}", level="WARN")
                        card_count += 1
                        if settings.display_mode == "grid":
                            status_label = "tela(s) na grade"
                        elif settings.display_mode == "grade_real":
                            status_label = "janela(s) em grade real"
                        else:
                            status_label = "janela(s) posicionada(s)"
                        self._emit(BotStatusEvent(state="running", message=f"[{profile.name}] {card_count} {status_label}.", profile_id=profile.user_id, profile_name=profile.name, session_id=session_id, opened_tabs=card_count, total_profiles=total_profiles))
                    except BrowserSessionBroken:
                        raise
                    except Exception as exc:
                        if self._is_fatal_session_error(exc):
                            raise BrowserSessionBroken(str(exc)) from exc
                        failure_label = "tela na grade" if settings.display_mode == "grid" else "janela"
                        self._log(f"[{profile.name}] Falha ao adicionar {failure_label}: {exc}", level="WARN")
                    if settings.tab_open_delay_seconds > 0:
                        time.sleep(settings.tab_open_delay_seconds)

                if stop_flag.is_set():
                    break

            if not stop_flag.is_set() and entry.slave_urls:
                self._log(f"[{profile.name}] Abrindo {len(entry.slave_urls)} link(s) da aba Filhas como SLAVE.", level="INFO")
                for slave_index, raw_slave_url in enumerate(entry.slave_urls, start=1):
                    if stop_flag.is_set():
                        break
                    target_url = self._build_passthrough_url(raw_slave_url)
                    label = f"FILHA {slave_index}"
                    self._log(f"[{profile.name}] [filha {slave_index}/{len(entry.slave_urls)}] {label} -> {target_url}", level="INFO")
                    try:
                        self._ensure_driver_alive(driver)
                        if settings.display_mode == "grid":
                            self._grid_add_site(runtime, profile, card_count, target_url, settings, role_label=label, role_class="slave")
                            self._open_site_tab(driver, runtime, card_count, target_url, role_label=label, role_class="slave")
                        else:
                            placement = placements[card_count] if card_count < len(placements) else WindowPlacement(0, 0, 1280, 720)
                            self._open_site_window(driver, runtime, card_count, target_url, placement, reuse_current=False, role_label=label, role_class="slave")
                        card_count += 1
                    except BrowserSessionBroken:
                        raise
                    except Exception as exc:
                        if self._is_fatal_session_error(exc):
                            raise BrowserSessionBroken(str(exc)) from exc
                        failure_label = "tela filha" if settings.display_mode == "grid" else "janela filha"
                        self._log(f"[{profile.name}] Falha ao adicionar {failure_label}: {exc}", level="WARN")
                    if settings.tab_open_delay_seconds > 0:
                        time.sleep(settings.tab_open_delay_seconds)

                if batch_index < len(entry.batches) and settings.batch_pause_seconds > 0:
                    self._log(f"[{profile.name}] Pausa entre lotes por {settings.batch_pause_seconds:.1f}s.", level="INFO")
                    time.sleep(settings.batch_pause_seconds)

            if stop_flag.is_set():
                return card_count

            self._detect_browser_network(driver, profile, session_id)
            if settings.display_mode == "grid":
                self._verify_grid(runtime, profile.name, card_count)
                self._capture_grid_previews_once(driver, runtime)
                self._start_preview_thread(runtime, settings)
                self._log(f"[{profile.name}] Preview continuo do grid iniciado.", level="INFO")
                self._log(f"[{profile.name}] Concluido! {card_count} tela(s) | layout: {active_script.name}", level="INFO")
            elif settings.display_mode == "grade_real":
                self._log(f"[{profile.name}] Concluido! {card_count} janela(s) em grade real | layout: {active_script.name}", level="INFO")
            else:
                self._log(f"[{profile.name}] Concluido! {card_count} janela(s) em mosaico | layout: {active_script.name}", level="INFO")
            job_completed = True
            return card_count

        except BrowserSessionBroken as exc:
            runtime.state = "BROKEN"
            self._log(f"[{profile.name}] Sessao perdida: {exc}", level="ERROR")
            raise
        finally:
            stop_requested = stop_flag.is_set()
            if browser_started and (runtime.state == "BROKEN" or (not job_completed and not stop_requested)):
                self._close_profile_safe(profile.user_id, profile.name)
            elif stop_requested:
                self._close_profile_safe(profile.user_id, profile.name)
            elif not job_completed:
                self._unregister_driver(profile.user_id)
                self._clear_stop_flag(profile.user_id)
                self._runtimes.pop(profile.user_id, None)

    def stop_profiles(self, profile_ids: list[str]) -> None:
        targets = profile_ids or self.get_active_profile_ids()
        for profile_id in targets:
            self._close_profile_safe(profile_id, self._active_profiles.get(profile_id, profile_id))

    def stop_current(self, profile_id: str) -> None:
        self._close_profile_safe(profile_id, self._active_profiles.get(profile_id, profile_id))

    def shutdown(self) -> None:
        self.stop_profiles([])

    @staticmethod
    def open_directory(path: str) -> None:
        target = str(path)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", target])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", target])
        else:
            subprocess.Popen(["xdg-open", target])

    def _close_profile_safe(self, profile_id: str, profile_name: str) -> None:
        self._mark_stop_requested(profile_id)
        runtime = self._runtimes.get(profile_id)
        if runtime:
            runtime.state = "CLOSING"
            runtime.preview_stop.set()
        with self._closing_lock:
            if profile_id in self._closing_profiles:
                return
            self._closing_profiles.add(profile_id)

        try:
            driver = None
            with self._drivers_lock:
                driver = self._drivers.pop(profile_id, None)
                self._active_profiles.pop(profile_id, None)

            if driver is not None:
                try:
                    driver.quit()
                except Exception as exc:
                    self._log(f"Erro ao encerrar Selenium de {profile_name}: {exc}", level="WARN")

            for attempt in range(1, 4):
                try:
                    payload = self.adspower_client.stop_browser(profile_id, timeout=10 + attempt * 5)
                    code = payload.get("code")
                    msg = str(payload.get("msg", "")).lower()
                    if code == 0:
                        self._log(f"Perfil {profile_name} encerrado.", level="WARN")
                        return
                    if "user_id is not open" in msg:
                        self._log(f"Perfil {profile_name} ja estava fechado.", level="INFO")
                        return
                    if "too many request" in msg:
                        self._log(f"Encerrar perfil {profile_name}: {payload.get('msg')}", level="WARN")
                        time.sleep(1.5 * attempt)
                        continue
                    self._log(f"Encerrar perfil {profile_name}: {payload.get('msg', 'sem resposta')}", level="WARN")
                    return
                except Exception as exc:
                    message = str(exc).lower()
                    if "read timed out" in message:
                        time.sleep(1.0 * attempt)
                        continue
                    self._log(f"Erro ao encerrar perfil {profile_name}: {exc}", level="ERROR")
                    return
        finally:
            with self._closing_lock:
                self._closing_profiles.discard(profile_id)
            if runtime:
                runtime.state = "CLOSED"
                clear_grid_session(runtime.session_id)
            self._unregister_driver(profile_id)
            self._clear_stop_flag(profile_id)
            self._runtimes.pop(profile_id, None)

    def _build_hush_plus_cdp_source(self, config: dict | None = None) -> str:
        speed_config = normalize_speed_config_for_cdp(config or build_html5_speed_config(self.workspace_data))
        return (
            "window.__ltdfSpeedInitialConfig = "
            + json.dumps(speed_config, ensure_ascii=False)
            + ";\n"
            + SPEED_HACK_PAGE_SCRIPT
            + "\n//# sourceURL=ltdf_hush_plus_cdp.js"
        )

    def _install_hush_plus_cdp_hooks(self, driver: webdriver.Chrome, config: dict | None = None) -> None:
        source = self._build_hush_plus_cdp_source(config)
        try:
            driver.execute_cdp_cmd("Page.enable", {})
        except Exception:
            pass
        try:
            driver.execute_cdp_cmd("Runtime.enable", {})
        except Exception:
            pass
        if str(os.environ.get("LTDF_CDP_BYPASS_CSP", "")).strip().lower() in {"1", "true", "yes", "on"}:
            try:
                driver.execute_cdp_cmd("Page.setBypassCSP", {"enabled": True})
            except Exception:
                pass
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": source})
        try:
            driver.execute_cdp_cmd("Runtime.evaluate", {"expression": source, "awaitPromise": False})
        except Exception:
            pass

    def _connect_driver(self, profile_id: str, profile_name: str, browser_info: AdsPowerBrowserInfo) -> webdriver.Chrome:
        options = Options()
        options.add_experimental_option("debuggerAddress", f"127.0.0.1:{browser_info.debug_port}")
        driver = webdriver.Chrome(service=Service(executable_path=browser_info.webdriver), options=options)
        try:
            driver.set_script_timeout(5)
            driver.set_page_load_timeout(12)
        except Exception:
            pass
        try:
            self._install_hush_plus_cdp_hooks(driver)
            self._log(f"[{profile_name}] HUSH+ CDP preloader instalado.", level="INFO")
        except Exception as exc:
            self._log(f"[{profile_name}] Falha ao instalar HUSH+ CDP preloader: {exc}", level="WARN")
        with self._drivers_lock:
            self._drivers[profile_id] = driver
            self._active_profiles[profile_id] = profile_name
        return driver

    def _unregister_driver(self, profile_id: str) -> None:
        with self._drivers_lock:
            self._drivers.pop(profile_id, None)
            self._active_profiles.pop(profile_id, None)

    def _poll_active_browser(self, profile_id: str, attempts: int = 16, sleep_seconds: float = 1.0) -> AdsPowerBrowserInfo:
        last_error: Exception | None = None
        for _ in range(attempts):
            try:
                browser_info = self.adspower_client.get_active_browser(profile_id)
            except Exception as exc:
                last_error = exc
                time.sleep(sleep_seconds)
                continue
            if browser_info.debug_port and browser_info.webdriver:
                return browser_info
            time.sleep(sleep_seconds)
        if last_error is not None:
            raise RuntimeError(f"Erro ao aguardar browser ativo: {last_error}") from last_error
        return AdsPowerBrowserInfo()

    def _reset_roles(self, relay_config: RelayConfig, session_id: str) -> None:
        response = requests.get(f"{relay_config.http_origin}/reset_roles", params={"session_id": session_id}, timeout=3)
        response.raise_for_status()
        self._log(f"reset_roles[{session_id}] OK: {response.json()}", level="INFO")

    def _build_registered_url(self, raw_url: str) -> str:
        current = raw_url.strip()
        if not current.startswith("http"):
            current = f"https://{current}"
        parts = urlparse(current)
        path = parts.path or "/"
        if not path.endswith("/registered"):
            base = path.rstrip("/")
            path = f"{base}/registered" if base else "/registered"
        return parts._replace(path=path, params="", query="", fragment="").geturl()

    def _build_passthrough_url(self, raw_url: str) -> str:
        current = raw_url.strip()
        if not current.startswith("http"):
            current = f"https://{current}"
        return current


    def _build_grid_dashboard_url(self, relay_config: RelayConfig, profile: AdsPowerProfile, session_id: str, settings: VolumeSettings) -> str:
        from urllib.parse import quote
        profile_q = quote(profile.name, safe="")
        session_q = quote(session_id, safe="")
        return f"{relay_config.local_origin}/grid?profile={profile_q}&session_id={session_q}&cols={max(1, settings.grid_columns)}"

    def _find_chrome_binary(self) -> str | None:
        candidates = [
            os.environ.get("CHROME_PATH"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        return None

    def _launch_dashboard_window(self, relay_config: RelayConfig, profile: AdsPowerProfile, session_id: str, settings: VolumeSettings) -> None:
        dashboard_url = self._build_grid_dashboard_url(relay_config=relay_config, profile=profile, session_id=session_id, settings=settings)
        chrome = self._find_chrome_binary()
        try:
            if chrome:
                subprocess.Popen([chrome, "--new-window", "--start-maximized", f"--app={dashboard_url}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif os.name == "nt":
                os.startfile(dashboard_url)  # type: ignore[attr-defined]
            else:
                webbrowser.open_new(dashboard_url)
        except Exception as exc:
            self._log(f"[{profile.name}] Falha ao abrir dashboard externo: {exc}", level="WARN")

    def _wait_grid_ready(self, driver: webdriver.Chrome, timeout: float = 15.0) -> None:
        deadline = time.time() + timeout
        last_error = ""
        while time.time() < deadline:
            try:
                ready = driver.execute_script("return !!window.__LTDF_GRID__ || !!document.querySelector('.grid')")
                state = driver.execute_script("return document.readyState")
                if ready and state in {'interactive', 'complete'}:
                    return
            except Exception as exc:
                last_error = str(exc)
                if self._is_fatal_session_error(exc):
                    raise BrowserSessionBroken(last_error) from exc
            time.sleep(0.3)
        raise RuntimeError(f"Grid nao inicializou: {last_error or 'timeout'}")

    def _ensure_driver_alive(self, driver: webdriver.Chrome) -> None:
        try:
            _ = driver.current_window_handle
            driver.execute_script("return document.readyState")
        except Exception as exc:
            if self._is_fatal_session_error(exc):
                raise BrowserSessionBroken(str(exc)) from exc
            raise


    def _position_worker_window(self, driver: webdriver.Chrome, runtime: ProfileRuntime) -> None:
        runtime.dashboard_handle = driver.current_window_handle
        try:
            driver.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
        except Exception:
            pass
        try:
            driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": False})
        except Exception:
            pass
        try:
            driver.set_window_rect(x=-32000, y=0, width=1366, height=900)
        except Exception:
            pass
        try:
            driver.minimize_window()
        except Exception:
            pass

    def _grid_add_site(
        self,
        runtime: ProfileRuntime,
        profile: AdsPowerProfile,
        index: int,
        url: str,
        settings: VolumeSettings,
        role_label: str | None = None,
        role_class: str | None = None,
    ) -> None:
        role_label = role_label or ("MASTER" if index == 0 else f"SLAVE {index}")
        role_class = role_class or ("master" if index == 0 else "slave")
        payload = {
            "url": url,
            "index": index,
            "role_label": role_label,
            "role_class": role_class,
            "profile_name": profile.name,
            "state": "loading",
            "counter": 0,
            "stamp": "--",
            "note": f"Abrindo {url}",
        }
        upsert_grid_card(runtime.session_id, index, payload)

    def _grid_update_card(self, runtime: ProfileRuntime, payload: dict) -> None:
        upsert_grid_card(runtime.session_id, int(payload.get("index", 0)), payload)

    def _apply_dashboard_desktop_view(self, driver: webdriver.Chrome) -> None:
        try:
            driver.execute_cdp_cmd("Emulation.clearDeviceMetricsOverride", {})
        except Exception:
            pass
        try:
            driver.execute_cdp_cmd("Emulation.setPageScaleFactor", {"pageScaleFactor": 1})
        except Exception:
            pass
        try:
            driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": False})
        except Exception:
            pass
        try:
            driver.set_window_rect(x=0, y=0, width=1920, height=1080)
        except Exception:
            pass
        try:
            driver.maximize_window()
        except Exception:
            pass

    def _apply_site_mobile_view(self, driver: webdriver.Chrome) -> None:
        try:
            driver.execute_cdp_cmd(
                "Emulation.setDeviceMetricsOverride",
                {
                    "width": MOBILE_VIEWPORT_WIDTH,
                    "height": MOBILE_VIEWPORT_HEIGHT,
                    "deviceScaleFactor": 1,
                    "mobile": True,
                    "screenOrientation": {"type": "portraitPrimary", "angle": 0},
                },
            )
        except Exception:
            pass
        try:
            driver.execute_cdp_cmd("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
        except Exception:
            pass


    def _create_background_window(self, driver: webdriver.Chrome, dashboard_handle: str) -> str:
        self._switch_to_handle(driver, dashboard_handle)
        driver.switch_to.new_window("window")
        background_handle = driver.current_window_handle
        try:
            driver.get("about:blank")
        except Exception:
            pass
        for handle in (background_handle, dashboard_handle):
            try:
                self._switch_to_handle(driver, handle)
                driver.set_window_rect(x=-32000, y=0, width=1366, height=900)
            except Exception:
                pass
            try:
                driver.minimize_window()
            except Exception:
                pass
        self._switch_to_handle(driver, background_handle)
        return background_handle

    def _open_background_tab_cdp(self, driver: webdriver.Chrome, url: str, background_handle: str | None) -> str | None:
        try:
            before = set(driver.window_handles)
            driver.execute_cdp_cmd("Target.createTarget", {"url": "about:blank", "newWindow": False, "background": True})
            deadline = time.time() + 6.0
            while time.time() < deadline:
                current = set(driver.window_handles)
                created = list(current - before)
                if created:
                    handle = created[-1]
                    self._switch_to_handle(driver, handle)
                    self._install_hush_plus_cdp_hooks(driver)
                    driver.get(url)
                    self._focus_game_iframe_after_load(driver)
                    if background_handle:
                        try:
                            self._switch_to_handle(driver, background_handle)
                        except Exception:
                            pass
                    return handle
                time.sleep(0.15)
        except Exception:
            return None
        return None

    def _open_site_tab(
        self,
        driver: webdriver.Chrome,
        runtime: ProfileRuntime,
        index: int,
        url: str,
        role_label: str | None = None,
        role_class: str | None = None,
    ) -> SiteTabState:
        dashboard = runtime.dashboard_handle or driver.current_window_handle
        host_handle = runtime.background_handle or dashboard
        self._switch_to_handle(driver, host_handle)
        handle = self._open_background_tab_cdp(driver, url=url, background_handle=runtime.background_handle)
        if not handle:
            driver.switch_to.new_window("tab")
            handle = driver.current_window_handle
            self._install_hush_plus_cdp_hooks(driver)
            driver.get(url)
            self._focus_game_iframe_after_load(driver)
        self._switch_to_handle(driver, handle)
        self._apply_site_mobile_view(driver)
        state = SiteTabState(
            index=index,
            url=url,
            handle=handle,
            role_label=role_label or ("MASTER" if index == 0 else f"SLAVE {index}"),
            role_class=role_class or ("master" if index == 0 else "slave"),
            last_reload_at=time.time(),
            current_url=url,
        )
        runtime.site_tabs.append(state)
        self._switch_to_handle(driver, dashboard)
        return state

    def _switch_to_handle(self, driver: webdriver.Chrome, handle: str) -> None:
        try:
            driver.switch_to.window(handle)
        except Exception as exc:
            if self._is_fatal_session_error(exc):
                raise BrowserSessionBroken(str(exc)) from exc
            raise

    def _reopen_site_tab(self, driver: webdriver.Chrome, runtime: ProfileRuntime, site: SiteTabState) -> None:
        host_handle = runtime.background_handle or runtime.dashboard_handle or driver.current_window_handle
        self._switch_to_handle(driver, host_handle)
        handle = self._open_background_tab_cdp(driver, url=site.url, background_handle=runtime.background_handle)
        if handle:
            site.handle = handle
        else:
            driver.switch_to.new_window("tab")
            site.handle = driver.current_window_handle
            self._install_hush_plus_cdp_hooks(driver)
            driver.get(site.url)
            self._focus_game_iframe_after_load(driver)
        self._switch_to_handle(driver, site.handle)
        self._apply_site_mobile_view(driver)
        site.last_reload_at = time.time()
        site.current_url = site.url
        self._switch_to_handle(driver, runtime.dashboard_handle or host_handle)

    def _preview_loop(self, profile_id: str, settings: VolumeSettings) -> None:
        stop_flag = self._stop_flags.get(profile_id)
        while True:
            runtime = self._runtimes.get(profile_id)
            if runtime is None:
                return
            if runtime.preview_stop.is_set() or (stop_flag and stop_flag.is_set()):
                return
            if runtime.state in {"BROKEN", "CLOSING", "CLOSED"}:
                return

            with self._drivers_lock:
                driver = self._drivers.get(profile_id)
            if driver is None:
                return

            try:
                self._ensure_driver_alive(driver)
            except BrowserSessionBroken as exc:
                runtime.state = "BROKEN"
                self._log(f"[{runtime.profile_name}] Sessao perdida no preview: {exc}", level="ERROR")
                self._close_profile_safe(profile_id, runtime.profile_name)
                return

            for site in list(runtime.site_tabs):
                if runtime.preview_stop.is_set() or (stop_flag and stop_flag.is_set()):
                    return
                try:
                    with runtime.lock:
                        now = time.time()
                        min_interval = GRID_PREVIEW_MIN_INTERVAL_MASTER if site.index == 0 else GRID_PREVIEW_MIN_INTERVAL_SLAVE
                        if site.last_capture_at and (now - site.last_capture_at) < min_interval:
                            continue
                        self._switch_to_handle(driver, site.handle)
                        if settings.grid_auto_reload_seconds > 0 and (now - site.last_reload_at) >= settings.grid_auto_reload_seconds:
                            try:
                                driver.refresh()
                                site.last_reload_at = now
                                time.sleep(1.0)
                            except Exception:
                                pass

                        title = ""
                        current_url = site.url
                        try:
                            title = driver.title or ""
                            current_url = driver.current_url or site.url
                        except Exception:
                            pass
                        previous_capture_at = float(site.last_capture_at or 0.0)
                        image_base64 = self._capture_frame_base64(driver)
                        site.last_capture_at = time.time()
                        delta = site.last_capture_at - previous_capture_at if previous_capture_at else 0.0
                        fps = round(1.0 / delta, 1) if delta > 0 else 0.0
                        site.capture_count += 1
                        site.retries = 0
                        site.last_error = ""
                        site.title = title
                        site.current_url = current_url
                        self._grid_update_card(runtime, {
                            "index": site.index,
                            "state": "ok",
                            "counter": site.capture_count,
                            "fps": fps,
                            "stamp": time.strftime("%H:%M:%S"),
                            "image_base64": image_base64,
                            "current_url": current_url,
                            "note": f"<b>{title or 'Preview ativa'}</b>",
                        })
                except BrowserSessionBroken as exc:
                    runtime.state = "BROKEN"
                    self._log(f"[{runtime.profile_name}] Sessao perdida no card {site.index}: {exc}", level="ERROR")
                    self._close_profile_safe(profile_id, runtime.profile_name)
                    return
                except Exception as exc:
                    site.retries += 1
                    site.last_error = str(exc)
                    self._grid_update_card(runtime, {
                        "index": site.index,
                        "state": "error",
                        "counter": site.capture_count,
                        "stamp": time.strftime("%H:%M:%S"),
                        "current_url": site.current_url or site.url,
                        "note": f"Erro: {str(exc)[:180]}<br>Retry {site.retries}/{settings.iframe_retry_limit}",
                    })
                    if site.retries <= settings.iframe_retry_limit:
                        try:
                            self._reopen_site_tab(driver, runtime, site)
                        except Exception as reopen_exc:
                            if self._is_fatal_session_error(reopen_exc):
                                runtime.state = "BROKEN"
                                self._close_profile_safe(profile_id, runtime.profile_name)
                                return
                    time.sleep(min(1.5 * site.retries, 4.0))
                time.sleep(0.2)
            time.sleep(0.7)

    def _start_preview_thread(self, runtime: ProfileRuntime, settings: VolumeSettings) -> None:
        if runtime.preview_thread and runtime.preview_thread.is_alive():
            return
        runtime.preview_thread = threading.Thread(
            target=self._preview_loop,
            args=(runtime.profile_id, settings),
            daemon=True,
            name=f"preview-{runtime.profile_id}",
        )
        runtime.preview_thread.start()

    def _verify_grid(self, runtime: ProfileRuntime, profile_name: str, expected_cards: int) -> None:
        snapshot = get_grid_snapshot(runtime.session_id)
        count = len(snapshot.get("cards") or [])
        self._log(f"[{profile_name}] Grid pronta com {count}/{expected_cards} tela(s).", level="INFO")

    def _detect_browser_network(self, driver: webdriver.Chrome, profile: AdsPowerProfile, session_id: str) -> None:
        adapters = detect_network_adapters()
        ips: list[str] = []
        scripts = [
            "return (window.RTCPeerConnection || window.webkitRTCPeerConnection) ? 'rtc' : ''",
        ]
        try:
            _ = driver.current_url
        except Exception:
            return
        info = match_browser_network(ips, adapters) or BrowserNetworkInfo(note="Sem dados de rede do browser.")
        info.profile_id = profile.user_id
        info.profile_name = profile.name
        info.session_id = session_id
        self._emit(NetworkSnapshotEvent(pc_adapters=adapters, browser_networks=[info]))


# ============================================================
# UI - APP UNIFICADO
# ============================================================
class ProfileRow(ctk.CTkFrame):
    def __init__(self, master, profile: AdsPowerProfile, selected: bool = False):
        super().__init__(master, fg_color="transparent")
        self.profile = profile
        self.var = ctk.BooleanVar(value=selected)
        self.chk = ctk.CTkCheckBox(self, text=profile.display_name, variable=self.var, text_color=C_TEXT, font=(FONT_MONO, 12))
        self.chk.pack(anchor="w", padx=2, pady=2)


class CalibrationPreviewWindow(ctk.CTkToplevel):
    """Janela de exemplo para o usuario calibrar tamanho/posicao da grade real."""

    def __init__(self, parent, on_confirm):
        super().__init__(parent)
        self._on_confirm = on_confirm
        self.title("Janela de exemplo - grade real")
        self.geometry("420x760+120+120")
        self.minsize(220, 320)
        self.configure(fg_color="#08121a")
        self.attributes("-topmost", True)

        outer = ctk.CTkFrame(self, fg_color="#0f1822", border_width=2, border_color=C_ACCENT)
        outer.pack(fill="both", expand=True, padx=6, pady=6)

        ctk.CTkLabel(outer, text="JANELA DE EXEMPLO", text_color=C_ACCENT, font=(FONT_MONO, 18, "bold")).pack(pady=(18, 8))
        ctk.CTkLabel(
            outer,
            text=(
                "Arraste e redimensione esta janela\n"
                "ate ficar no formato desejado para a grade real.\n"
                "Depois clique em CONFIRMAR para copiar\n"
                "a posicao e o tamanho para a interface."
            ),
            text_color=C_TEXT,
            font=(FONT_MONO, 12),
            justify="center",
        ).pack(pady=(0, 12))

        self._info = ctk.CTkLabel(outer, text="", text_color=C_MUTED, font=(FONT_MONO, 11))
        self._info.pack(pady=(0, 14))

        footer = ctk.CTkFrame(outer, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=14, pady=14)
        ctk.CTkButton(footer, text="CONFIRMAR", fg_color=C_ACCENT, text_color="#00140c", command=self._confirm).pack(side="left", padx=(0, 8))
        ctk.CTkButton(footer, text="FECHAR", fg_color="#22313f", command=self.destroy).pack(side="left")

        self.bind("<Configure>", self._on_configure)
        self.after(50, self._refresh_info)

    def _on_configure(self, _event=None):
        self._refresh_info()

    def _refresh_info(self):
        try:
            self._info.configure(
                text=f"Posicao: ({self.winfo_x()}, {self.winfo_y()})  |  Tamanho: {self.winfo_width()} x {self.winfo_height()}"
            )
        except Exception:
            pass

    def _confirm(self):
        payload = {
            "x": int(self.winfo_x()),
            "y": int(self.winfo_y()),
            "width": int(self.winfo_width()),
            "height": int(self.winfo_height()),
        }
        try:
            self._on_confirm(payload)
        finally:
            self.destroy()


class LTDFSingleFileApp(ctk.CTk):
    def __init__(self, relay_service: RelayService, browser_runner: BrowserRunner, scripts_manager: ScriptsManager, relay_config: RelayConfig) -> None:
        apply_theme()
        super().__init__()
        self.title("LTDF SNIPER - single file")
        self.geometry("1500x920")
        self.minsize(1180, 760)
        self.configure(fg_color=C_BG)

        self.relay_service = relay_service
        self.browser_runner = browser_runner
        self.scripts_manager = scripts_manager
        self.relay_config = relay_config
        self._relay_port_auto = False
        self._event_queue: queue.Queue[object] = queue.Queue()
        self.runtime_log_path = Path.cwd() / "ltdf_runtime.log"
        try:
            self.runtime_log_path.write_text("", encoding="utf-8")
        except Exception:
            pass
        self._profiles: list[AdsPowerProfile] = []
        self._profile_rows: list[ProfileRow] = []
        self._scripts = self.scripts_manager.load_scripts()
        self._active_script_id = self._scripts[0].id if self._scripts else None
        self.workspace_store = WorkspaceStore()
        self.workspace_data = self.workspace_store.load()
        self.master_urls_state: list[str] = list(self.workspace_data.master_urls)
        self.child_urls_state: list[str] = list(self.workspace_data.child_urls)
        self.completed_links_state: list[str] = list(self.workspace_data.completed_links)
        self.calibrated_cells = self._load_grid()
        self.calibrated_cell = None
        self._bot_running = False
        self._last_network_snapshot: NetworkSnapshotEvent | None = None

        self.relay_service.set_event_sink(self._enqueue_event)
        self.browser_runner.set_event_sink(self._enqueue_event)

        self._build_ui()
        self._sync_scripts_list()
        self._sync_current_calibration_from_selection()
        self._apply_calibrated_cell_to_entries(log_change=False)
        self._refresh_calib_label()
        self._relay_started = self._start_relay_with_fallback()
        if self._relay_port_auto:
            self.add_log(f"Porta 19876 ocupada. Relay iniciou automaticamente na porta {self.relay_config.port}.", "WARN")
        if not self._relay_started:
            self.add_log("Erro critico: relay WebSocket nao iniciou.", "ERROR")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._process_event_queue)
        self.after(250, self.refresh_profiles)
        self.after(350, self.refresh_network_snapshot)

    # ---------- UI ----------
    def _build_ui(self) -> None:
        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(self, fg_color=C_PANEL, corner_radius=0, height=78)
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(top, text="LTDF SNIPER", text_color=C_ACCENT, font=(FONT_DISPLAY, 34, "bold")).grid(row=0, column=0, sticky="w", padx=18, pady=(10, 0))
        ctk.CTkLabel(top, text="arquivo unico com relay + UI + AdsPower + grid + correcoes", text_color=C_MUTED, font=(FONT_MONO, 11)).grid(row=1, column=0, sticky="w", padx=18, pady=(0, 10))

        status = ctk.CTkFrame(self, fg_color="#0a1118", corner_radius=0, height=34)
        status.grid(row=1, column=0, sticky="ew")
        self.status_label = ctk.CTkLabel(status, text="AdsPower: --", text_color=C_TEXT, font=(FONT_MONO, 11, "bold"))
        self.status_label.pack(side="left", padx=16)
        self.port_label = ctk.CTkLabel(status, text=f"Relay: {self.relay_config.host}:{self.relay_config.port}", text_color=C_MUTED, font=(FONT_MONO, 11))
        self.port_label.pack(side="right", padx=16)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, sticky="nsew", padx=14, pady=12)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=1)

        left = ctk.CTkFrame(body, fg_color=C_PANEL, border_width=1, border_color=C_BORDER)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        left.grid_rowconfigure(3, weight=1)

        self._build_left_panel(left)

        right = ctk.CTkFrame(body, fg_color=C_PANEL_2, border_width=1, border_color=C_BORDER)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        controls = ctk.CTkFrame(right, fg_color="transparent")
        controls.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        for i in range(9):
            controls.grid_columnconfigure(i, weight=1 if i in (0, 1, 2, 8) else 0)

        self.operation_label = ctk.CTkLabel(controls, text="Pronto.", text_color=C_MUTED, font=(FONT_MONO, 11))
        self.operation_label.grid(row=0, column=0, sticky="w")
        ctk.CTkButton(controls, text="REFRESH PERFIS", fg_color="#193246", command=self.refresh_profiles).grid(row=0, column=1, padx=6)
        ctk.CTkButton(controls, text="AUTO IP", fg_color="#193246", command=self.auto_detect_relay_ip).grid(row=0, column=2, padx=6)
        ctk.CTkButton(controls, text="GERAR MIRROR", fg_color="#193246", command=self.generate_mirror).grid(row=0, column=3, padx=6)
        ctk.CTkButton(controls, text="START MASTERS", fg_color="#1f6feb", command=self.start_masters_only).grid(row=0, column=4, padx=6)
        ctk.CTkButton(controls, text="START FILHAS", fg_color=C_WARN, text_color="#00140c", command=self.start_children_only).grid(row=0, column=5, padx=6)
        ctk.CTkButton(controls, text="RETOMAR", fg_color="#2f855a", command=self.start_resume_only).grid(row=0, column=6, padx=6)
        ctk.CTkButton(controls, text="STOP", fg_color=C_DANGER, command=self.stop_bot).grid(row=0, column=7, padx=6)
        quick_speed = ctk.CTkFrame(controls, fg_color="#0f1822", border_width=1, border_color=C_BORDER)
        quick_speed.grid(row=1, column=0, columnspan=9, sticky="ew", pady=(10, 0))
        quick_speed.grid_columnconfigure(4, weight=1)
        ctk.CTkLabel(quick_speed, text="HTML5 SPEED", text_color=C_ACCENT, font=(FONT_MONO, 12, "bold")).grid(row=0, column=0, padx=(10, 8), pady=8, sticky="w")
        self.quick_speed_enabled_var = ctk.BooleanVar(value=False)
        self.quick_speed_switch = ctk.CTkSwitch(
            quick_speed,
            text="ATIVO",
            variable=self.quick_speed_enabled_var,
            command=self._toggle_html5_speed_quick,
        )
        self.quick_speed_switch.grid(row=0, column=1, padx=(0, 8), pady=8, sticky="w")
        self.quick_speed_choice_var = ctk.StringVar(value="1x")
        self.quick_speed_menu = ctk.CTkOptionMenu(
            quick_speed,
            values=["0.5x", "1x", "2x", "3x", "4x", "Custom"],
            variable=self.quick_speed_choice_var,
            command=self._set_html5_speed_quick,
            font=(FONT_MONO, 11),
            width=110,
        )
        self.quick_speed_menu.grid(row=0, column=2, padx=(0, 8), pady=8, sticky="w")
        self.quick_speed_status_label = ctk.CTkLabel(quick_speed, text="1.00x", text_color=C_TEXT, font=(FONT_MONO, 11, "bold"))
        self.quick_speed_status_label.grid(row=0, column=3, padx=(0, 8), pady=8, sticky="w")
        ctk.CTkButton(
            quick_speed,
            text="APLICAR SPEED",
            fg_color=C_ACCENT,
            text_color="#00140c",
            command=self.apply_html5_speed_from_ui,
            width=140,
        ).grid(row=0, column=5, padx=(8, 10), pady=8, sticky="e")

        tabs = ctk.CTkTabview(right, fg_color=C_PANEL_2, segmented_button_fg_color="#12202e", segmented_button_selected_color=C_ACCENT, segmented_button_selected_hover_color=C_ACCENT, text_color="#00140c")
        tabs.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        for tab_name in ["Inicio", "Chaves PIX", "Contas", "Proxies", "Telas", "OperaÃ§Ã£o", "Logs"]:
            tabs.add(tab_name)

        self._build_home_tab(tabs.tab("Inicio"))
        self._build_pix_tab(tabs.tab("Chaves PIX"))
        self._build_accounts_tab(tabs.tab("Contas"))
        self._build_proxies_tab(tabs.tab("Proxies"))
        self._build_screens_tab(tabs.tab("Telas"))
        self._build_operation_tab(tabs.tab("OperaÃ§Ã£o"))
        self._build_logs_tab(tabs.tab("Logs"))
        self._load_workspace_into_ui()
        self.browser_runner.set_workspace_data(self.workspace_data)
        self.browser_runner.set_calibrated_cells(self.calibrated_cells)
        self.browser_runner.set_calibrated_cell(self.calibrated_cell)

    def _build_home_tab(self, tab) -> None:
        tab.grid_columnconfigure(0, weight=1)
        shell = ctk.CTkFrame(tab, fg_color="transparent")
        shell.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        shell.grid_columnconfigure(0, weight=1)
        shell.grid_columnconfigure(1, weight=1)

        summary = ctk.CTkFrame(shell, fg_color="#08121a", border_width=1, border_color=C_BORDER)
        summary.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        ctk.CTkLabel(summary, text="PAINEL INICIAL", text_color=C_ACCENT, font=(FONT_MONO, 16, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        ctk.CTkLabel(
            summary,
            text="Baseado no fluxo do vÃ­deo: centraliza modos de navegaÃ§Ã£o, senhas e valores rÃ¡pidos, preservando o relay, mirror e grade real do LTDF.",
            text_color=C_TEXT,
            font=(FONT_MONO, 11),
            justify="left",
            wraplength=760,
        ).pack(anchor="w", padx=12, pady=(0, 12))

        nav = ctk.CTkFrame(shell, fg_color="#0f1822", border_width=1, border_color=C_BORDER)
        nav.grid(row=1, column=0, sticky="nsew", padx=(0, 6))
        ctk.CTkLabel(nav, text="CONFIGURACAO DE NAVEGACAO", text_color=C_TEXT, font=(FONT_MONO, 13, "bold")).pack(anchor="w", padx=12, pady=(12, 8))
        self.mobile_mode_option = ctk.CTkOptionMenu(nav, values=[
            "Modo Mobile (Android e iOS)",
            "Modo Aplicativo Android",
            "Modo Standalone Android",
            "Modo Computador",
        ])
        self.mobile_mode_option.pack(fill="x", padx=12, pady=4)
        self.home_stealth_var = ctk.BooleanVar(value=True)
        self.home_extension_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(nav, text="Chrome indetectavel", variable=self.home_stealth_var).pack(anchor="w", padx=12, pady=6)
        ctk.CTkSwitch(nav, text="Adicionar extensao mirror", variable=self.home_extension_var).pack(anchor="w", padx=12, pady=6)
        ctk.CTkButton(nav, text="SALVAR PREFERENCIAS", fg_color="#193246", command=self.save_workspace_preferences).pack(fill="x", padx=12, pady=(8, 12))

        security = ctk.CTkFrame(shell, fg_color="#0f1822", border_width=1, border_color=C_BORDER)
        security.grid(row=1, column=1, sticky="nsew", padx=(6, 0))
        ctk.CTkLabel(security, text="SEGURANCA & SENHAS", text_color=C_TEXT, font=(FONT_MONO, 13, "bold")).pack(anchor="w", padx=12, pady=(12, 8))
        ctk.CTkLabel(security, text="Senha de cadastro", text_color=C_MUTED, font=(FONT_MONO, 11)).pack(anchor="w", padx=12)
        self.security_password_entry = ctk.CTkEntry(security, font=(FONT_MONO, 12), show="*")
        self.security_password_entry.pack(fill="x", padx=12, pady=(2, 8))
        ctk.CTkLabel(security, text="Senha de saque", text_color=C_MUTED, font=(FONT_MONO, 11)).pack(anchor="w", padx=12)
        self.withdraw_password_entry = ctk.CTkEntry(security, font=(FONT_MONO, 12), show="*")
        self.withdraw_password_entry.pack(fill="x", padx=12, pady=(2, 8))
        ctk.CTkLabel(security, text="Valores para deposito (1 por linha)", text_color=C_MUTED, font=(FONT_MONO, 11)).pack(anchor="w", padx=12)
        self.deposit_values_text = ctk.CTkTextbox(security, height=110, font=(FONT_MONO, 11), fg_color="#08121a", border_width=1, border_color=C_BORDER)
        self.deposit_values_text.pack(fill="both", expand=True, padx=12, pady=(2, 8))
        ctk.CTkButton(security, text="SALVAR SENHAS", fg_color=C_ACCENT, text_color="#00140c", command=self.save_workspace_preferences).pack(fill="x", padx=12, pady=(0, 12))

        speed = ctk.CTkFrame(shell, fg_color="#0f1822", border_width=1, border_color=C_BORDER)
        speed.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        speed.grid_columnconfigure(0, weight=1)
        speed.grid_columnconfigure(1, weight=0)
        ctk.CTkLabel(speed, text="HTML5 SPEED HACK", text_color=C_ACCENT, font=(FONT_MONO, 13, "bold")).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))
        self.html5_speed_enabled_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            speed,
            text="Ativar speed hack em games HTML5",
            variable=self.html5_speed_enabled_var,
            command=self._toggle_html5_speed_home,
        ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 8))
        self.html5_speed_value_label = ctk.CTkLabel(speed, text="1.00x", text_color=C_TEXT, font=(FONT_MONO, 12, "bold"))
        self.html5_speed_value_label.grid(row=1, column=1, sticky="e", padx=12, pady=(0, 8))
        self.html5_speed_slider = ctk.CTkSlider(speed, from_=0.25, to=HTML5_SPEED_MAX_MULTIPLIER, number_of_steps=15, command=self._on_html5_speed_slider_changed)
        self.html5_speed_slider.grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 8))
        ctk.CTkLabel(
            speed,
            text="Aplique a velocidade nas abas abertas e nas proximas abas carregadas pela extensao LTDF.",
            text_color=C_MUTED,
            font=(FONT_MONO, 11),
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 8))
        speed_actions = ctk.CTkFrame(speed, fg_color="transparent")
        speed_actions.grid(row=4, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 12))
        ctk.CTkButton(speed_actions, text="APLICAR AGORA", fg_color=C_ACCENT, text_color="#00140c", command=self.apply_html5_speed_from_ui).pack(side="left")
        ctk.CTkButton(speed_actions, text="SALVAR VELOCIDADE", fg_color="#193246", command=self.save_workspace_preferences).pack(side="left", padx=8)

    def _build_pix_tab(self, tab) -> None:
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(tab, text="CHAVES PIX", text_color=C_ACCENT, font=(FONT_MONO, 15, "bold")).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))
        self.pix_text = ctk.CTkTextbox(tab, font=(FONT_MONO, 11), fg_color="#08121a", border_width=1, border_color=C_BORDER)
        self.pix_text.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        ctk.CTkButton(tab, text="SALVAR CHAVES PIX", fg_color="#193246", command=self.save_workspace_preferences).grid(row=2, column=0, sticky="e", padx=10, pady=(0, 10))

    def _build_accounts_tab(self, tab) -> None:
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(tab, text="CONTAS / OBSERVACOES", text_color=C_ACCENT, font=(FONT_MONO, 15, "bold")).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))
        self.accounts_text = ctk.CTkTextbox(tab, font=(FONT_MONO, 11), fg_color="#08121a", border_width=1, border_color=C_BORDER)
        self.accounts_text.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        ctk.CTkButton(tab, text="SALVAR CONTAS", fg_color="#193246", command=self.save_workspace_preferences).grid(row=2, column=0, sticky="e", padx=10, pady=(0, 10))

    def _build_proxies_tab(self, tab) -> None:
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(tab, text="PROXIES", text_color=C_ACCENT, font=(FONT_MONO, 15, "bold")).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))
        self.proxies_text = ctk.CTkTextbox(tab, font=(FONT_MONO, 11), fg_color="#08121a", border_width=1, border_color=C_BORDER)
        self.proxies_text.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        proxy_actions = ctk.CTkFrame(tab, fg_color="transparent")
        proxy_actions.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        ctk.CTkButton(proxy_actions, text="SALVAR PROXIES", fg_color="#193246", command=self.save_workspace_preferences).pack(side="left")
        ctk.CTkButton(proxy_actions, text="APLICAR NAS SELECIONADAS", fg_color=C_ACCENT, text_color="#00140c", command=self.apply_saved_proxies_to_selected).pack(side="left", padx=8)

    def _build_screens_tab(self, tab) -> None:
        tab.grid_columnconfigure(0, weight=1)
        wrapper = ctk.CTkFrame(tab, fg_color="transparent")
        wrapper.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        ctk.CTkLabel(wrapper, text="TELAS / GRADE REAL", text_color=C_ACCENT, font=(FONT_MONO, 15, "bold")).pack(anchor="w")
        ctk.CTkLabel(
            wrapper,
            text="Use a janela de exemplo para definir a cÃ©lula-base. O bot mantÃ©m grade_real, mirror e relay do LTDF, sÃ³ facilitando a calibragem visual como no vÃ­deo.",
            text_color=C_TEXT,
            font=(FONT_MONO, 11),
            justify="left",
            wraplength=820,
        ).pack(anchor="w", pady=(4, 10))
        ctk.CTkButton(wrapper, text="ABRIR JANELA EXEMPLO", fg_color="#1f6feb", command=self.open_example_window).pack(anchor="w")
        self.screens_status_label = ctk.CTkLabel(wrapper, text="Sem janela modelo confirmada.", text_color=C_MUTED, font=(FONT_MONO, 11))
        self.screens_status_label.pack(anchor="w", pady=(8, 0))

    def _build_operation_tab(self, tab) -> None:
        tab.grid_rowconfigure(1, weight=1)
        tab.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(tab, text="OPERACAO", text_color=C_ACCENT, font=(FONT_MONO, 15, "bold")).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 4))
        inner_tabs = ctk.CTkTabview(tab, fg_color=C_PANEL_2, segmented_button_fg_color="#12202e", segmented_button_selected_color=C_ACCENT, segmented_button_selected_hover_color=C_ACCENT, text_color="#00140c")
        inner_tabs.grid(row=1, column=0, sticky="nsew", padx=0, pady=(0, 0))
        inner_tabs.add("URLs")
        inner_tabs.add("Scripts")

        urls_tab = inner_tabs.tab("URLs")
        urls_tab.grid_rowconfigure(3, weight=1)
        urls_tab.grid_rowconfigure(7, weight=1)
        urls_tab.grid_rowconfigure(10, weight=1)
        urls_tab.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            urls_tab,
            text="LINKS MASTER â€” forcam /registered e definem a janela principal do fluxo.",
            text_color=C_ACCENT,
            font=(FONT_MONO, 11, "bold"),
            justify="left",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(10, 4))
        self.url_input = ctk.CTkEntry(urls_tab, placeholder_text="https://site.com ou dominio.com", font=(FONT_MONO, 12))
        self.url_input.grid(row=1, column=0, sticky="ew", padx=(10, 6), pady=6)
        ctk.CTkButton(urls_tab, text="ADICIONAR MASTER", fg_color="#193246", command=lambda: self.add_url(self.url_input.get())).grid(row=1, column=1, padx=(0, 6), pady=6)
        ctk.CTkButton(urls_tab, text="IMPORTAR MASTERS", fg_color="#193246", command=self.import_bulk_urls).grid(row=1, column=2, padx=(0, 10), pady=6)
        self.urls_frame = ctk.CTkScrollableFrame(urls_tab, fg_color="#08121a", border_color=C_BORDER, border_width=1)
        self.urls_frame.grid(row=3, column=0, columnspan=3, sticky="nsew", padx=10, pady=(0, 10))
        ctk.CTkLabel(
            urls_tab,
            text="LINKS FILHAS â€” abrem crus, sem forcar /registered, e sempre entram como SLAVE.",
            text_color=C_MUTED,
            font=(FONT_MONO, 11, "bold"),
            justify="left",
        ).grid(row=4, column=0, columnspan=3, sticky="w", padx=10, pady=(4, 4))
        self.child_url_input = ctk.CTkEntry(urls_tab, placeholder_text="https://site.com/todas ou link profundo", font=(FONT_MONO, 12))
        self.child_url_input.grid(row=5, column=0, sticky="ew", padx=(10, 6), pady=6)
        ctk.CTkButton(urls_tab, text="ADICIONAR FILHA", fg_color="#193246", command=lambda: self.add_child_url(self.child_url_input.get())).grid(row=5, column=1, padx=(0, 6), pady=6)
        ctk.CTkButton(urls_tab, text="IMPORTAR FILHAS", fg_color="#193246", command=self.import_bulk_child_urls).grid(row=5, column=2, padx=(0, 10), pady=6)
        self.child_urls_frame = ctk.CTkScrollableFrame(urls_tab, fg_color="#08121a", border_color=C_BORDER, border_width=1)
        self.child_urls_frame.grid(row=7, column=0, columnspan=3, sticky="nsew", padx=10, pady=(0, 10))
        ctk.CTkLabel(
            urls_tab,
            text="CASAS FEITAS â€” links removidos ficam salvos aqui.",
            text_color="#88cfff",
            font=(FONT_MONO, 11, "bold"),
            justify="left",
        ).grid(row=8, column=0, columnspan=3, sticky="w", padx=10, pady=(4, 4))
        history_actions = ctk.CTkFrame(urls_tab, fg_color="transparent")
        history_actions.grid(row=9, column=0, columnspan=3, sticky="ew", padx=10, pady=(0, 6))
        ctk.CTkButton(history_actions, text="LIMPAR HISTORICO", fg_color=C_DANGER, command=self.clear_completed_links).pack(side="left")
        self.completed_urls_frame = ctk.CTkScrollableFrame(urls_tab, fg_color="#08121a", border_color=C_BORDER, border_width=1)
        self.completed_urls_frame.grid(row=10, column=0, columnspan=3, sticky="nsew", padx=10, pady=(0, 10))

        scripts_tab = inner_tabs.tab("Scripts")
        scripts_tab.grid_rowconfigure(1, weight=1)
        scripts_tab.grid_columnconfigure(1, weight=1)
        self.script_list = ctk.CTkTextbox(scripts_tab, width=260, font=(FONT_MONO, 11), fg_color="#08121a", border_color=C_BORDER, border_width=1)
        self.script_list.grid(row=0, column=0, rowspan=2, sticky="nsw", padx=(10, 6), pady=10)
        self.script_editor = ctk.CTkTextbox(scripts_tab, font=(FONT_MONO, 11), fg_color="#08121a", border_color=C_BORDER, border_width=1)
        self.script_editor.grid(row=0, column=1, sticky="nsew", padx=(6, 10), pady=(10, 6))
        script_actions = ctk.CTkFrame(scripts_tab, fg_color="transparent")
        script_actions.grid(row=1, column=1, sticky="ew", padx=(6, 10), pady=(0, 10))
        ctk.CTkButton(script_actions, text="NOVO", fg_color="#193246", command=self.new_script).pack(side="left", padx=(0, 6))
        ctk.CTkButton(script_actions, text="SALVAR", fg_color="#193246", command=self.save_active_script).pack(side="left", padx=6)
        ctk.CTkButton(script_actions, text="DELETAR", fg_color=C_DANGER, command=self.delete_active_script).pack(side="left", padx=6)
        ctk.CTkButton(script_actions, text="ATIVAR SELECIONADO", fg_color=C_ACCENT, text_color="#00140c", command=self.activate_script_from_list).pack(side="left", padx=6)

    def _build_logs_tab(self, tab) -> None:
        tab.grid_rowconfigure(0, weight=1)
        tab.grid_columnconfigure(0, weight=1)
        self.logs_box = ctk.CTkTextbox(tab, font=(FONT_MONO, 11), fg_color="#08121a", border_color=C_BORDER, border_width=1)
        self.logs_box.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)

    def _load_workspace_into_ui(self) -> None:
        data = self.workspace_data
        if hasattr(self, "mobile_mode_option"):
            self.mobile_mode_option.set(data.mobile_mode)
        if hasattr(self, "home_stealth_var"):
            self.home_stealth_var.set(bool(data.stealth_mode))
        if hasattr(self, "home_extension_var"):
            self.home_extension_var.set(bool(data.add_extension_mode))
        if hasattr(self, "security_password_entry"):
            self.security_password_entry.delete(0, "end")
            self.security_password_entry.insert(0, data.security_password)
        if hasattr(self, "withdraw_password_entry"):
            self.withdraw_password_entry.delete(0, "end")
            self.withdraw_password_entry.insert(0, data.withdraw_password)
        if hasattr(self, "deposit_values_text"):
            self.deposit_values_text.delete("1.0", "end")
            self.deposit_values_text.insert("1.0", "\n".join(data.deposit_values))
        if hasattr(self, "pix_text"):
            self.pix_text.delete("1.0", "end")
            self.pix_text.insert("1.0", "\n".join(data.pix_keys))
        if hasattr(self, "accounts_text"):
            self.accounts_text.delete("1.0", "end")
            self.accounts_text.insert("1.0", "\n".join(data.accounts))
        if hasattr(self, "proxies_text"):
            self.proxies_text.delete("1.0", "end")
            self.proxies_text.insert("1.0", "\n".join(data.proxies))
        if hasattr(self, "html5_speed_enabled_var"):
            self.html5_speed_enabled_var.set(bool(data.html5_speed_enabled))
        if hasattr(self, "html5_speed_slider"):
            self.html5_speed_slider.set(float(data.html5_speed or 1.0))
            self._on_html5_speed_slider_changed(float(data.html5_speed or 1.0))
        self._sync_speed_controls()
        self.master_urls_state = list(data.master_urls)
        self.child_urls_state = list(data.child_urls)
        self.completed_links_state = list(data.completed_links)
        if hasattr(self, "urls_frame"):
            self._render_master_urls()
        if hasattr(self, "child_urls_frame"):
            self._render_child_urls()
        if hasattr(self, "completed_urls_frame"):
            self._render_completed_links()

    def save_workspace_preferences(self) -> None:
        self.workspace_data = WorkspaceData(
            mobile_mode=self.mobile_mode_option.get() if hasattr(self, "mobile_mode_option") else self.workspace_data.mobile_mode,
            stealth_mode=bool(self.home_stealth_var.get()) if hasattr(self, "home_stealth_var") else self.workspace_data.stealth_mode,
            add_extension_mode=bool(self.home_extension_var.get()) if hasattr(self, "home_extension_var") else self.workspace_data.add_extension_mode,
            security_password=self.security_password_entry.get().strip() if hasattr(self, "security_password_entry") else self.workspace_data.security_password,
            withdraw_password=self.withdraw_password_entry.get().strip() if hasattr(self, "withdraw_password_entry") else self.workspace_data.withdraw_password,
            pix_keys=[line.strip() for line in self.pix_text.get("1.0", "end").splitlines() if line.strip()] if hasattr(self, "pix_text") else list(self.workspace_data.pix_keys),
            accounts=[line.strip() for line in self.accounts_text.get("1.0", "end").splitlines() if line.strip()] if hasattr(self, "accounts_text") else list(self.workspace_data.accounts),
            proxies=[line.strip() for line in self.proxies_text.get("1.0", "end").splitlines() if line.strip()] if hasattr(self, "proxies_text") else list(self.workspace_data.proxies),
            deposit_values=[line.strip() for line in self.deposit_values_text.get("1.0", "end").splitlines() if line.strip()] if hasattr(self, "deposit_values_text") else list(self.workspace_data.deposit_values),
            master_urls=list(getattr(self, "master_urls_state", [])),
            child_urls=list(getattr(self, "child_urls_state", [])),
            completed_links=list(getattr(self, "completed_links_state", [])),
            html5_speed_enabled=bool(self.html5_speed_enabled_var.get()) if hasattr(self, "html5_speed_enabled_var") else self.workspace_data.html5_speed_enabled,
            html5_speed=float(self.html5_speed_slider.get()) if hasattr(self, "html5_speed_slider") else self.workspace_data.html5_speed,
        )
        self.workspace_store.save(self.workspace_data)
        self.browser_runner.set_workspace_data(self.workspace_data)
        self.add_log("Preferencias salvas com sucesso.", "INFO")

    def _persist_url_workspace_state(self) -> None:
        self.workspace_data = WorkspaceData(
            mobile_mode=self.mobile_mode_option.get() if hasattr(self, "mobile_mode_option") else self.workspace_data.mobile_mode,
            stealth_mode=bool(self.home_stealth_var.get()) if hasattr(self, "home_stealth_var") else self.workspace_data.stealth_mode,
            add_extension_mode=bool(self.home_extension_var.get()) if hasattr(self, "home_extension_var") else self.workspace_data.add_extension_mode,
            security_password=self.security_password_entry.get().strip() if hasattr(self, "security_password_entry") else self.workspace_data.security_password,
            withdraw_password=self.withdraw_password_entry.get().strip() if hasattr(self, "withdraw_password_entry") else self.workspace_data.withdraw_password,
            pix_keys=[line.strip() for line in self.pix_text.get("1.0", "end").splitlines() if line.strip()] if hasattr(self, "pix_text") else list(self.workspace_data.pix_keys),
            accounts=[line.strip() for line in self.accounts_text.get("1.0", "end").splitlines() if line.strip()] if hasattr(self, "accounts_text") else list(self.workspace_data.accounts),
            proxies=[line.strip() for line in self.proxies_text.get("1.0", "end").splitlines() if line.strip()] if hasattr(self, "proxies_text") else list(self.workspace_data.proxies),
            deposit_values=[line.strip() for line in self.deposit_values_text.get("1.0", "end").splitlines() if line.strip()] if hasattr(self, "deposit_values_text") else list(self.workspace_data.deposit_values),
            master_urls=list(getattr(self, "master_urls_state", [])),
            child_urls=list(getattr(self, "child_urls_state", [])),
            completed_links=list(getattr(self, "completed_links_state", [])),
            html5_speed_enabled=bool(self.html5_speed_enabled_var.get()) if hasattr(self, "html5_speed_enabled_var") else self.workspace_data.html5_speed_enabled,
            html5_speed=float(self.html5_speed_slider.get()) if hasattr(self, "html5_speed_slider") else self.workspace_data.html5_speed,
        )
        self.workspace_store.save(self.workspace_data)
        self.browser_runner.set_workspace_data(self.workspace_data)

    def _on_html5_speed_slider_changed(self, value) -> None:
        try:
            speed = float(value)
        except Exception:
            speed = 1.0
        if hasattr(self, "html5_speed_value_label"):
            self.html5_speed_value_label.configure(text=f"{speed:.2f}x")
        if hasattr(self, "quick_speed_status_label"):
            self.quick_speed_status_label.configure(text=f"{speed:.2f}x")
        if hasattr(self, "quick_speed_choice_var"):
            self.quick_speed_choice_var.set(self._speed_to_quick_choice(speed))

    def _speed_to_quick_choice(self, speed: float) -> str:
        presets = {
            0.5: "0.5x",
            1.0: "1x",
            2.0: "2x",
            3.0: "3x",
            4.0: "4x",
        }
        for preset, label in presets.items():
            if abs(speed - preset) < 0.05:
                return label
        return "Custom"

    def _quick_choice_to_speed(self, choice: str) -> float | None:
        mapping = {
            "0.5x": 0.5,
            "1x": 1.0,
            "2x": 2.0,
            "3x": 3.0,
            "4x": 4.0,
        }
        return mapping.get(choice)

    def _sync_speed_controls(self) -> None:
        enabled = bool(self.html5_speed_enabled_var.get()) if hasattr(self, "html5_speed_enabled_var") else bool(self.workspace_data.html5_speed_enabled)
        try:
            speed = float(self.html5_speed_slider.get()) if hasattr(self, "html5_speed_slider") else float(self.workspace_data.html5_speed or 1.0)
        except Exception:
            speed = 1.0
        if hasattr(self, "quick_speed_enabled_var"):
            self.quick_speed_enabled_var.set(enabled)
        if hasattr(self, "quick_speed_status_label"):
            self.quick_speed_status_label.configure(text=f"{speed:.2f}x")
        if hasattr(self, "quick_speed_choice_var"):
            self.quick_speed_choice_var.set(self._speed_to_quick_choice(speed))

    def _toggle_html5_speed_home(self) -> None:
        self._sync_speed_controls()
        self.apply_html5_speed_from_ui()

    def _toggle_html5_speed_quick(self) -> None:
        if hasattr(self, "html5_speed_enabled_var") and hasattr(self, "quick_speed_enabled_var"):
            self.html5_speed_enabled_var.set(bool(self.quick_speed_enabled_var.get()))
        self._sync_speed_controls()
        self.apply_html5_speed_from_ui()

    def _set_html5_speed_quick(self, choice: str) -> None:
        speed = self._quick_choice_to_speed(choice)
        if speed is None:
            self._sync_speed_controls()
            return
        if hasattr(self, "html5_speed_slider"):
            self.html5_speed_slider.set(speed)
        self._on_html5_speed_slider_changed(speed)
        self.apply_html5_speed_from_ui()

    def apply_html5_speed_from_ui(self) -> None:
        try:
            self.add_log("Speed HTML5: comando recebido pelo painel.", "INFO")
            self.save_workspace_preferences()
            try:
                selected_speed = float(self.html5_speed_slider.get()) if hasattr(self, "html5_speed_slider") else float(self.workspace_data.html5_speed or 1.0)
            except Exception:
                selected_speed = 1.0
            selected_speed = max(0.1, min(HTML5_SPEED_MAX_MULTIPLIER, selected_speed))
            selected_enabled = selected_speed > 1.05
            if hasattr(self, "html5_speed_enabled_var"):
                self.html5_speed_enabled_var.set(selected_enabled)
            if hasattr(self, "quick_speed_enabled_var"):
                self.quick_speed_enabled_var.set(selected_enabled)
            speed_update = {"html5_speed_enabled": selected_enabled, "html5_speed": selected_speed}
            if hasattr(self.workspace_data, "model_copy"):
                self.workspace_data = self.workspace_data.model_copy(update=speed_update)
            else:
                self.workspace_data = self.workspace_data.copy(update=speed_update)
            self.workspace_store.save(self.workspace_data)
            self.browser_runner.set_workspace_data(self.workspace_data)
            config = build_html5_speed_config(self.workspace_data)
            self.add_log(
                f"Speed HTML5 config local: enabled={config.get('enabled')} speed={config.get('speed', 1.0):.2f}x",
                "INFO",
            )
            if getattr(self, "_speed_apply_running", False):
                started_at = float(getattr(self, "_speed_apply_started_at", 0) or 0)
                if time.time() - started_at < 8:
                    self.add_log("Speed HTML5: aplicacao anterior ainda em andamento; aguarde finalizar antes de enviar outra.", "WARN")
                    self._sync_speed_controls()
                    return
                self.add_log("Speed HTML5: liberando aplicacao anterior presa por timeout.", "WARN")
            self._speed_apply_running = True
            self._speed_apply_started_at = time.time()
            self.add_log(f"Speed HTML5 solicitado em {config.get('speed', 1.0):.2f}x; aplicando em segundo plano.", "INFO")
        except Exception as exc:
            self.add_log(f"Speed HTML5 falhou antes de iniciar worker: {exc}", "ERROR")
            try:
                self._sync_speed_controls()
            except Exception:
                pass
            return

        def ui_log(message: str, level: str = "INFO") -> None:
            try:
                self.after(0, lambda: self.add_log(message, level))
            except Exception:
                pass

        def worker() -> None:
            try:
                self.browser_runner.apply_html5_speed_config(config)
                url = f"http://127.0.0.1:{self.relay_config.port}/speed_config"
                payload = {"config": config}
                response = requests.post(url, json=payload, timeout=2)
                response.raise_for_status()
                status = "ativado" if config.get("enabled") else "desativado"
                ui_log(f"Speed HTML5 {status} em {config.get('speed', 1.0):.2f}x aplicado via relay.", "INFO")
            except Exception as exc:
                ui_log(f"Falha ao propagar speed HTML5 via relay: {exc}", "WARN")
            finally:
                self._speed_apply_running = False
                self._speed_apply_started_at = 0

        threading.Thread(target=worker, daemon=True).start()
        self._sync_speed_controls()

    def apply_saved_proxies_to_selected(self) -> None:
        self.save_workspace_preferences()
        selected = self.get_selected_profile_ids()
        if not selected:
            self.add_log("Selecione pelo menos um perfil para associar proxies.", "WARN")
            return
        if not self.workspace_data.proxies:
            self.add_log("Nenhum proxy salvo para aplicar.", "WARN")
            return
        self.add_log(
            f"Proxies salvos para uso operacional: {len(self.workspace_data.proxies)} item(ns) prontos para {len(selected)} perfil(is).",
            "INFO",
        )

    def _build_left_panel(self, parent) -> None:
        ctk.CTkLabel(parent, text="PERFIS ADSPOWER", text_color=C_TEXT, font=(FONT_MONO, 13, "bold")).grid(row=0, column=0, sticky="w", padx=10, pady=(10, 6))
        self.relay_ip_entry = ctk.CTkEntry(parent, font=(FONT_MONO, 12), width=280)
        self.relay_ip_entry.grid(row=1, column=0, padx=10, sticky="ew")
        self.relay_ip_entry.insert(0, self.relay_config.host)
        self.pc_network_label = ctk.CTkLabel(parent, text="PC: --", text_color=C_MUTED, font=(FONT_MONO, 11))
        self.pc_network_label.grid(row=2, column=0, sticky="w", padx=10, pady=(6, 6))

        self.profile_scroll = ctk.CTkScrollableFrame(parent, width=320, fg_color="#08121a", border_width=1, border_color=C_BORDER)
        self.profile_scroll.grid(row=3, column=0, sticky="nsew", padx=10, pady=(0, 10))

        grade_actions = ctk.CTkFrame(parent, fg_color="transparent")
        grade_actions.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 8))
        grade_actions.grid_columnconfigure(0, weight=1)
        grade_actions.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(grade_actions, text="ABRIR JANELA EXEMPLO", fg_color="#1f6feb", command=self.open_example_window).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.example_hint_label = ctk.CTkLabel(grade_actions, text="Sem janela modelo confirmada.", text_color=C_MUTED, font=(FONT_MONO, 10))
        self.example_hint_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        settings = ctk.CTkScrollableFrame(parent, height=320, fg_color="#0f1822", border_width=1, border_color=C_BORDER)
        settings.grid(row=5, column=0, sticky="ew", padx=10, pady=(0, 10))
        self.desktop_monitors = list_desktop_monitors()
        self.all_monitors_bounds = detect_all_monitors_bounds(self.desktop_monitors)
        default_monitor = self.all_monitors_bounds
        self.monitor_choice_var = ctk.StringVar(value="Todos os monitores")
        self.assignment_var = ctk.StringVar(value="distribute")
        self.display_mode_var = ctk.StringVar(value="grade_real")
        self.concurrent_entry = self._labeled_entry(settings, "Concorrencia", "2", 0)
        self.batch_entry = self._labeled_entry(settings, "Lote", "8", 1)
        self.limit_entry = self._labeled_entry(settings, "Limite por perfil", "24", 2)
        self.delay_entry = self._labeled_entry(settings, "Delay abrir", "0.15", 3)
        ctk.CTkLabel(settings, text="Monitor alvo", text_color=C_MUTED, font=(FONT_MONO, 11)).grid(row=4, column=0, sticky="w", padx=8, pady=(8, 0))
        self.monitor_choice_menu = ctk.CTkOptionMenu(
            settings,
            values=["Todos os monitores", *[format_monitor_label(i, mon) for i, mon in enumerate(self.desktop_monitors)]],
            variable=self.monitor_choice_var,
            command=self._on_monitor_selected,
            font=(FONT_MONO, 11),
        )
        self.monitor_choice_menu.grid(row=4, column=1, sticky="ew", padx=8, pady=(8, 0))
        self.refresh_monitors_button = ctk.CTkButton(settings, text="Atualizar monitores", fg_color="#1f6feb", command=self._reload_monitor_list, font=(FONT_MONO, 11))
        self.refresh_monitors_button.grid(row=5, column=0, columnspan=2, sticky="ew", padx=8, pady=(6, 0))
        self.origin_x_entry = self._labeled_entry(settings, "Origem X", str(default_monitor.x), 6)
        self.origin_y_entry = self._labeled_entry(settings, "Origem Y", str(default_monitor.y), 7)
        self.area_width_entry = self._labeled_entry(settings, "Area largura", str(default_monitor.width), 8)
        self.area_height_entry = self._labeled_entry(settings, "Area altura", str(default_monitor.height), 9)
        self.layout_cols_entry = self._labeled_entry(settings, "Cols janela 0=auto", "0", 10)
        self.window_gap_entry = self._labeled_entry(settings, "Gap janela", "6", 11)
        self.base_pause_entry = self._labeled_entry(settings, "Pausa janela base", "5", 12)
        self.reload_entry = self._labeled_entry(settings, "Reload grid", "30", 13)
        self.grid_cols_entry = self._labeled_entry(settings, "Cols grid 0=auto", "0", 14)
        self.retry_entry = self._labeled_entry(settings, "Retry iframe", "3", 15)
        ctk.CTkLabel(settings, text="Modo atribuicao", text_color=C_MUTED, font=(FONT_MONO, 11)).grid(row=16, column=0, sticky="w", padx=8, pady=(6, 0))
        seg = ctk.CTkSegmentedButton(settings, values=["distribute", "replicate"], variable=self.assignment_var)
        seg.grid(row=17, column=0, sticky="ew", padx=8, pady=(2, 4))
        ctk.CTkLabel(settings, text="Modo visualizacao", text_color=C_MUTED, font=(FONT_MONO, 11)).grid(row=18, column=0, sticky="w", padx=8, pady=(6, 0))
        view_seg = ctk.CTkSegmentedButton(settings, values=["windows", "grade_real", "grid"], variable=self.display_mode_var)
        view_seg.grid(row=19, column=0, sticky="ew", padx=8, pady=(2, 8))

    def open_example_window(self) -> None:
        self.add_log("Abrindo janela de exemplo para calibracao manual.", "INFO")
        win = CalibrationPreviewWindow(self, self._apply_example_window)
        try:
            win.focus_force()
        except Exception:
            pass

    def _load_grid(self) -> dict[str, tuple[int, int, int, int]]:
        try:
            if _GRID_FILE.exists():
                payload = json.loads(_GRID_FILE.read_text(encoding="utf-8"))
                cells = payload.get("cells_by_monitor")
                if isinstance(cells, dict):
                    normalized: dict[str, tuple[int, int, int, int]] = {}
                    for key, value in cells.items():
                        if isinstance(value, list) and len(value) == 4:
                            normalized[str(key)] = tuple(int(v) for v in value)
                    return normalized
                cell = payload.get("cell")
                if isinstance(cell, list) and len(cell) == 4:
                    legacy = tuple(int(v) for v in cell)
                    monitor = detect_monitor_from_cell(legacy)
                    return {monitor_storage_key(monitor): legacy}
        except Exception as exc:
            logger.warning("grid_load_failed", error=str(exc))
        return {}

    def _save_grid(self) -> None:
        try:
            if not self.calibrated_cells:
                if _GRID_FILE.exists():
                    _GRID_FILE.unlink(missing_ok=True)
                return
            payload = {
                "cells_by_monitor": {
                    str(key): [int(v) for v in value]
                    for key, value in self.calibrated_cells.items()
                }
            }
            _GRID_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self.add_log(f"Aviso: nao consegui salvar calibracao ({exc})", "WARN")

    def _refresh_calib_label(self) -> None:
        selected_label = self.monitor_choice_var.get() if hasattr(self, "monitor_choice_var") else "Todos os monitores"
        if not self.calibrated_cells:
            text = "Sem janela modelo confirmada."
            color = C_MUTED
        elif selected_label == "Todos os monitores":
            calibrated = []
            for index, monitor in enumerate(getattr(self, "desktop_monitors", [])):
                key = monitor_storage_key(monitor)
                if key in self.calibrated_cells:
                    cell = self.calibrated_cells[key]
                    calibrated.append(f"M{index + 1}:{cell[2]}x{cell[3]}")
            summary = ", ".join(calibrated) if calibrated else "nenhum monitor calibrado"
            text = f"Calibracoes salvas: {len(self.calibrated_cells)} monitor(es) | {summary}"
            color = C_ACCENT
        elif not self.calibrated_cell:
            text = f"Sem calibracao salva para {selected_label}."
            color = C_MUTED
        else:
            x, y, w, h = self.calibrated_cell
            monitor = detect_monitor_from_cell(self.calibrated_cell)
            cols = max(1, (monitor.width + max(0, int(self.window_gap_entry.get() or 6))) // max(1, w + max(0, int(self.window_gap_entry.get() or 6)))) if hasattr(self, "window_gap_entry") else max(1, monitor.width // max(1, w))
            rows = max(1, (monitor.height + max(0, int(self.window_gap_entry.get() or 6))) // max(1, h + max(0, int(self.window_gap_entry.get() or 6)))) if hasattr(self, "window_gap_entry") else max(1, monitor.height // max(1, h))
            text = f"Modelo: {w}x{h} @ ({x},{y}) | Monitor {monitor.width}x{monitor.height} -> {cols}x{rows} slots"
            color = C_ACCENT
        if hasattr(self, "example_hint_label"):
            self.example_hint_label.configure(text=text, text_color=color)
        if hasattr(self, "screens_status_label"):
            self.screens_status_label.configure(text=text, text_color=color)

    def _find_monitor_index(self, monitor: DesktopBounds) -> int:
        if not hasattr(self, "desktop_monitors"):
            return 0
        for idx, current in enumerate(self.desktop_monitors):
            if (current.x, current.y, current.width, current.height) == (monitor.x, monitor.y, monitor.width, monitor.height):
                return idx
        return 0

    def _monitor_choice_values(self) -> list[str]:
        return ["Todos os monitores", *[format_monitor_label(i, mon) for i, mon in enumerate(getattr(self, "desktop_monitors", []))]]

    def _sync_current_calibration_from_selection(self) -> None:
        choice = self.monitor_choice_var.get() if hasattr(self, "monitor_choice_var") else "Todos os monitores"
        if choice == "Todos os monitores":
            self.calibrated_cell = None
        else:
            labels = self._monitor_choice_values()[1:]
            try:
                index = labels.index(choice)
                monitor = getattr(self, "desktop_monitors", [])[index]
                self.calibrated_cell = self.calibrated_cells.get(monitor_storage_key(monitor))
            except Exception:
                self.calibrated_cell = None
        self.browser_runner.set_calibrated_cells(self.calibrated_cells)
        self.browser_runner.set_calibrated_cell(self.calibrated_cell)

    def _apply_monitor_bounds_to_entries(self, monitor: DesktopBounds, log_change: bool = True) -> None:
        for entry, value in (
            (self.origin_x_entry, monitor.x),
            (self.origin_y_entry, monitor.y),
            (self.area_width_entry, monitor.width),
            (self.area_height_entry, monitor.height),
        ):
            entry.delete(0, "end")
            entry.insert(0, str(value))
        if hasattr(self, "monitor_choice_var"):
            if hasattr(self, "all_monitors_bounds") and (monitor.x, monitor.y, monitor.width, monitor.height) == (
                self.all_monitors_bounds.x,
                self.all_monitors_bounds.y,
                self.all_monitors_bounds.width,
                self.all_monitors_bounds.height,
            ):
                self.monitor_choice_var.set("Todos os monitores")
            else:
                self.monitor_choice_var.set(format_monitor_label(self._find_monitor_index(monitor), monitor))
        if log_change:
            self.add_log(
                f"Monitor aplicado: {monitor.width}x{monitor.height} em ({monitor.x},{monitor.y}).",
                "INFO",
            )

    def _on_monitor_selected(self, choice: str) -> None:
        if not hasattr(self, "desktop_monitors"):
            return
        if choice == "Todos os monitores":
            monitor = self.all_monitors_bounds
        else:
            try:
                index = [format_monitor_label(i, mon) for i, mon in enumerate(self.desktop_monitors)].index(choice)
            except ValueError:
                index = 0
            monitor = self.desktop_monitors[index]
        self._apply_monitor_bounds_to_entries(monitor, log_change=True)
        self._sync_current_calibration_from_selection()
        self._refresh_calib_label()

    def _reload_monitor_list(self) -> None:
        self.desktop_monitors = list_desktop_monitors()
        self.all_monitors_bounds = detect_all_monitors_bounds(self.desktop_monitors)
        values = self._monitor_choice_values()
        if hasattr(self, "monitor_choice_menu"):
            self.monitor_choice_menu.configure(values=values)
        current_choice = self.monitor_choice_var.get() if hasattr(self, "monitor_choice_var") else "Todos os monitores"
        if current_choice not in values:
            current_choice = "Todos os monitores"
            if hasattr(self, "monitor_choice_var"):
                self.monitor_choice_var.set(current_choice)
        self._on_monitor_selected(current_choice)
        self.add_log(
            f"Monitores detectados: {len(self.desktop_monitors)} | Area combinada: {self.all_monitors_bounds.width}x{self.all_monitors_bounds.height} em ({self.all_monitors_bounds.x},{self.all_monitors_bounds.y})",
            "INFO",
        )

    def _apply_calibrated_cell_to_entries(self, log_change: bool = True) -> None:
        if not self.calibrated_cell:
            return
        x, y, width, height = self.calibrated_cell
        monitor = detect_monitor_from_cell(self.calibrated_cell)
        self._apply_monitor_bounds_to_entries(monitor, log_change=False)
        self.base_pause_entry.delete(0, "end")
        self.base_pause_entry.insert(0, "0")
        if log_change:
            self.add_log(f"Calibracao aplicada: janela modelo {width}x{height} em ({x},{y}) no monitor {monitor.width}x{monitor.height}.", "INFO")

    def _apply_example_window(self, payload: dict[str, int]) -> None:
        x = int(payload.get("x", 0))
        y = int(payload.get("y", 0))
        width = int(payload.get("width", 0))
        height = int(payload.get("height", 0))
        self.calibrated_cell = (x, y, width, height)
        monitor = detect_monitor_from_cell(self.calibrated_cell)
        self.calibrated_cells[monitor_storage_key(monitor)] = self.calibrated_cell
        self._save_grid()
        self.browser_runner.set_calibrated_cells(self.calibrated_cells)
        self.browser_runner.set_calibrated_cell(self.calibrated_cell)
        self._apply_calibrated_cell_to_entries(log_change=False)
        self._refresh_calib_label()
        if hasattr(self, "monitor_choice_var"):
            self.monitor_choice_var.set(format_monitor_label(self._find_monitor_index(monitor), monitor))
        self.add_log(f"Janela exemplo confirmada em x={x} y={y} w={width} h={height}. Monitor aplicado: {monitor.width}x{monitor.height}.", "INFO")

    def _labeled_entry(self, master, label: str, value: str, row: int):
        ctk.CTkLabel(master, text=label, text_color=C_MUTED, font=(FONT_MONO, 11)).grid(row=row, column=0, sticky="w", padx=8, pady=(8 if row == 0 else 4, 0))
        entry = ctk.CTkEntry(master, font=(FONT_MONO, 12))
        entry.grid(row=row, column=1, sticky="ew", padx=8, pady=(8 if row == 0 else 4, 0))
        entry.insert(0, value)
        master.grid_columnconfigure(1, weight=1)
        return entry

    # ---------- state helpers ----------
    def add_log(self, message: str, level: str = "INFO") -> None:
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] [{level:<5}] {message}\n"
        self.logs_box.insert("end", line)
        self.logs_box.see("end")
        try:
            with self.runtime_log_path.open("a", encoding="utf-8") as fp:
                fp.write(line)
        except Exception:
            pass

    def set_operation_status(self, message: str, color: str = C_MUTED) -> None:
        self.operation_label.configure(text=message, text_color=color)

    def get_selected_profile_ids(self) -> list[str]:
        return [row.profile.user_id for row in self._profile_rows if row.var.get()]

    def get_url_list(self) -> list[str]:
        return list(getattr(self, "master_urls_state", []))

    def get_child_url_list(self) -> list[str]:
        return list(getattr(self, "child_urls_state", []))

    def get_volume_settings(self) -> VolumeSettings:
        return VolumeSettings(
            display_mode=self.display_mode_var.get(),
            monitor_choice=self.monitor_choice_var.get() if hasattr(self, "monitor_choice_var") else "Todos os monitores",
            url_assignment_mode=self.assignment_var.get(),
            max_concurrent_profiles=int(float(self.concurrent_entry.get() or 2)),
            url_batch_size=int(float(self.batch_entry.get() or 8)),
            max_tabs_per_profile=int(float(self.limit_entry.get() or 24)),
            tab_open_delay_seconds=float(self.delay_entry.get() or 0.15),
            layout_columns=int(float(self.layout_cols_entry.get() or 0)),
            window_origin_x=int(float(self.origin_x_entry.get() or 0)),
            window_origin_y=int(float(self.origin_y_entry.get() or 0)),
            window_area_width=int(float(self.area_width_entry.get() or 0)),
            window_area_height=int(float(self.area_height_entry.get() or 0)),
            window_gap=int(float(self.window_gap_entry.get() or 6)),
            base_window_pause_seconds=float(self.base_pause_entry.get() or 5),
            grid_auto_reload_seconds=float(self.reload_entry.get() or 30.0),
            grid_columns=int(float(self.grid_cols_entry.get() or 0)),
            iframe_retry_limit=int(float(self.retry_entry.get() or 3)),
        )

    def get_active_script(self) -> ScriptRecord | None:
        return next((s for s in self._scripts if s.id == self._active_script_id), None)

    # ---------- scripts ----------
    def _sync_scripts_list(self) -> None:
        self.script_list.delete("1.0", "end")
        for script in self._scripts:
            marker = "*" if script.id == self._active_script_id else " "
            self.script_list.insert("end", f"{marker} {script.id} | {script.name}\n")
        active = self.get_active_script()
        self.script_editor.delete("1.0", "end")
        if active:
            self.script_editor.insert("1.0", active.code)

    def new_script(self) -> None:
        new_id = f"script_{uuid.uuid4().hex[:8]}"
        script = ScriptRecord(id=new_id, name=f"Novo Script {len(self._scripts)+1}", description="", code="// novo script\n", active=True)
        self._scripts = self.scripts_manager.upsert_script(script)
        self._active_script_id = script.id
        self._sync_scripts_list()

    def save_active_script(self) -> None:
        active = self.get_active_script()
        if not active:
            self.add_log("Nenhum script ativo.", "WARN")
            return
        code = self.script_editor.get("1.0", "end").strip()
        ok, error = self.scripts_manager.validate_script_code(code)
        if not ok:
            self.add_log(f"Erro de sintaxe no script: {error}", "ERROR")
            return
        updated = ScriptRecord(**{**model_dump_compat(active), "code": self.scripts_manager.normalize_script_code(code)})
        self._scripts = self.scripts_manager.upsert_script(updated)
        self._sync_scripts_list()
        self.add_log(f"Script salvo: {updated.name}", "INFO")

    def delete_active_script(self) -> None:
        active = self.get_active_script()
        if not active:
            return
        self._scripts = self.scripts_manager.delete_script(active.id)
        self._active_script_id = self._scripts[0].id if self._scripts else None
        self._sync_scripts_list()
        self.add_log(f"Script removido: {active.name}", "WARN")

    def activate_script_from_list(self) -> None:
        content = self.script_list.get("insert linestart", "insert lineend").strip()
        if not content:
            content = self.script_list.get("1.0", "2.0").strip()
        match = re.match(r"^[ *]\s*([^|\s]+)", content)
        if not match:
            self.add_log("Selecione uma linha da lista de scripts.", "WARN")
            return
        script_id = match.group(1).strip()
        if any(s.id == script_id for s in self._scripts):
            self._active_script_id = script_id
            self._sync_scripts_list()
            self.add_log(f"Script ativo: {script_id}", "INFO")

    # ---------- urls ----------
    def _render_url_cards(self, frame, items: list[str], kind: str) -> None:
        for child in frame.winfo_children():
            child.destroy()
        if not items:
            ctk.CTkLabel(frame, text="(vazio)", text_color=C_MUTED, font=(FONT_MONO, 11)).pack(anchor="w", padx=8, pady=6)
            return
        for index, value in enumerate(items):
            row = ctk.CTkFrame(frame, fg_color="#0f1822", border_width=1, border_color=C_BORDER)
            row.pack(fill="x", padx=6, pady=4)
            ctk.CTkLabel(row, text=value, text_color=C_TEXT, font=(FONT_MONO, 10), justify="left", wraplength=720).pack(side="left", fill="x", expand=True, padx=(8, 6), pady=6)
            ctk.CTkButton(
                row,
                text="X",
                width=28,
                fg_color=C_DANGER,
                command=lambda idx=index, current_kind=kind: self._remove_url_card(current_kind, idx),
            ).pack(side="right", padx=6, pady=6)

    def _render_master_urls(self) -> None:
        self._render_url_cards(self.urls_frame, getattr(self, "master_urls_state", []), "master")

    def _render_child_urls(self) -> None:
        self._render_url_cards(self.child_urls_frame, getattr(self, "child_urls_state", []), "child")

    def _render_completed_links(self) -> None:
        frame = self.completed_urls_frame
        for child in frame.winfo_children():
            child.destroy()
        items = list(getattr(self, "completed_links_state", []))
        if not items:
            ctk.CTkLabel(frame, text="(nenhuma casa feita salva ainda)", text_color=C_MUTED, font=(FONT_MONO, 11)).pack(anchor="w", padx=8, pady=6)
            return
        for value in items:
            row = ctk.CTkFrame(frame, fg_color="#0f1822", border_width=1, border_color=C_BORDER)
            row.pack(fill="x", padx=6, pady=4)
            ctk.CTkLabel(row, text=value, text_color="#88cfff", font=(FONT_MONO, 10), justify="left", wraplength=760).pack(side="left", fill="x", expand=True, padx=8, pady=6)

    def _mark_completed_link(self, value: str) -> None:
        value = str(value or "").strip()
        if not value:
            return
        history = getattr(self, "completed_links_state", [])
        if value not in history:
            history.append(value)
            self.completed_links_state = history[-300:]
            self._render_completed_links()
            self._persist_url_workspace_state()

    def _handle_captured_child_link(self, event: ChildLinkCapturedEvent) -> None:
        value = str(event.url or "").strip()
        if not value:
            self.add_log(f"[{event.domain}] Link da filha capturado vazio; ignorado.", "WARN")
            return
        if not value.startswith("http"):
            value = f"https://{value}"
        current = list(getattr(self, "child_urls_state", []))
        if value in current:
            self.add_log(f"[{event.domain}] Link da filha ja estava salvo em FILHAS: {value}", "INFO")
            return
        current.append(value)
        self.child_urls_state = current
        self._render_child_urls()
        self._persist_url_workspace_state()
        self.add_log(f"[{event.domain}] Link da filha salvo automaticamente em FILHAS: {value}", "INFO")

    def _handle_captured_account(self, event: AccountCapturedEvent) -> None:
        domain = str(event.domain or "?").strip() or "?"
        account = str(event.account or "").strip()
        phone = str(event.phone or "").strip()
        password = str(event.password or "").strip()
        url = str(event.url or "").strip()
        if not any([account, phone, password]):
            self.add_log(f"[{domain}] Conta capturada vazia; ignorada.", "WARN")
            return
        timestamp = time.strftime("%d/%m %H:%M")
        parts = [timestamp, domain]
        if account:
            parts.append(f"conta={account}")
        if phone:
            parts.append(f"celular={phone}")
        if password:
            parts.append(f"senha={password}")
        if url:
            parts.append(f"url={url}")
        entry = " | ".join(parts)

        current = [line.strip() for line in self.accounts_text.get("1.0", "end").splitlines() if line.strip()] if hasattr(self, "accounts_text") else list(self.workspace_data.accounts)
        fingerprint = f"{domain}|{account}|{phone}|{password}"
        for line in current:
            if fingerprint in line:
                self.add_log(f"[{domain}] Conta ja estava salva em CONTAS.", "INFO")
                return

        current.append(entry)
        if hasattr(self, "accounts_text"):
            self.accounts_text.delete("1.0", "end")
            self.accounts_text.insert("1.0", "\n".join(current))
        self.workspace_data.accounts = current[-500:]
        self.workspace_store.save(self.workspace_data)
        self.add_log(f"[{domain}] Conta salva automaticamente em CONTAS.", "INFO")

    def _remove_url_card(self, kind: str, index: int) -> None:
        if kind == "master":
            items = list(getattr(self, "master_urls_state", []))
        else:
            items = list(getattr(self, "child_urls_state", []))
        if not (0 <= index < len(items)):
            return
        removed = items.pop(index)
        if kind == "master":
            self.master_urls_state = items
            self._render_master_urls()
            self.add_log(f"MASTER removido: {removed}", "INFO")
        else:
            self.child_urls_state = items
            self._render_child_urls()
            self.add_log(f"FILHA removida: {removed}", "INFO")
        self._mark_completed_link(removed)
        self._persist_url_workspace_state()

    def add_url(self, raw_url: str) -> None:
        value = str(raw_url or "").strip()
        if not value:
            return
        if not value.startswith("http"):
            value = f"https://{value}"
        urls = list(getattr(self, "master_urls_state", []))
        if value not in urls:
            urls.append(value)
            self.master_urls_state = urls
            self._render_master_urls()
            self._persist_url_workspace_state()
            self.add_log(f"URL adicionada: {value}", "INFO")
        self.url_input.delete(0, "end")

    def import_bulk_urls(self) -> None:
        dialog = ctk.CTkToplevel(self)
        dialog.title("Importar URLs")
        dialog.geometry("560x360")
        dialog.configure(fg_color=C_BG)
        dialog.grab_set()
        ctk.CTkLabel(dialog, text="Cole uma URL por linha:", text_color=C_TEXT, font=(FONT_MONO, 12)).pack(anchor="w", padx=16, pady=(16, 6))
        box = ctk.CTkTextbox(dialog, font=(FONT_MONO, 11), fg_color="#08121a", border_color=C_BORDER, border_width=1)
        box.pack(fill="both", expand=True, padx=16, pady=(0, 10))

        def do_import() -> None:
            imported = 0
            existing = set(self.get_url_list())
            values = list(getattr(self, "master_urls_state", []))
            for line in box.get("1.0", "end").splitlines():
                current = line.strip()
                if not current:
                    continue
                if not current.startswith("http"):
                    current = f"https://{current}"
                if current in existing:
                    continue
                values.append(current)
                existing.add(current)
                imported += 1
            self.master_urls_state = values
            self._render_master_urls()
            self._persist_url_workspace_state()
            self.add_log(f"{imported} URL(s) importada(s).", "INFO")
            dialog.destroy()

        ctk.CTkButton(dialog, text="IMPORTAR", fg_color=C_ACCENT, text_color="#00140c", command=do_import).pack(fill="x", padx=16, pady=(0, 16))

    def add_child_url(self, raw_url: str) -> None:
        value = str(raw_url or "").strip()
        if not value:
            return
        if not value.startswith("http"):
            value = f"https://{value}"
        urls = list(getattr(self, "child_urls_state", []))
        if value not in urls:
            urls.append(value)
            self.child_urls_state = urls
            self._render_child_urls()
            self._persist_url_workspace_state()
            self.add_log(f"URL filha adicionada: {value}", "INFO")
        self.child_url_input.delete(0, "end")

    def clear_completed_links(self) -> None:
        if not getattr(self, "completed_links_state", []):
            self.add_log("Nenhuma casa feita para limpar.", "WARN")
            return
        self.completed_links_state = []
        self._render_completed_links()
        self._persist_url_workspace_state()
        self.add_log("Historico de casas feitas limpo.", "WARN")

    def import_bulk_child_urls(self) -> None:
        dialog = ctk.CTkToplevel(self)
        dialog.title("Importar URLs Filhas")
        dialog.geometry("560x360")
        dialog.configure(fg_color=C_BG)
        dialog.grab_set()
        ctk.CTkLabel(dialog, text="Cole uma URL filha por linha:", text_color=C_TEXT, font=(FONT_MONO, 12)).pack(anchor="w", padx=16, pady=(16, 6))
        box = ctk.CTkTextbox(dialog, font=(FONT_MONO, 11), fg_color="#08121a", border_color=C_BORDER, border_width=1)
        box.pack(fill="both", expand=True, padx=16, pady=(0, 10))

        def do_import() -> None:
            imported = 0
            existing = set(self.get_child_url_list())
            values = list(getattr(self, "child_urls_state", []))
            for line in box.get("1.0", "end").splitlines():
                current = line.strip()
                if not current:
                    continue
                if not current.startswith("http"):
                    current = f"https://{current}"
                if current in existing:
                    continue
                values.append(current)
                existing.add(current)
                imported += 1
            self.child_urls_state = values
            self._render_child_urls()
            self._persist_url_workspace_state()
            self.add_log(f"{imported} URL(s) filha(s) importada(s).", "INFO")
            dialog.destroy()

        ctk.CTkButton(dialog, text="IMPORTAR FILHAS", fg_color=C_ACCENT, text_color="#00140c", command=do_import).pack(fill="x", padx=16, pady=(0, 16))

    # ---------- relay / network / profiles ----------
    def _start_relay_with_fallback(self) -> bool:
        started = self.relay_service.start(port=self.relay_config.port)
        if started:
            return True
        retry_port = pick_available_port(preferred=self.relay_config.port + 1, host="0.0.0.0")
        if retry_port == self.relay_config.port:
            return False
        self.relay_config = RelayConfig(host=self.relay_config.host, port=retry_port)
        self._relay_port_auto = True
        self.port_label.configure(text=f"Relay: {self.relay_config.host}:{self.relay_config.port}")
        return self.relay_service.start(port=self.relay_config.port)

    def refresh_profiles(self) -> None:
        if self._bot_running:
            self.set_operation_status("Atualizando perfis em background...", C_MUTED)
        threading.Thread(target=self._refresh_profiles_worker, daemon=True).start()

    def _refresh_profiles_worker(self) -> None:
        self._enqueue_event(BotStatusEvent(state="profiles_loading", message="Carregando perfis AdsPower..."))
        try:
            profiles = self.browser_runner.adspower_client.list_profiles()
        except Exception as exc:
            self._enqueue_event(LogEvent(level="ERROR", message=f"AdsPower offline: {exc}"))
            self._enqueue_event(ProfilesLoadedEvent(profiles=[], reachable=False))
            return
        if profiles:
            self._enqueue_event(LogEvent(level="INFO", message=f"AdsPower OK - {len(profiles)} perfil(is) carregado(s)."))
        else:
            self._enqueue_event(LogEvent(level="WARN", message="Nenhum perfil encontrado."))
        self._enqueue_event(ProfilesLoadedEvent(profiles=profiles, reachable=True))

    def refresh_network_snapshot(self) -> None:
        adapters = detect_network_adapters()
        self._handle_event(NetworkSnapshotEvent(pc_adapters=adapters))

    def auto_detect_relay_ip(self) -> None:
        adapter = detect_primary_adapter(default_ip=self.relay_config.host)
        if adapter is not None:
            self.relay_ip_entry.delete(0, "end")
            self.relay_ip_entry.insert(0, adapter.ipv4)
            self.pc_network_label.configure(text=f"PC: {format_adapter_summary(adapter)}")
            self.add_log(f"Relay IPv4 detectado: {adapter.ipv4}", "INFO")
            return
        ip = detect_local_ipv4()
        self.relay_ip_entry.delete(0, "end")
        self.relay_ip_entry.insert(0, ip)
        self.add_log(f"Relay IPv4 detectado: {ip}", "INFO")

    def generate_mirror(self) -> None:
        cfg = self._build_relay_config()
        if not is_valid_relay_ipv4(cfg.host):
            self.add_log("Erro: Relay IPv4 invalido.", "ERROR")
            return
        threading.Thread(target=self.browser_runner.generate_mirror_extension, args=(cfg,), daemon=True).start()

    # ---------- start/stop ----------
    def _start_bot_common(self, start_mode: str = "all") -> None:
        if self._bot_running:
            self.add_log("Operacao ja em andamento.", "WARN")
            return
        self.save_workspace_preferences()
        selected_ids = set(self.get_selected_profile_ids())
        selected_profiles = [profile for profile in self._profiles if profile.user_id in selected_ids]
        if not selected_profiles:
            self.add_log("Erro: Selecione pelo menos um perfil.", "ERROR")
            return
        urls = list(self.get_url_list())
        child_urls = list(self.get_child_url_list())
        promoted_child_to_master = False
        if not urls and child_urls:
            promoted = child_urls.pop(0)
            urls = [promoted]
            promoted_child_to_master = True
            self.add_log(f"Sem link em MASTER: promovendo a primeira FILHA para MASTER -> {promoted}", "WARN")
        if start_mode in {"all", "masters"} and not urls:
            self.add_log("Erro: Adicione pelo menos uma URL em MASTER ou FILHAS.", "ERROR")
            return
        if start_mode == "masters":
            child_urls = []
            self.add_log("Modo START MASTERS: abrindo apenas links master; filhas foram ignoradas nesta rodada.", "INFO")
        elif start_mode == "children":
            if not urls:
                self.add_log("Erro: Adicione pelo menos uma URL na area LINKS FILHAS para eleger a MASTER.", "ERROR")
                return
            self.add_log("Modo START FILHAS: a primeira URL disponivel vira MASTER e as demais abrem como FILHAS.", "INFO")
        elif start_mode == "resume":
            if promoted_child_to_master:
                self.add_log("Modo RETOMAR: sem link em MASTER, a primeira FILHA foi promovida para MASTER e as demais abrirao como FILHAS.", "INFO")
            else:
                self.add_log("Modo RETOMAR: abrindo os links atuais sem refazer o fluxo de cadastro, preservando a ordem entre MASTER e FILHAS.", "INFO")
        if child_urls:
            self.add_log(f"{len(child_urls)} URL(s) da aba Filhas serao abertas sem forcar /registered e sempre como SLAVE.", "INFO")
        active_script = self.get_active_script()
        if active_script is None:
            self.add_log("Erro: Nenhum script ativo.", "ERROR")
            return
        runtime_workspace = WorkspaceData(**model_dump_compat(self.workspace_data))
        runtime_workspace.resume_only = start_mode == "resume"
        self.browser_runner.set_workspace_data(runtime_workspace)
        self.browser_runner.set_calibrated_cells(self.calibrated_cells)
        self.browser_runner.set_calibrated_cell(self.calibrated_cell)
        relay_config = self._build_relay_config()
        if not is_valid_relay_ipv4(relay_config.host):
            self.add_log("Erro: Relay IPv4 precisa ser um IP local valido (nao loopback).", "ERROR")
            return
        settings = self.get_volume_settings()
        settings.master_passthrough_first_url = promoted_child_to_master
        self._bot_running = True
        if start_mode == "masters":
            mode_label = "masters"
        elif start_mode == "children":
            mode_label = "filhas"
        elif start_mode == "resume":
            mode_label = "retomada"
        else:
            mode_label = "completo"
        self.set_operation_status(f"Iniciando {len(selected_profiles)} perfil(is) em modo {settings.url_assignment_mode}/{settings.display_mode} ({mode_label})...", C_ACCENT)
        threading.Thread(target=self.browser_runner.run_profiles, args=(selected_profiles, urls, child_urls, relay_config, active_script, settings), daemon=True).start()

    def start_bot(self) -> None:
        self._start_bot_common(start_mode="all")

    def start_masters_only(self) -> None:
        self._start_bot_common(start_mode="masters")

    def start_children_only(self) -> None:
        self._start_bot_common(start_mode="children")

    def start_resume_only(self) -> None:
        self._start_bot_common(start_mode="resume")

    def stop_bot(self) -> None:
        selected_ids = self.get_selected_profile_ids()
        active_ids = self.browser_runner.get_active_profile_ids()
        targets = selected_ids or active_ids
        if not targets:
            self.add_log("Erro: Nenhum perfil ativo/selecionado.", "ERROR")
            return
        threading.Thread(target=self.browser_runner.stop_profiles, args=(targets,), daemon=True).start()

    def _build_relay_config(self) -> RelayConfig:
        host = self.relay_ip_entry.get().strip() or detect_local_ipv4()
        self.relay_config = RelayConfig(host=host, port=self.relay_config.port)
        self.port_label.configure(text=f"Relay: {self.relay_config.host}:{self.relay_config.port}")
        return self.relay_config

    # ---------- events ----------
    def _enqueue_event(self, event: object) -> None:
        self._event_queue.put(event)

    def _process_event_queue(self) -> None:
        while True:
            try:
                event = self._event_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
        self.after(100, self._process_event_queue)

    def _handle_event(self, event: object) -> None:
        if isinstance(event, LogEvent):
            self.add_log(event.message, event.level)
            return
        if isinstance(event, BrowserDebugEvent):
            self.add_log(f"[{event.domain}] {event.message}", event.level)
            return
        if isinstance(event, ChildLinkCapturedEvent):
            self._handle_captured_child_link(event)
            return
        if isinstance(event, AccountCapturedEvent):
            self._handle_captured_account(event)
            return
        if isinstance(event, ProfilesLoadedEvent):
            self._apply_profiles_event(event)
            return
        if isinstance(event, NetworkSnapshotEvent):
            self._apply_network_snapshot(event)
            return
        if isinstance(event, MirrorGeneratedEvent):
            self.add_log("Mirror Extension gerada!", "INFO")
            self.add_log(f"Pasta: {event.path}", "INFO")
            self.add_log("AdsPower -> perfil -> Extensoes -> Personalizada -> Carregar sem compactacao", "INFO")
            return
        if isinstance(event, BotStatusEvent):
            self._apply_bot_status_event(event)
            return

    def _apply_profiles_event(self, event: ProfilesLoadedEvent) -> None:
        if event.reachable:
            self.status_label.configure(text=f"AdsPower: ONLINE - {len(event.profiles)} perfil(is)", text_color=C_TEXT)
        else:
            self.status_label.configure(text="AdsPower: OFFLINE", text_color=C_DANGER)
        previous = set(self.get_selected_profile_ids())
        self._profiles = list(event.profiles)
        for child in self.profile_scroll.winfo_children():
            child.destroy()
        self._profile_rows.clear()
        for profile in self._profiles:
            row = ProfileRow(self.profile_scroll, profile, selected=profile.user_id in previous)
            row.pack(fill="x", padx=4, pady=2)
            self._profile_rows.append(row)

    def _apply_network_snapshot(self, event: NetworkSnapshotEvent) -> None:
        self._last_network_snapshot = event
        adapter = event.pc_adapters[0] if event.pc_adapters else None
        self.pc_network_label.configure(text=f"PC: {format_adapter_summary(adapter)}")

    def _apply_bot_status_event(self, event: BotStatusEvent) -> None:
        if event.state == "profiles_loading":
            self.set_operation_status(event.message, C_MUTED)
            return
        if event.state == "running":
            self._bot_running = True
            self.set_operation_status(event.message or "Executando...", C_ACCENT)
            return
        if event.state == "completed":
            self._bot_running = False
            self.set_operation_status(event.message or "Concluido.", C_ACCENT)
            return
        if event.state == "error":
            self._bot_running = False
            self.set_operation_status(event.message or "Falha.", C_DANGER)
            return
        if event.state == "idle":
            self._bot_running = False
            self.set_operation_status("Pronto.", C_MUTED)
            return

    def _on_close(self) -> None:
        try:
            self.browser_runner.shutdown()
        except Exception:
            pass
        try:
            self.relay_service.stop()
        except Exception:
            pass
        self.destroy()


# ============================================================
# APP FACTORY / ENTRYPOINT
# ============================================================

def build_app() -> LTDFSingleFileApp:
    relay_config = RelayConfig(host=detect_local_ipv4(), port=pick_available_port(preferred=19876, host="0.0.0.0"))
    relay_service = RelayService()
    adspower_client = AdsPowerClient()
    browser_runner = BrowserRunner(adspower_client=adspower_client)
    relay_service.set_browser_runner(browser_runner)
    scripts_manager = ScriptsManager()
    return LTDFSingleFileApp(relay_service=relay_service, browser_runner=browser_runner, scripts_manager=scripts_manager, relay_config=relay_config)


def main() -> None:
    app = build_app()
    app.mainloop()


if __name__ == "__main__":
    main()




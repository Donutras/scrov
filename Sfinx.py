#!/usr/bin/env python3
# =============================================================================
# IMPLEMENTATION: Laptop sender using the NETWORK INTERFACE SPEC
# (Laptop ⇄ Raspberry Pi 4B over Cat6)
# =============================================================================
# This script:
#   • Reads Xbox Series X/S controller input via pygame
#   • Maps inputs to the agreed logical names (lx, ly, rx, ry, lt, rt, buttons, dpad)
#   • Sends NDJSON lines over a TCP CONTROL channel to the Raspberry Pi
#   • Emits heartbeats when idle
#   • (Optional) Opens a simple UDP/H.264 receive socket using OpenCV for video
#
# CONTROL (inputs) → TCP 55001  |  VIDEO (return) ← UDP 55002
# Messages are newline-delimited JSON with `type` fields ("input", "heartbeat").
# =============================================================================

"""
Requirements:
    pip install pygame rich
    # For optional video receive preview (window):
    pip install opencv-python

Usage:
    python read_xbox_controller_sender.py --host 192.168.50.20 --enable-control --video-udp :55002

Notes:
  - Works on Windows, macOS, and Linux (controller must be recognized by the OS).
  - On Windows you may need to install SDL2 runtimes which ship with pygame wheels.
  - Press Ctrl+C in terminal to quit.
"""

from __future__ import annotations
import sys
import time
import json
import socket
import argparse
from typing import Optional, Tuple
from datetime import datetime, timezone

# --- Third-party libs ---
try:
    import pygame
except ImportError:
    print("Missing dependency 'pygame'. Install with: pip install pygame")
    raise

try:
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.panel import Panel
    from rich import box
except ImportError:
    print("Missing dependency 'rich'. Install with: pip install rich")
    raise

# OpenCV is optional (only for local preview of incoming video)
try:
    import cv2  # type: ignore
    OPCV_AVAILABLE = True
except Exception:
    OPCV_AVAILABLE = False

console = Console()

# ======================== Configuration Defaults ============================
DEFAULT_HOST = "192.168.1.2"   # Raspberry Pi IP
DEFAULT_CTRL_PORT = 55001         # CONTROL TCP port (laptop → Pi)
DEFAULT_VIDEO_URL = None          # e.g. ":55002" to listen local, or "udp://@:55002"
POLL_HZ = 60.0
HEARTBEAT_MS = 500
DEADZONE = 0.04

# ============================ TCP Control Client ============================
class ControlClient:
    def __init__(self, host: str, port: int, timeout: float = 2.0):
        self.host, self.port, self.timeout = host, port, timeout
        self.sock: Optional[socket.socket] = None
        self.seq = 0

    def connect(self):
        try:
            s = socket.create_connection((self.host, self.port), self.timeout)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock = s
            console.print(f"[green]Connected CONTROL → {self.host}:{self.port}[/green]")
        except OSError as e:
            console.print(f"[red]CONTROL connect failed:[/red] {e}")
            self.sock = None

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None

    def next_seq(self) -> int:
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        return self.seq

    def send_json_line(self, obj: dict):
        if not self.sock:
            return
        try:
            line = json.dumps(obj, separators=(",", ":")) + "\n"
            self.sock.sendall(line.encode("utf-8"))
        except OSError as e:
            console.print(f"[red]CONTROL send failed:[/red] {e}")
            self.close()

# ============================ Video Receiver (Optional) =====================
class VideoReceiver:
    """Very simple UDP/H.264 receiver using OpenCV VideoCapture.
    Accepts either a raw port string like ":55002" or a full URL like
    "udp://@:55002?fifo_size=1000000&overrun_nonfatal=1".
    """
    def __init__(self, url: Optional[str]):
        self.cap = None
        self.url = url

    def start(self):
        if not self.url:
            return
        if not OPCV_AVAILABLE:
            console.print("[yellow]OpenCV not available; skipping video preview.[/yellow]")
            return
        url = self.url
        if url.startswith(":"):
            # Make a full URL from shorthand ":PORT"
            port = url[1:]
            url = f"udp://@:{port}?fifo_size=1000000&overrun_nonfatal=1"
        self.cap = cv2.VideoCapture(url)
        if not self.cap or not self.cap.isOpened():
            console.print(f"[red]Failed to open video source:[/red] {url}")
            self.cap = None
        else:
            console.print(f"[green]Video receive opened:[/green] {url}")

    def poll_frame(self):
        if self.cap is None:
            return
        ok, frame = self.cap.read()
        if ok:
            cv2.imshow("Pi → Laptop Video", frame)
            # A tiny wait keeps UI responsive; adjust if needed
            if cv2.waitKey(1) & 0xFF == ord('q'):
                self.stop()

    def stop(self):
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        if OPCV_AVAILABLE:
            try:
                cv2.destroyAllWindows()
            except Exception:
                pass

# ============================ Joystick Setup ================================

def init_joystick() -> pygame.joystick.Joystick:
    pygame.init()
    pygame.joystick.init()

    count = pygame.joystick.get_count()
    if count == 0:
        console.print("[red]No gamepads detected.[/red] Connect your Xbox controller and try again.")
        pygame.quit()
        sys.exit(1)

    js = pygame.joystick.Joystick(0)
    js.init()

    name = js.get_name()
    guid = getattr(js, "get_guid", lambda: "N/A")()
    axes = js.get_numaxes()
    buttons = js.get_numbuttons()
    hats = js.get_numhats()

    console.print(Panel.fit(
        f"[bold]Connected Controller[/bold]\n"
        f"Name: [cyan]{name}[/cyan]\nGUID: [cyan]{guid}[/cyan]\n"
        f"Axes: [cyan]{axes}[/cyan], Buttons: [cyan]{buttons}[/cyan], Hats: [cyan]{hats}[/cyan]",
        title="🎮 Xbox Controller Reader",
        border_style="blue"
    ))
    return js

# ============================ Input Reading =================================

def _axis(js: pygame.joystick.Joystick, idx: int, *, dead: float = DEADZONE, to01: bool = False) -> float:
    v = js.get_axis(idx)
    if abs(v) < dead:
        v = 0.0
    if to01:
        v = (v + 1.0) * 0.5  # map from [-1,1] to [0,1]
    v = max(-1.0, min(1.0, float(v)))
    return round(v, 3)


def read_sample(js: pygame.joystick.Joystick) -> dict:
    """Return a dict matching the CONTROL 'input' message in the spec."""
    pygame.event.pump()

    # Common pygame mappings for Xbox:
    # axes: 0=LX, 1=LY, 2=RX, 3=RY, 4=LT, 5=RT
    # buttons order often: A,B,X,Y, LB,RB, BACK, START, GUIDE(Xbox), LS,RS

    sample = {
        "type": "input",
        "ts": datetime.now(timezone.utc).isoformat(),
        "seq": 0,  # filled when sending
        "axes": {
            "lx": _axis(js, 0),
            "ly": _axis(js, 1),
            "rx": _axis(js, 2),
            "ry": _axis(js, 3),
            "lt": _axis(js, 4, to01=True),
            "rt": _axis(js, 5, to01=True),
        },
        "buttons": {},
        "dpad": {"x": 0, "y": 0},
        "meta": {"battery": None, "connected": True},
    }

    btn_names = ["a","b","x","y","lb","rb","back","start","xbox","ls","rs"]
    nbtn = js.get_numbuttons()
    for i, name in enumerate(btn_names):
        if i < nbtn:
            sample["buttons"][name] = int(js.get_button(i))

    if js.get_numhats() > 0:
        hx, hy = js.get_hat(0)
        sample["dpad"]["x"], sample["dpad"]["y"] = int(hx), int(hy)

    return sample

# ============================ Table Rendering ===============================

def build_table(sample: dict) -> Table:
    table = Table(title="Xbox Controller — Live Input", box=box.SIMPLE_HEAVY)
    table.add_column("Category", style="bold magenta", no_wrap=True)
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("Value", justify="right")

    for name, val in sample.get("axes", {}).items():
        table.add_row("Axis", name, str(val))

    for name, val in sample.get("buttons", {}).items():
        table.add_row("Button", name, str(val))

    d = sample.get("dpad", {})
    table.add_row("D‑pad", "x", str(d.get("x", 0)))
    table.add_row("D‑pad", "y", str(d.get("y", 0)))

    return table

# ============================ Heartbeat =====================================

def heartbeat_message(seq: int) -> dict:
    return {"type": "heartbeat", "ts": datetime.now(timezone.utc).isoformat(), "seq": seq}

# ============================ Main Loop =====================================

def run(host: str, port: int, enable_control: bool, video_url: Optional[str]):
    js = init_joystick()
    delay = 1.0 / POLL_HZ

    control = ControlClient(host, port)
    last_send = 0.0
    last_any = time.perf_counter()

    if enable_control:
        control.connect()

    vr = VideoReceiver(video_url)
    vr.start()

    with Live(refresh_per_second=int(POLL_HZ), console=console, transient=False) as live:
        try:
            while True:
                sample = read_sample(js)
                table = build_table(sample)
                live.update(table)

                now = time.perf_counter()
                # Send sample at most POLL_HZ
                if enable_control and control.sock and (now - last_send) >= (1.0 / POLL_HZ):
                    sample["seq"] = control.next_seq()
                    control.send_json_line(sample)
                    last_send = now
                    last_any = now
                else:
                    # Heartbeat if idle for HEARTBEAT_MS
                    if enable_control and control.sock and (now - last_any) * 1000.0 >= HEARTBEAT_MS:
                        hb = heartbeat_message(control.next_seq())
                        control.send_json_line(hb)
                        last_any = now

                # Poll video preview if enabled
                vr.poll_frame()

                time.sleep(delay)
        except KeyboardInterrupt:
            pass
        finally:
            vr.stop()
            control.close()
            js.quit()
            pygame.joystick.quit()
            pygame.quit()
            console.print("\n[bold green]Goodbye![/bold green]")

# ============================ CLI ===========================================

def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Xbox controller sender (NDJSON over TCP) + optional UDP video preview")
    p.add_argument("--host", default=DEFAULT_HOST, help="Raspberry Pi host/IP for CONTROL")
    p.add_argument("--port", type=int, default=DEFAULT_CTRL_PORT, help="CONTROL TCP port (default 55001)")
    p.add_argument("--enable-control", action="store_true", help="Enable TCP sender to the Pi")
    p.add_argument("--video-udp", default=DEFAULT_VIDEO_URL, help="Open a local video preview from UDP source (e.g. :55002 or full udp:// URL)")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    run(args.host, args.port, args.enable_control, args.video_udp)

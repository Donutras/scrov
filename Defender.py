#!/usr/bin/env python3
# =============================================================================
# Raspberry Pi 4B Receiver — CONTROL + VIDEO Spec
# =============================================================================
"""
This script is meant for the Raspberry Pi side when connected via Ethernet (Cat6)
to the laptop running the Xbox Controller Sender.

Connection Overview:
  - CONTROL channel: TCP server on Pi, listens for NDJSON controller messages from the laptop.
  - VIDEO channel: Pi sends H.264 RTP/UDP video stream to laptop.

Steps to Connect:
  1. Assign static IPs or ensure both systems are on same subnet.
     Example:
       Laptop:      192.168.50.10
       RaspberryPi: 192.168.50.20

  2. On Pi, run this receiver script.
     - Listens on TCP port 55001 for CONTROL.
     - Parses NDJSON messages of type "input" or "heartbeat".
     - Optional: sends ACKs if needed.

  3. Start video stream from Pi to Laptop.
     - Use GStreamer to capture from camera and encode in H.264:
       gst-launch-1.0 v4l2src ! videoconvert ! \
         x264enc tune=zerolatency bitrate=4000 speed-preset=ultrafast ! \
         rtph264pay config-interval=1 pt=96 ! \
         udpsink host=192.168.50.10 port=55002

  4. Laptop will preview this video via OpenCV or GStreamer pipeline.

CONTROL Message Format (from laptop):
  {
    "type": "input",        # or "heartbeat"
    "ts":   ISO-8601 UTC timestamp,
    "seq":  integer sequence number,
    "axes": {"lx":..., "ly":..., "rx":..., "ry":..., "lt":..., "rt":...},
    "buttons": {"a":0|1, ...},
    "dpad": {"x":-1|0|1, "y":-1|0|1},
    "meta": {...}
  }

Processing on Pi:
  - For "input" messages: handle control logic for robot/vehicle/actuator.
  - For "heartbeat": maintain connection status.
  - Implement reconnect/timeout detection.

Security:
  - Optional PSK + HMAC per message or trusted LAN.
"""

# =============================================================================
# IMPLEMENTATION: Pi CONTROL receiver (prints inputs)
# =============================================================================
# Listens on TCP port 55001 for NDJSON messages from the laptop sender.
# For each message:
#   • type == "input": prints a compact, readable line with axes, dpad, and
#     currently pressed buttons.
#   • type == "heartbeat": (optional) prints a heartbeat notice.
#
# Optional: send NDJSON ACKs back with {"type":"ack","seq":<seq>}.
#
# Usage (on the Pi):
#   python pi_receiver_print_inputs.py --bind 0.0.0.0 --port 55001
#   # show heartbeats too:
#   python pi_receiver_print_inputs.py --show-heartbeats
#   # enable ACKs back to the laptop:
#   python pi_receiver_print_inputs.py --ack
# =============================================================================

import argparse
import json
import socket
import sys
import time

def fmt_float(val, places=3):
    try:
        return f"{float(val):.{places}f}"
    except Exception:
        return "na"

def pressed_buttons(btns):
    if not isinstance(btns, dict):
        return "-"
    return ",".join([k for k, v in btns.items() if v]) or "-"

def build_line(msg):
    mtype = msg.get("type", "?")
    ts    = msg.get("ts", "")
    seq   = msg.get("seq", "")
    axes  = msg.get("axes", {}) or {}
    dpad  = msg.get("dpad", {}) or {}

    lx = fmt_float(axes.get("lx"))
    ly = fmt_float(axes.get("ly"))
    rx = fmt_float(axes.get("rx"))
    ry = fmt_float(axes.get("ry"))
    lt = fmt_float(axes.get("lt"))
    rt = fmt_float(axes.get("rt"))

    dx = dpad.get("x", 0)
    dy = dpad.get("y", 0)

    btns = pressed_buttons(msg.get("buttons", {}))

    return (
        f"[{mtype}] seq={seq} ts={ts}  "
        f"axes(lx={lx} ly={ly}  rx={rx} ry={ry}  lt={lt} rt={rt})  "
        f"dpad(x={dx} y={dy})  "
        f"pressed=[{btns}]"
    )

def recv_lines(sock):
    """Yield complete lines (decoded UTF-8) from a socket, handling partial frames."""
    buf = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
        while True:
            nl = buf.find(b"\n")
            if nl == -1:
                break
            line = buf[:nl]
            del buf[:nl+1]
            try:
                yield line.decode("utf-8", errors="replace").strip()
            except Exception:
                continue

def handle_client(conn, addr, show_heartbeats=False, send_ack=False):
    peer = f"{addr[0]}:{addr[1]}"
    print(f"Client connected: {peer}")
    with conn:
        for line in recv_lines(conn):
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                print(f"(warn) bad json from {peer}: {line[:120]}...")
                continue

            mtype = msg.get("type")
            if mtype == "input":
                print(build_line(msg))
                if send_ack:
                    try:
                        ack = {"type": "ack", "seq": msg.get("seq")}
                        conn.sendall((json.dumps(ack) + "\n").encode("utf-8"))
                    except Exception as e:
                        print(f"(warn) failed to send ack: {e}")
            elif mtype == "heartbeat":
                if show_heartbeats:
                    ts = msg.get("ts", "")
                    seq = msg.get("seq", "")
                    print(f"[heartbeat] seq={seq} ts={ts}")
            else:
                print(f"[unknown] {line}")

    print(f"Client disconnected: {peer}")

def serve(bind, port, show_heartbeats=False, send_ack=False):
    print(f"Listening on {bind}:{port} (CTRL+C to stop)")
    while True:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((bind, port))
                s.listen(1)
                conn, addr = s.accept()
                handle_client(conn, addr, show_heartbeats, send_ack)
        except KeyboardInterrupt:
            print("\nShutting down.")
            return
        except Exception as e:
            print(f"(error) server exception: {e}")
            time.sleep(0.5)

def main():
    ap = argparse.ArgumentParser(description="Raspberry Pi CONTROL receiver: print controller inputs.")
    ap.add_argument("--bind", default="0.0.0.0", help="Interface to bind on (default: 0.0.0.0)")
    ap.add_argument("--port", type=int, default=55001, help="TCP port (default: 55001)")
    ap.add_argument("--show-heartbeats", action="store_true", help="Also print heartbeat messages")
    ap.add_argument("--ack", action="store_true", help="Send NDJSON ACKs back with the same seq")
    args = ap.parse_args()

    serve(args.bind, args.port, show_heartbeats=args.show_heartbeats, send_ack=args.ack)

if __name__ == "__main__":
    main()

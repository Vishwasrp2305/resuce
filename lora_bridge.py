"""
lora_bridge.py — Nano (LoRa receiver) -> rescue dashboard bridge.

*** Only needed if the Nano is plugged into a DIFFERENT computer than
*** the one running `python app.py`. If they're the same machine, you
*** don't need this script at all — app.py already has a built-in
*** LoRa receiver (see LORA_PORT / LORA_SERIAL_PORT near the top of
*** app.py) that reads the Nano directly, prints the same GPS/Map/RSSI
*** details to that terminal, and files the alert. Running both this
*** script AND app.py's built-in receiver against the SAME COM port at
*** the same time will fail — only one program can hold a serial port
*** open — and would double-file every alert if it somehow didn't.

Run this on whichever OTHER computer the Arduino Nano
(hardware/nano_receiver/nano_receiver.ino) is plugged into over USB
when that's not the dashboard server itself. It watches the Nano's
serial output for the "$SOS,<lat>,<lon>,<rssi>" line the Nano prints
whenever it receives an SOS packet over LoRa from the ESP32
(hardware/esp32_sender/esp32_sender.ino), and POSTs it to the
dashboard's /api/sos endpoint — the same endpoint the ESP32 itself
posts to directly when it has Wi-Fi.

This is what makes the LoRa path real-time too: even when the victim's
ESP32 has no Wi-Fi/internet at all, the alert still reaches the
dashboard (and blinks red on both /user and /admin) via LoRa -> Nano ->
this bridge -> the dashboard's HTTP API — and DASHBOARD_URL can point
at a server on a completely different computer from this one, so the
"admin's Nano" and "the dashboard" and "the user's browser" can all be
on three different machines as long as they're all on the same network
(or the dashboard is reachable over the internet, e.g. via Tailscale).

Usage:
    pip install -r bridge_requirements.txt
    python lora_bridge.py

Configure with environment variables (all optional):
    SERIAL_PORT     e.g. COM30 (Windows) or /dev/ttyUSB0 (Linux/Mac).
                    If unset, the script lists available ports and
                    asks you to pick one.
    BAUD_RATE       default 115200 (must match the Nano sketch).
    DASHBOARD_URL   default http://127.0.0.1:5000 — set this to
                    wherever `python app.py` is actually running,
                    e.g. http://192.168.1.50:5000 if the dashboard
                    is on a different computer than this bridge.
    DEVICE_ID       default NANO-LORA-BRIDGE — shown in the admin
                    alert feed so responders can tell an alert came
                    in over the LoRa path rather than straight from
                    the ESP32's own Wi-Fi.
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("Missing dependency: pyserial.")
    print("Install it with:  pip install -r bridge_requirements.txt")
    sys.exit(1)

SERIAL_PORT = os.environ.get("SERIAL_PORT", "")
BAUD_RATE = int(os.environ.get("BAUD_RATE", "115200"))
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://127.0.0.1:5000").rstrip("/")
DEVICE_ID = os.environ.get("DEVICE_ID", "NANO-LORA-BRIDGE")
# Hardcoded fallback identity for this device, used whenever no "USER:"
# line comes through in the packet — kept in sync with app.py's
# DEFAULT_REPORTER_LABEL so the reporter shows up the same way on the
# admin feed/map regardless of which path (same-PC or different-PC)
# relayed the alert.
DEFAULT_REPORTER_LABEL = os.environ.get("DEFAULT_REPORTER_LABEL", "VISHWAS (ID 1236)")

# Ignore a repeat of the exact same coordinates within this many seconds,
# in case a LoRa packet gets processed twice — one SOS button press
# should mean one alert, not a flood of them.
DEDUPE_SECONDS = 15
# How long to wait before retrying a serial connection that dropped
# (Nano unplugged/replugged, wrong port temporarily, etc).
RECONNECT_DELAY_SECONDS = 3

# Matches a "lat,lon" pair like "12.873183,74.851145" wherever it
# appears in a line — fallback for a Nano sketch that doesn't print the
# exact "$SOS,<lat>,<lon>,<rssi>" line (e.g. a custom sketch with its
# own banner text), so coordinates can still be pulled straight out of
# a "GPS: ..." or "MAP: https://maps.google.com/?q=..." line. Kept in
# sync with the same fallback in app.py's LoRaReceiver.
LATLON_RE = re.compile(r'(-?\d{1,3}\.\d{3,}),\s*(-?\d{1,3}\.\d{3,})')
# Matches a "USER: <name>,<id>" line, if the ESP32 firmware sends one,
# so a fixed reporter identity (e.g. "VISHWAS,1236") reaches the admin
# feed/map instead of a random "Guest-XXXX" label — same as app.py.
USER_RE = re.compile(r'USER:\s*([^,]+),\s*(\S+)', re.IGNORECASE)

_last_user_label = None  # most recent "USER: name,id" line seen


def pick_serial_port():
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found. Plug in the Nano and try again.")
        sys.exit(1)
    if len(ports) == 1:
        print(f"Using the only serial port found: {ports[0].device}")
        return ports[0].device
    print("Multiple serial ports found — set SERIAL_PORT to pick one non-interactively next time:")
    for i, p in enumerate(ports):
        print(f"  [{i}] {p.device}  {p.description}")
    while True:
        choice = input("Pick a port number: ").strip()
        if choice.isdigit() and int(choice) < len(ports):
            return ports[int(choice)].device
        print("Invalid choice, try again.")


def post_sos(lat, lon, guest_label=None):
    body = json.dumps(
        {
            "lat": lat,
            "lon": lon,
            "device_id": DEVICE_ID,
            "message": "SOS button pressed (relayed over LoRa)",
            "guest_label": guest_label,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        f"{DASHBOARD_URL}/api/sos",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"  -> dashboard responded {resp.status}: {resp.read().decode('utf-8', 'replace')[:200]}")
            return True
    except urllib.error.URLError as e:
        print(f"  -> FAILED to reach dashboard at {DASHBOARD_URL}: {e}")
        print("     Check DASHBOARD_URL and that `python app.py` is running and reachable from this computer.")
        return False


def parse_sos_line(line):
    """Parses "$SOS,<lat>,<lon>,<rssi>" -> (lat, lon) floats, or None if
    it's a no-GPS packet ($SOS,NOGPS,NOGPS,<rssi>) or malformed."""
    parts = line.strip().split(",")
    if len(parts) < 3 or parts[0] != "$SOS":
        return None
    lat_str, lon_str = parts[1], parts[2]
    if lat_str == "NOGPS" or lon_str == "NOGPS":
        print("  Nano reported an SOS with no GPS fix — can't dispatch")
        print("  without coordinates, so this one isn't posted to the")
        print("  dashboard. (It's still on the Nano's serial log above.)")
        return None
    try:
        return float(lat_str), float(lon_str)
    except ValueError:
        return None


def run():
    global _last_user_label
    port = SERIAL_PORT or pick_serial_port()
    print(f"rescue_dashboard LoRa bridge — port={port} baud={BAUD_RATE} -> {DASHBOARD_URL}/api/sos")
    print("Waiting for SOS packets relayed from the Nano...\n")

    last_sent = {}  # (lat, lon) -> last-sent unix time, for dedupe

    while True:
        try:
            with serial.Serial(port, BAUD_RATE, timeout=1) as ser:
                print(f"Connected to {port}.")
                while True:
                    raw = ser.readline()
                    if not raw:
                        continue
                    try:
                        line = raw.decode("utf-8", errors="replace").strip()
                    except Exception:
                        continue
                    if not line:
                        continue

                    # Echo everything for visibility (this also shows the
                    # Nano's normal human-readable log, whatever its exact
                    # banner text looks like).
                    print(line)

                    user_match = USER_RE.search(line)
                    if user_match:
                        name, uid = user_match.group(1).strip(), user_match.group(2).strip()
                        if name:
                            _last_user_label = f"{name} (ID {uid})" if uid else name

                    if line.startswith("$SOS,"):
                        parsed = parse_sos_line(line)
                        if parsed is None:
                            continue
                        lat, lon = parsed
                    else:
                        # Fallback: this Nano isn't printing the exact
                        # "$SOS," line, but the line may still contain
                        # raw coordinates (e.g. a "GPS: ..." or
                        # "MAP: https://maps.google.com/?q=..." line) —
                        # try to pull them out directly, same as app.py's
                        # built-in receiver does for the same-PC path.
                        if not ("SOS" in line.upper() or "GPS" in line.upper() or "MAP" in line.upper()):
                            continue
                        match = LATLON_RE.search(line)
                        if not match:
                            continue
                        try:
                            lat, lon = float(match.group(1)), float(match.group(2))
                        except ValueError:
                            continue
                        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                            continue

                    now = time.time()
                    key = (round(lat, 5), round(lon, 5))
                    if now - last_sent.get(key, 0) < DEDUPE_SECONDS:
                        print("  (duplicate within dedupe window, skipping repost)")
                        continue
                    last_sent[key] = now

                    label = _last_user_label or DEFAULT_REPORTER_LABEL
                    print(f"  SOS relayed via LoRa: lat={lat:.6f} lon={lon:.6f} "
                          f"reporter={label} -> posting to dashboard...")
                    post_sos(lat, lon, guest_label=label)
                    _last_user_label = None  # don't leak into an unrelated future packet

        except serial.SerialException as e:
            print(f"Serial error ({e}). Retrying in {RECONNECT_DELAY_SECONDS}s...")
            time.sleep(RECONNECT_DELAY_SECONDS)
        except KeyboardInterrupt:
            print("\nStopped.")
            return


if __name__ == "__main__":
    run()

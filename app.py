
"""
Main Flask Application with LoRa Receiver Integration
Receives SOS messages from Arduino Nano and displays them in terminal
"""

import os
import csv
import re
import time
import sqlite3
import math
import json
import random
import threading
import urllib.request
import urllib.error
import urllib.parse
import serial
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, g, jsonify, render_template, request
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "rescue.db")

# Bumped once per process start so <script>/<link> tags get a fresh
# ?v=... query string on every deploy — without this, browsers can keep
# running an old cached copy of dashboard.js after an update ships, which
# looks exactly like "the new feature isn't working".
STATIC_VERSION = str(int(time.time()))

# Change this before any real deployment — set the ADMIN_TOKEN environment
# variable instead of relying on the default.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "admin123")

# Optional: a free CARTO API key (carto.com/basemaps/apikey, no card needed,
# 5M tile requests/month free) switches the map to CARTO's Voyager basemap,
# which always labels places in Latin/English script. Without a key we fall
# back to plain tile.openstreetmap.org tiles, which render whatever script
# is in OSM's raw "name" tag for each feature — for big bodies of water
# like the Arabian Sea that's sometimes Arabic, which is expected upstream
# OSM behavior, not a bug in this app. See static/dashboard.js.
CARTO_API_KEY = os.environ.get("CARTO_API_KEY", "")

# ============================================================
# LORA RECEIVER CONFIGURATION
# ------------------------------------------------------------
# This is the ONLY place the Nano's COM port is set. Either edit the
# "COM30" default below, or (better, so you never have to touch code)
# set the LORA_PORT environment variable before starting the app, e.g.:
#   Windows :  set LORA_PORT=COM30 && python app.py
#   Linux/Mac: LORA_PORT=/dev/ttyUSB0 python app.py
#
# lora_bridge.py is a SEPARATE, OPTIONAL script — only needed if the
# Nano is plugged into a different computer than the one running this
# app.py. If the Nano is plugged into this same machine, you don't need
# lora_bridge.py at all: the LoRaReceiver below already reads the
# Nano's serial output directly, prints every SOS (with GPS + Google
# Maps link) to this terminal, and files the alert so it's pinned on
# the admin map. Never point both this app and lora_bridge.py at the
# same COM port at the same time — only one program can hold a serial
# port open, and you'd risk double-filing the same alert.
# ============================================================
LORA_SERIAL_PORT = os.environ.get("LORA_PORT", "COM30")
LORA_BAUDRATE = 115200
lora_receiver = None
lora_thread = None

app = Flask(__name__)


@app.context_processor
def inject_static_version():
    return {"static_version": STATIC_VERSION, "carto_api_key": CARTO_API_KEY}


# ---------------------------------------------------------------------------
# AI satisfaction review — a RandomForestClassifier trained (in Colab) on
# final_updated_data.csv (400 historical incidents: where it happened, and
# whether hospital/police/ambulance were assigned) predicting whether the
# person would report being satisfied with the response. Loaded once, lazily,
# so a missing/incompatible model file never crashes the app — it just makes
# ai_review show up as unavailable.
# ---------------------------------------------------------------------------
MODEL_PATH = os.path.join(BASE_DIR, "random_forest_model.pkl")
TRAINING_DATA_PATH = os.path.join(BASE_DIR, "final_updated_data.csv")
LIVE_LOG_PATH = os.path.join(BASE_DIR, "live_predictions_log.csv")

# One-hot column order the model was trained on. Verified empirically by
# inspecting the fitted trees' split feature-indices and thresholds (e.g.
# a threshold of ~74.73 only makes sense as a longitude split, ~13.3 only
# as a latitude split, ~14 as an "area" split, and 0.5 thresholds as the
# three binary *_assigned flags) — the place one-hot columns come FIRST,
# followed by latitude, longitude, area, hospital_assigned,
# police_station_assigned, ambulance_assigned in that order. NOTE: the
# training data spells one place "Moodabidri" while the live regions
# table spells it "Moodbidri" — REGION_TO_MODEL_PLACE bridges that.
MODEL_PLACES = ["Mangalore", "Moodabidri", "Padubidri", "Udupi"]
MODEL_FEATURES = (
    [f"place_{p}" for p in MODEL_PLACES]
    + ["latitude", "longitude", "area", "hospital_assigned", "police_station_assigned", "ambulance_assigned"]
)
REGION_TO_MODEL_PLACE = {
    "Mangalore": "Mangalore",
    "Padubidri": "Padubidri",
    "Udupi": "Udupi",
    "Moodbidri": "Moodabidri",
}

_satisfaction_model = None
_model_load_attempted = False


def get_satisfaction_model():
    """Lazily loads the trained model on first use. joblib is required to
    read a scikit-learn pickle reliably; if it (or the file) is missing,
    every caller just treats the model as unavailable."""
    global _satisfaction_model, _model_load_attempted
    if not _model_load_attempted:
        _model_load_attempted = True
        try:
            import joblib
            _satisfaction_model = joblib.load(MODEL_PATH)
        except Exception:
            _satisfaction_model = None
    return _satisfaction_model


def predict_satisfaction(lat, lon, region_name, hospital_assigned, police_assigned, ambulance_assigned, area_km):
    """Returns {"prediction": 0|1, "label": "Yes"|"No", "confidence": float}
    or None if the model isn't available."""
    model = get_satisfaction_model()
    if model is None:
        return None
    place = REGION_TO_MODEL_PLACE.get(region_name)
    row = [1 if place == p else 0 for p in MODEL_PLACES]
    row += [
        lat, lon, area_km,
        1 if hospital_assigned else 0,
        1 if police_assigned else 0,
        1 if ambulance_assigned else 0,
    ]
    try:
        pred = int(model.predict([row])[0])
        confidence = None
        if hasattr(model, "predict_proba"):
            confidence = round(float(max(model.predict_proba([row])[0])), 3)
        return {"prediction": pred, "label": "Yes" if pred == 1 else "No", "confidence": confidence}
    except Exception:
        return None


LIVE_LOG_FIELDS = [
    "timestamp", "alert_id", "latitude", "longitude", "place", "area",
    "hospital_assigned", "police_station_assigned", "ambulance_assigned",
    "predicted_satisfaction", "confidence",
]


def log_prediction_to_csv(alert_id, lat, lon, region_name, area_km,
                           hospital_assigned, police_assigned, ambulance_assigned, result):
    """Appends the live incident + its AI prediction to its own log file
    (kept separate from final_updated_data.csv, the original Colab training
    set, so production traffic never mutates the data the model was
    trained on). Best-effort — a logging failure must never break the
    approval flow the person is waiting on."""
    try:
        file_exists = os.path.exists(LIVE_LOG_PATH)
        with open(LIVE_LOG_PATH, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=LIVE_LOG_FIELDS)
            if not file_exists:
                writer.writeheader()
            writer.writerow({
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "alert_id": alert_id,
                "latitude": lat,
                "longitude": lon,
                "place": region_name or "",
                "area": area_km,
                "hospital_assigned": 1 if hospital_assigned else 0,
                "police_station_assigned": 1 if police_assigned else 0,
                "ambulance_assigned": 1 if ambulance_assigned else 0,
                "predicted_satisfaction": result["prediction"] if result else "",
                "confidence": result["confidence"] if result else "",
            })
    except Exception:
        pass


# ---------------------------------------------------------------------------
# LORA RECEIVER CLASS
# ---------------------------------------------------------------------------
class LoRaReceiver:
    """Background thread that reads the Arduino Nano's serial output.

    The Nano (hardware/nano_receiver/nano_receiver.ino) prints a
    human-readable block for every LoRa packet it gets from the ESP32
    (a "MESSAGE RECEIVED" banner, the raw message text — which includes
    "GPS: <lat>,<lon>" and "MAP: https://..." lines for an SOS — and
    "RSSI: <n> dBm"), followed by one clean machine-readable line:
        $SOS,<lat>,<lon>,<rssi>      (GPS fix)
        $SOS,NOGPS,NOGPS,<rssi>      (no GPS fix)

    This class echoes every line to the terminal as it arrives (so you
    see the Nano's own GPS/Map/RSSI details live), and whenever a
    "$SOS,..." line comes through with real coordinates, it pinpoints
    them clearly in the terminal and files an alert the same way the
    ESP32's own Wi-Fi POST to /api/sos would — so it shows up pinned on
    the admin map with a dispatch, no separate script required.
    """

    DEDUPE_SECONDS = 15  # ignore a repeat of the same fix within this window

    # Matches a "lat,lon" pair like "12.873183,74.851145" or "-12.9, -74.8"
    # wherever it appears in a line — used as a fallback for Nano sketches
    # that don't print the exact "$SOS,<lat>,<lon>,<rssi>" line (e.g. a
    # custom/older nano_receiver sketch with its own banner text), so we
    # can still pull the coordinates straight out of a "GPS: ..." or
    # "MAP: https://maps.google.com/?q=..." line instead of dropping it.
    LATLON_RE = re.compile(r'(-?\d{1,3}\.\d{3,}),\s*(-?\d{1,3}\.\d{3,})')
    RSSI_RE = re.compile(r'RSSI:\s*(-?\d+)', re.IGNORECASE)
    # Matches a "USER: <name>,<id>" line — e.g. "USER: VISHWAS,1236" —
    # if this specific ESP32's firmware sends one as part of its LoRa
    # packet, so a fixed identity shows up on the admin feed/map instead
    # of a random "Guest-XXXX" label.
    USER_RE = re.compile(r'USER:\s*([^,]+),\s*(\S+)', re.IGNORECASE)

    # Hardcoded fallback identity for this device. Getting a "USER:"
    # line to survive ESP32 -> LoRa -> a custom Nano sketch -> here
    # intact isn't always reliable, so every LoRa-relayed alert is
    # attributed to this name/ID by default; a "USER:" line, if one
    # does come through intact, simply overrides it for that alert.
    DEFAULT_REPORTER_LABEL = "VISHWAS (ID 1236)"

    def __init__(self, port, baudrate=115200):
        self.port = port
        self.baudrate = baudrate
        self.serial = None
        self.running = False
        self.message_count = 0
        self.sos_count = 0
        self._last_sent = {}  # (lat, lon) rounded -> last-sent unix time
        self._last_rssi = "?"  # most recent RSSI line seen, for the fallback path
        self._last_user_label = None  # most recent "USER: name,id" line seen

    def connect(self):
        """Connect to the serial port"""
        try:
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=1,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                bytesize=serial.EIGHTBITS
            )
            print(f"\n✅ LoRa Receiver connected to {self.port} at {self.baudrate} baud")
            return True
        except serial.SerialException as e:
            print(f"\n❌ LoRa Receiver failed to connect to {self.port}: {e}")
            print("\n💡 Tips:")
            print("  - Make sure the Arduino Nano is connected")
            print("  - Check the COM port (set the LORA_PORT env var — see top of app.py)")
            print("  - Close the Arduino IDE / Serial Monitor if it's open")
            print("  - Make sure lora_bridge.py isn't ALSO running against this same port")
            return False
        except Exception as e:
            print(f"\n❌ LoRa Receiver error: {e}")
            return False
    
    def disconnect(self):
        """Disconnect from serial port"""
        self.running = False
        if self.serial and self.serial.is_open:
            self.serial.close()
            print("🔌 LoRa Receiver disconnected")
    
    def start(self):
        """Start the receiver in a background thread"""
        if not self.connect():
            return False
        
        self.running = True
        thread = threading.Thread(target=self._receive_loop, daemon=True)
        thread.start()
        return True
    
    def _receive_loop(self):
        """Main receive loop - runs in background thread, one line at a time
        (matches exactly how the Nano prints: line by line)."""
        print("\n" + "=" * 40)
        print("  📡 LoRa Receiver Active")
        print("  Waiting for SOS messages...")
        print("=" * 40 + "\n")

        while self.running:
            try:
                if not self.serial or not self.serial.is_open:
                    time.sleep(1)
                    continue

                raw = self.serial.readline()
                if not raw:
                    continue
                try:
                    line = raw.decode("utf-8", errors="replace").strip()
                except Exception:
                    continue
                if not line:
                    continue

                self._handle_line(line)

            except Exception as e:
                print(f"❌ LoRa Receiver error: {e}")
                time.sleep(1)

    def _handle_line(self, line):
        """Echo the Nano's line, and pull GPS coordinates out of it
        whenever they appear — either the exact machine-readable
        "$SOS,<lat>,<lon>,<rssi>" line the bundled nano_receiver.ino
        sketch prints, OR (fallback, for a different/custom Nano sketch
        that prints its own banner text) any line containing a
        "<lat>,<lon>" pair, such as a "GPS: 12.87,74.85" or
        "MAP: https://maps.google.com/?q=12.87,74.85" line."""
        # Echo every line so the Nano's own log — whatever banner text,
        # "GPS:"/"MAP:" lines, and "RSSI: <n> dBm" it prints — is all
        # visible here.
        print(line)

        if "MESSAGE RECEIVED" in line or "PACKET RECEIVED" in line:
            self.message_count += 1

        # Track the most recent RSSI we've seen so the fallback path
        # below can attach it even when RSSI is printed on its own line,
        # separate from the GPS line (as with some custom sketches).
        rssi_match = self.RSSI_RE.search(line)
        if rssi_match:
            self._last_rssi = rssi_match.group(1)

        user_match = self.USER_RE.search(line)
        if user_match:
            name, uid = user_match.group(1).strip(), user_match.group(2).strip()
            if name:
                self._last_user_label = f"{name} (ID {uid})" if uid else name

        if line.startswith("$SOS,"):
            parts = line.split(",")
            if len(parts) < 3:
                return
            lat_str, lon_str = parts[1].strip(), parts[2].strip()
            rssi = parts[3].strip() if len(parts) > 3 else self._last_rssi

            if lat_str == "NOGPS" or lon_str == "NOGPS":
                print("⚠️  SOS received but the ESP32 had no GPS fix — can't pinpoint or dispatch without coordinates.")
                return

            try:
                lat, lon = float(lat_str), float(lon_str)
            except ValueError:
                print(f"⚠️  Couldn't parse GPS from Nano line: {line}")
                return

        elif "SOS" in line.upper() or "GPS" in line.upper() or "MAP" in line.upper():
            # Fallback path: this Nano isn't printing the exact "$SOS,"
            # line, but the line still looks GPS/SOS-related and may
            # contain raw coordinates — try to pull them out directly.
            match = self.LATLON_RE.search(line)
            if not match:
                return
            try:
                lat, lon = float(match.group(1)), float(match.group(2))
            except ValueError:
                return
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                return  # not a plausible lat/lon — probably an unrelated number
            rssi = self._last_rssi

        else:
            return

        now = time.time()
        key = (round(lat, 5), round(lon, 5))
        if now - self._last_sent.get(key, 0) < self.DEDUPE_SECONDS:
            return  # duplicate fix within the dedupe window (e.g. the same
                     # coordinates repeated across "GPS:"/"MAP:" lines in
                     # one packet) — already filed, don't file it again.
        self._last_sent[key] = now
        self.sos_count += 1

        maps_url = f"https://maps.google.com/?q={lat},{lon}"
        print("\n" + "=" * 40)
        print("🚨        SOS PINPOINTED")
        print("=" * 40)
        print(f"📍 Latitude:  {lat}")
        print(f"📍 Longitude: {lon}")
        print(f"🗺️  Map: {maps_url}")
        print(f"📶 RSSI: {rssi} dBm")
        print(f"🕐 Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"📊 Total messages: {self.message_count} | SOS alerts: {self.sos_count}")
        print("=" * 40)

        self._create_alert(lat, lon, rssi)

    def _create_alert(self, lat, lon, rssi):
        """Files this SOS exactly like POST /api/sos would, so it shows
        up pinned on the admin (and user) map with a real dispatch —
        nearest hospital/police/ambulance, and that region's Telegram
        alert — within the next ~2s poll. Uses its own DB connection
        since this runs in a background thread, not a Flask request."""
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            row, region, hospital, police, ambulance = create_alert(
                conn, lat, lon,
                device_id="NANO-LORA-BRIDGE",
                message=f"SOS button pressed (relayed via LoRa -> Nano, RSSI {rssi} dBm)",
                guest_label=self._last_user_label or self.DEFAULT_REPORTER_LABEL,
            )
            where = region["name"] if region else "no matching region"
            print(f"✅ Alert #{row['id']} filed and pinned on the admin map ({where})\n")
        except Exception as e:
            print(f"❌ Failed to file dashboard alert from LoRa SOS: {e}\n")
        finally:
            # Don't let a real "USER:" line (if one did come through) leak
            # into some future, unrelated packet that has none of its own
            # — that one should fall back to DEFAULT_REPORTER_LABEL instead.
            self._last_user_label = None
            conn.close()


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _table_columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate_schema():
    """Bring an existing rescue.db up to date with newer features (the
    users table, and alerts.user_id) without wiping existing data. Safe
    to call every startup — every statement is guarded by an existence
    check first."""
    conn = sqlite3.connect(DB_PATH)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "users" not in tables:
            conn.execute(
                """CREATE TABLE users (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    name           TEXT NOT NULL,
                    phone          TEXT NOT NULL UNIQUE,
                    password_hash  TEXT NOT NULL,
                    created_at     TEXT NOT NULL
                )"""
            )
        if "alerts" in tables and "user_id" not in _table_columns(conn, "alerts"):
            conn.execute("ALTER TABLE alerts ADD COLUMN user_id INTEGER REFERENCES users(id)")
        if "alerts" in tables and "ai_review_label" not in _table_columns(conn, "alerts"):
            conn.execute("ALTER TABLE alerts ADD COLUMN ai_review_label TEXT")
        if "alerts" in tables and "ai_review_confidence" not in _table_columns(conn, "alerts"):
            conn.execute("ALTER TABLE alerts ADD COLUMN ai_review_confidence REAL")
        if "alerts" in tables and "guest_label" not in _table_columns(conn, "alerts"):
            conn.execute("ALTER TABLE alerts ADD COLUMN guest_label TEXT")
        conn.commit()
    finally:
        conn.close()


def ensure_db():
    """Build + seed the database the first time the app runs; migrate it
    in place on every later run so existing installs pick up new tables
    (like `users`) without losing their alert history."""
    if not os.path.exists(DB_PATH):
        import seed
        seed.seed()
    else:
        migrate_schema()


# ---------------------------------------------------------------------------
# Admin auth — a single shared passcode checked against X-Admin-Token.
# Simple by design for a small deployment; swap for real accounts if this
# ever leaves a trusted network.
# ---------------------------------------------------------------------------
def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = request.headers.get("X-Admin-Token", "")
        if token != ADMIN_TOKEN:
            return jsonify({"error": "unauthorized"}), 401
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Distance helpers
# ---------------------------------------------------------------------------
def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def nearest_row(db, table, lat, lon, extra_where="", extra_params=()):
    rows = db.execute(f"SELECT * FROM {table} {extra_where}", extra_params).fetchall()
    best, best_dist = None, None
    for row in rows:
        d = haversine_km(lat, lon, row["lat"], row["lon"])
        if best_dist is None or d < best_dist:
            best, best_dist = row, d
    return best, best_dist


# ---------------------------------------------------------------------------
# Place-name lookup — used by the public "Find nearby help" panel so a
# user can type a place name instead of raw coordinates.
# ---------------------------------------------------------------------------
def find_local_place(db, place_name):
    """Try to match the typed text against places/hospitals/police we
    already know about before ever going out to the network."""
    like = f"%{place_name}%"
    row = db.execute("SELECT name, lat, lon FROM regions WHERE name LIKE ? LIMIT 1", (like,)).fetchone()
    if row:
        return row["name"], row["lat"], row["lon"]
    row = db.execute("SELECT name, lat, lon FROM hospitals WHERE name LIKE ? LIMIT 1", (like,)).fetchone()
    if row:
        return row["name"], row["lat"], row["lon"]
    row = db.execute("SELECT name, lat, lon FROM police_stations WHERE name LIKE ? LIMIT 1", (like,)).fetchone()
    if row:
        return row["name"], row["lat"], row["lon"]
    return None


def geocode_place(place_name):
    """Fall back to OpenStreetMap's free Nominatim geocoder, biased to the
    coastal-belt bounding box this dashboard covers. Returns
    (lat, lon, display_name) or None. Never raises — a geocoding miss or
    network hiccup should degrade to 'not found', not a 500."""
    try:
        q = urllib.parse.quote(place_name)
        url = (
            "https://nominatim.openstreetmap.org/search"
            f"?q={q}&format=json&limit=1&viewbox=74.55,13.55,75.10,12.75&bounded=0"
        )
        req = urllib.request.Request(
            url, headers={"User-Agent": "rescue-dashboard/1.0 (emergency dispatch demo)"}
        )
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not data:
            return None
        hit = data[0]
        return float(hit["lat"]), float(hit["lon"]), hit.get("display_name", place_name)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Telegram — one bot per region. Fires in a background thread so the SOS
# API response never waits on a network call.
# ---------------------------------------------------------------------------
def build_telegram_text(region, hospital, police, ambulance, alert):
    lines = [
        f"\U0001F6A8 SOS ALERT \u2014 {region['name']}",
        f"Location: {alert['victim_lat']:.5f}, {alert['victim_lon']:.5f}",
        f"Map: https://maps.google.com/?q={alert['victim_lat']},{alert['victim_lon']}",
        f"Nearest Hospital: {hospital['name'] if hospital else 'Unknown'}"
        + (f" ({hospital['phone']})" if hospital and hospital["phone"] else ""),
        f"Nearest Police Station: {police['name'] if police else 'Unknown'}"
        + (f" ({police['phone']})" if police and police["phone"] else ""),
        f"Ambulance: {ambulance['code'] if ambulance else 'None available right now'}",
        f"Device: {alert.get('device_id') or 'unknown device'}",
    ]
    # Only set for alerts with no logged-in account behind them (guest
    # "Send SOS" press, or straight from the ESP32/Nano hardware) — a
    # logged-in user's alert already carries their name via user_id, so
    # this line is skipped for those instead of guessing.
    if alert.get("guest_label"):
        lines.append(f"Reported by: {alert['guest_label']}")
    lines.append(f"Time (UTC): {alert['created_at']}")
    return "\n".join(lines)


def send_telegram_alert(region, hospital, police, ambulance, alert):
    """Returns (sent: bool, error: str|None)."""
    if not region or not region["telegram_bot_token"] or not region["telegram_chat_id"]:
        return False, "no Telegram bot configured for this region"

    text = build_telegram_text(region, hospital, police, ambulance, alert)
    url = f"https://api.telegram.org/bot{region['telegram_bot_token']}/sendMessage"
    payload = json.dumps({"chat_id": region["telegram_chat_id"], "text": text}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            resp.read()
        return True, None
    except urllib.error.HTTPError as e:
        return False, f"Telegram API error {e.code}"
    except Exception as e:
        return False, str(e)


def send_telegram_and_record(alert_id, region, hospital, police, ambulance, alert):
    ok, err = send_telegram_alert(region, hospital, police, ambulance, alert)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "UPDATE alerts SET telegram_sent = ?, telegram_error = ? WHERE id = ?",
        (1 if ok else 0, err, alert_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/user")
def user_page():
    return render_template("user.html")


@app.route("/admin")
def admin_page():
    return render_template("admin.html")


@app.route("/admin/analytics")
def admin_analytics_page():
    """Separate full page (opened from the Analytics button) showing both
    the live operational charts and the Colab-trained model's insights.
    Admin-only in practice: it reuses the same client-side passcode gate
    as /admin, and every chart it draws comes from admin-token-gated
    APIs."""
    return render_template("admin_analytics.html")


# ---------------------------------------------------------------------------
# API: user accounts — sign up / log in from the /user page. This is a
# simple demo-grade auth (no sessions/cookies, no email verification):
# the browser just remembers {id, name, phone} in localStorage after a
# successful call and sends the id along with any SOS it raises, so the
# admin panel can show who reported what.
# ---------------------------------------------------------------------------
def serialize_user(row, alerts_count=None, status_breakdown=None, ai_breakdown=None, last_alert_at=None):
    out = {"id": row["id"], "name": row["name"], "phone": row["phone"], "created_at": row["created_at"]}
    if alerts_count is not None:
        out["alerts_count"] = alerts_count
    if status_breakdown is not None:
        out["status_breakdown"] = status_breakdown
    if ai_breakdown is not None:
        out["ai_breakdown"] = ai_breakdown
    if last_alert_at is not None:
        out["last_alert_at"] = last_alert_at
    return out


@app.route("/api/signup", methods=["POST"])
def api_signup():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    password = data.get("password") or ""

    if not name or not phone or not password:
        return jsonify({"error": "Name, phone, and password are all required."}), 400
    if len(password) < 4:
        return jsonify({"error": "Password must be at least 4 characters."}), 400

    db = get_db()
    if db.execute("SELECT id FROM users WHERE phone = ?", (phone,)).fetchone():
        return jsonify({"error": "An account with that phone number already exists — try logging in instead."}), 409

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = db.execute(
        "INSERT INTO users (name, phone, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (name, phone, generate_password_hash(password), now),
    )
    db.commit()
    row = db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(serialize_user(row)), 201


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or {}
    phone = (data.get("phone") or "").strip()
    password = data.get("password") or ""

    db = get_db()
    row = db.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        return jsonify({"error": "That phone number and password don't match any account."}), 401
    return jsonify(serialize_user(row))


@app.route("/api/admin/users")
@require_admin
def api_admin_users():
    db = get_db()
    rows = db.execute(
        """SELECT u.*,
                  COUNT(a.id) AS alerts_count,
                  SUM(CASE WHEN a.status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                  SUM(CASE WHEN a.status = 'dispatched' THEN 1 ELSE 0 END) AS dispatched_count,
                  SUM(CASE WHEN a.status = 'resolved' THEN 1 ELSE 0 END) AS resolved_count,
                  SUM(CASE WHEN a.ai_review_label = 'Yes' THEN 1 ELSE 0 END) AS ai_yes_count,
                  SUM(CASE WHEN a.ai_review_label = 'No' THEN 1 ELSE 0 END) AS ai_no_count,
                  MAX(a.created_at) AS last_alert_at
           FROM users u LEFT JOIN alerts a ON a.user_id = u.id
           GROUP BY u.id ORDER BY u.id DESC"""
    ).fetchall()
    return jsonify([
        serialize_user(
            r, r["alerts_count"],
            status_breakdown={
                "pending": r["pending_count"] or 0,
                "dispatched": r["dispatched_count"] or 0,
                "resolved": r["resolved_count"] or 0,
            },
            ai_breakdown={"yes": r["ai_yes_count"] or 0, "no": r["ai_no_count"] or 0},
            last_alert_at=r["last_alert_at"],
        )
        for r in rows
    ])


# ---------------------------------------------------------------------------
# API: facilities (map layer)
# ---------------------------------------------------------------------------
@app.route("/api/facilities")
def api_facilities():
    db = get_db()
    regions = [dict(r) for r in db.execute("SELECT * FROM regions ORDER BY name").fetchall()]
    for r in regions:
        r["telegram_configured"] = bool(r.get("telegram_bot_token") and r.get("telegram_chat_id"))
        # never expose the raw bot token to the browser
        r.pop("telegram_bot_token", None)
    hospitals = [dict(r) for r in db.execute("SELECT * FROM hospitals ORDER BY name").fetchall()]
    police = [dict(r) for r in db.execute("SELECT * FROM police_stations ORDER BY name").fetchall()]
    ambulances = [dict(r) for r in db.execute("SELECT * FROM ambulances ORDER BY code").fetchall()]
    return jsonify(
        {
            "regions": regions,
            "hospitals": hospitals,
            "police_stations": police,
            "ambulances": ambulances,
        }
    )


# ---------------------------------------------------------------------------
# API: public "find nearby help" lookup — no admin token required. A user
# supplies either {lat, lon} or {place: "..."} and gets back the nearest
# hospital, police station, available ambulance, and covering place.
# ---------------------------------------------------------------------------
@app.route("/api/lookup", methods=["POST"])
def api_lookup():
    data = request.get_json(silent=True) or {}
    place_name = (data.get("place") or "").strip()

    lat = lon = None
    raw_lat, raw_lon = data.get("lat"), data.get("lon")
    if raw_lat not in (None, "") and raw_lon not in (None, ""):
        try:
            lat = float(raw_lat)
            lon = float(raw_lon)
        except (TypeError, ValueError):
            lat = lon = None

    db = get_db()
    resolved_label = None

    if lat is None or lon is None:
        if not place_name:
            return jsonify({"error": "Enter coordinates or a place name."}), 400
        local = find_local_place(db, place_name)
        if local:
            resolved_label, lat, lon = local
        else:
            geo = geocode_place(place_name)
            if geo is None:
                return jsonify({"error": f'Couldn\'t find "{place_name}" \u2014 try entering coordinates instead.'}), 404
            lat, lon, resolved_label = geo
    else:
        resolved_label = f"{lat:.5f}, {lon:.5f}"

    region, r_dist = nearest_row(db, "regions", lat, lon)
    hospital, h_dist = nearest_row(db, "hospitals", lat, lon)
    police, p_dist = nearest_row(db, "police_stations", lat, lon)
    ambulance, a_dist = nearest_row(db, "ambulances", lat, lon, extra_where="WHERE status = 'available'")

    def fmt(row, dist, fields):
        if row is None:
            return None
        out = {k: row[k] for k in fields}
        out["distance_km"] = round(dist, 2)
        return out

    return jsonify(
        {
            "query_lat": lat,
            "query_lon": lon,
            "resolved_label": resolved_label,
            "region": fmt(region, r_dist, ["id", "name", "lat", "lon"]),
            "hospital": fmt(hospital, h_dist, ["id", "name", "phone", "lat", "lon"]),
            "police_station": fmt(police, p_dist, ["id", "name", "phone", "lat", "lon"]),
            "ambulance": fmt(ambulance, a_dist, ["id", "code", "lat", "lon"]),
        }
    )


# ---------------------------------------------------------------------------
# API: alerts
# ---------------------------------------------------------------------------
def serialize_alert(db, row):
    hospital = db.execute("SELECT * FROM hospitals WHERE id = ?", (row["nearest_hospital_id"],)).fetchone()
    police = db.execute("SELECT * FROM police_stations WHERE id = ?", (row["nearest_police_id"],)).fetchone()
    ambulance = db.execute("SELECT * FROM ambulances WHERE id = ?", (row["ambulance_id"],)).fetchone()
    region = db.execute("SELECT * FROM regions WHERE id = ?", (row["region_id"],)).fetchone()
    user = None
    try:
        if row["user_id"]:
            user = db.execute("SELECT id, name, phone FROM users WHERE id = ?", (row["user_id"],)).fetchone()
    except (IndexError, KeyError):
        user = None
    ai_review = None
    try:
        if row["ai_review_label"]:
            ai_review = {"label": row["ai_review_label"], "confidence": row["ai_review_confidence"]}
    except (IndexError, KeyError):
        ai_review = None
    guest_label = None
    try:
        guest_label = row["guest_label"]
    except (IndexError, KeyError):
        guest_label = None
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "user": {"id": user["id"], "name": user["name"], "phone": user["phone"]} if user else None,
        "guest_label": guest_label,
        "victim_lat": row["victim_lat"],
        "victim_lon": row["victim_lon"],
        "message": row["message"],
        "status": row["status"],
        "created_at": row["created_at"],
        "resolved_at": row["resolved_at"],
        "region": region["name"] if region else None,
        "hospital": {"name": hospital["name"], "phone": hospital["phone"], "lat": hospital["lat"], "lon": hospital["lon"]} if hospital else None,
        "police_station": {"name": police["name"], "phone": police["phone"], "lat": police["lat"], "lon": police["lon"]} if police else None,
        "ambulance": {"code": ambulance["code"], "status": ambulance["status"], "lat": ambulance["lat"], "lon": ambulance["lon"]} if ambulance else None,
        "telegram_sent": bool(row["telegram_sent"]),
        "telegram_error": row["telegram_error"],
        "ai_review": ai_review,
    }


@app.route("/api/alerts")
def api_alerts_list():
    db = get_db()
    user_id = request.args.get("user_id")
    if user_id:
        rows = db.execute(
            "SELECT * FROM alerts WHERE user_id = ? ORDER BY id DESC LIMIT 100", (user_id,)
        ).fetchall()
    else:
        rows = db.execute("SELECT * FROM alerts ORDER BY id DESC LIMIT 100").fetchall()
    return jsonify([serialize_alert(db, r) for r in rows])


def find_nearest_facilities(db, lat, lon):
    """Shared nearest-region/hospital/police lookup — used by /api/sos and
    by the admin approve/dispatch step so both paths agree on what's
    "nearest" for a given point, whichever of the 4 places it falls near."""
    region, _ = nearest_row(db, "regions", lat, lon)
    hospital, _ = nearest_row(db, "hospitals", lat, lon)
    police, _ = nearest_row(db, "police_stations", lat, lon)
    return region, hospital, police


def make_guest_label():
    """A short, human-readable stand-in identity for an SOS raised without
    being logged in — from the web page's Send SOS button with no
    account, or straight from the hardware (ESP32 Wi-Fi POST or the
    Nano/LoRa bridge, neither of which ever sends a user_id). Gives the
    admin feed and map something to show instead of a blank reporter,
    without needing a real account. Not tied to any device or person
    across alerts — just a friendly per-alert label."""
    return f"Guest-{random.randint(1000, 9999)}"


def find_available_ambulance(db, lat, lon, hospital):
    """Prefer an available ambulance from the same region as the nearest
    hospital; fall back to the closest available ambulance anywhere."""
    ambulance, _ = nearest_row(
        db, "ambulances", lat, lon,
        extra_where="WHERE status = 'available' AND region_id = ?",
        extra_params=(hospital["region_id"],) if hospital else (),
    )
    if ambulance is None:
        ambulance, _ = nearest_row(db, "ambulances", lat, lon, extra_where="WHERE status = 'available'")
    return ambulance


def create_alert(db, lat, lon, device_id, message, user_id=None, require_approval=False, guest_label=None):
    """Shared alert-filing logic — the one code path both POST /api/sos
    (ESP32 Wi-Fi, admin "simulate", the /user page) AND the in-process
    LoRa receiver (Nano -> serial -> straight into this function, no
    HTTP involved) use. Finds the nearest region/hospital/police,
    assigns an ambulance, inserts the alert, and kicks off that
    region's Telegram notification in the background. Returns
    (alert_row, region, hospital, police, ambulance)."""
    if user_id is not None and db.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        user_id = None

    # No logged-in account behind this SOS (guest button press on /user,
    # or straight from the ESP32/Nano hardware) -> give it a throwaway
    # guest label so it still shows up as "someone" on the admin feed
    # and map instead of a blank reporter. Callers that already know who
    # this device belongs to (e.g. the LoRa receiver parsing a "USER:"
    # line out of a specific ESP32's packet) can pass guest_label
    # themselves to use that fixed identity instead of a random one.
    if user_id is not None:
        guest_label = None
    elif guest_label is None:
        guest_label = make_guest_label()

    region, hospital, police = find_nearest_facilities(db, lat, lon)

    # A "require_approval" alert (e.g. raised from the user page's Find
    # Nearest Help panel) always waits for an admin to approve it —
    # no ambulance is claimed until then.
    ambulance = None if require_approval else find_available_ambulance(db, lat, lon, hospital)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cur = db.execute(
        """INSERT INTO alerts
           (device_id, user_id, guest_label, victim_lat, victim_lon, message, region_id,
            nearest_hospital_id, nearest_police_id, ambulance_id, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            device_id, user_id, guest_label, lat, lon, message,
            region["id"] if region else None,
            hospital["id"] if hospital else None,
            police["id"] if police else None,
            ambulance["id"] if ambulance else None,
            "dispatched" if ambulance else "pending",
            now,
        ),
    )
    alert_id = cur.lastrowid

    if ambulance:
        db.execute("UPDATE ambulances SET status = 'dispatched' WHERE id = ?", (ambulance["id"],))

    db.commit()

    row = db.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()

    # Fire the region's own Telegram bot in the background — detects the
    # victim's place and notifies that place's hospital/police channel
    # without blocking the caller (the HTTP response, or the LoRa
    # receiver's read loop).
    if region:
        threading.Thread(
            target=send_telegram_and_record,
            args=(alert_id, dict(region), dict(hospital) if hospital else None,
                  dict(police) if police else None, dict(ambulance) if ambulance else None, dict(row)),
            daemon=True,
        ).start()

    return row, region, hospital, police, ambulance


@app.route("/api/sos", methods=["POST"])
def api_sos():
    """
    Ingest an SOS event. Expected JSON body from the ESP32 (or the
    dashboard's "simulate" control / admin's manual signal / a user's
    "Find nearest help" lookup, or lora_bridge.py relaying a LoRa fix
    from a Nano on a different computer):
        { "lat": 12.91, "lon": 74.85, "device_id": "ESP32-01", "message": "SOS",
          "user_id": 7,            # optional — set when a logged-in person
                                    # raises it from the /user page
          "guest_label": "VISHWAS (ID 1236)",  # optional — a fixed
                                    # reporter identity to show on the
                                    # admin feed/map instead of a random
                                    # "Guest-XXXX" label. Ignored when
                                    # user_id is also set.
          "require_approval": true # optional — when true, the alert is
                                    # always filed as "pending" and no
                                    # ambulance is auto-assigned; an admin
                                    # must approve it from the dashboard
                                    # before it counts as dispatched. }
    """
    data = request.get_json(silent=True) or {}
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "lat and lon are required numeric fields"}), 400

    device_id = data.get("device_id", "UNKNOWN-DEVICE")
    message = data.get("message", "SOS - immediate assistance required")
    require_approval = bool(data.get("require_approval"))
    guest_label = data.get("guest_label") or None
    user_id = data.get("user_id")
    try:
        user_id = int(user_id) if user_id not in (None, "") else None
    except (TypeError, ValueError):
        user_id = None

    db = get_db()
    row, region, hospital, police, ambulance = create_alert(
        db, lat, lon, device_id, message,
        user_id=user_id, require_approval=require_approval, guest_label=guest_label,
    )
    return jsonify(serialize_alert(db, row)), 201


@app.route("/api/alerts/<int:alert_id>/approve", methods=["POST"])
@require_admin
def api_approve_alert(alert_id):
    """Admin approves a pending alert: claims the nearest available
    ambulance (same logic as a normal SOS) and moves it to 'dispatched'.
    Used for alerts that were raised with require_approval=true (e.g. a
    user's "Find nearest help" request) so nothing gets dispatched until
    a human signs off on it."""
    db = get_db()
    row = db.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    if row is None:
        return jsonify({"error": "alert not found"}), 404
    if row["status"] != "pending":
        return jsonify({"error": "only pending alerts can be approved"}), 400

    hospital = db.execute("SELECT * FROM hospitals WHERE id = ?", (row["nearest_hospital_id"],)).fetchone()
    ambulance = find_available_ambulance(db, row["victim_lat"], row["victim_lon"], hospital)
    if ambulance is None:
        return jsonify({"error": "No ambulance is currently available to dispatch."}), 409

    db.execute(
        "UPDATE alerts SET status = 'dispatched', ambulance_id = ? WHERE id = ?",
        (ambulance["id"], alert_id),
    )
    db.execute("UPDATE ambulances SET status = 'dispatched' WHERE id = ?", (ambulance["id"],))
    db.commit()

    row = db.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()

    region = db.execute("SELECT * FROM regions WHERE id = ?", (row["region_id"],)).fetchone()
    police = db.execute("SELECT * FROM police_stations WHERE id = ?", (row["nearest_police_id"],)).fetchone()

    # AI review: once the admin has approved a dispatch, run the trained
    # model over this incident (location + which responders got assigned)
    # and store a Yes/No satisfaction prediction the user sees on their
    # alert card, and log the incident + prediction to its own CSV file.
    area_km = haversine_km(row["victim_lat"], row["victim_lon"], hospital["lat"], hospital["lon"]) if hospital else 0.0
    region_name = region["name"] if region else None
    ai_result = predict_satisfaction(
        row["victim_lat"], row["victim_lon"], region_name,
        hospital is not None, police is not None, True, area_km,
    )
    if ai_result:
        db.execute(
            "UPDATE alerts SET ai_review_label = ?, ai_review_confidence = ? WHERE id = ?",
            (ai_result["label"], ai_result["confidence"], alert_id),
        )
        db.commit()
        row = db.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    log_prediction_to_csv(
        alert_id, row["victim_lat"], row["victim_lon"], region_name, area_km,
        hospital is not None, police is not None, True, ai_result,
    )

    if region:
        threading.Thread(
            target=send_telegram_and_record,
            args=(alert_id, dict(region), dict(hospital) if hospital else None,
                  dict(police) if police else None, dict(ambulance), dict(row)),
            daemon=True,
        ).start()

    return jsonify(serialize_alert(db, row))


@app.route("/api/alerts/<int:alert_id>/resolve", methods=["POST"])
def api_resolve_alert(alert_id):
    db = get_db()
    row = db.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    if row is None:
        return jsonify({"error": "alert not found"}), 404

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.execute("UPDATE alerts SET status = 'resolved', resolved_at = ? WHERE id = ?", (now, alert_id))
    if row["ambulance_id"]:
        db.execute("UPDATE ambulances SET status = 'available' WHERE id = ?", (row["ambulance_id"],))
    db.commit()

    row = db.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    return jsonify(serialize_alert(db, row))


@app.route("/api/alerts/<int:alert_id>/status", methods=["POST"])
@require_admin
def api_set_alert_status(alert_id):
    """Admin control to move an alert directly between pending / dispatched /
    resolved — e.g. to revert a dispatched alert back to pending if it was
    marked in error, without going through the resolve flow."""
    data = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in ("pending", "dispatched", "resolved"):
        return jsonify({"error": "status must be one of pending, dispatched, resolved"}), 400

    db = get_db()
    row = db.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    if row is None:
        return jsonify({"error": "alert not found"}), 404

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if status == "resolved":
        db.execute("UPDATE alerts SET status = 'resolved', resolved_at = ? WHERE id = ?", (now, alert_id))
        if row["ambulance_id"]:
            db.execute("UPDATE ambulances SET status = 'available' WHERE id = ?", (row["ambulance_id"],))
    else:
        db.execute("UPDATE alerts SET status = ?, resolved_at = NULL WHERE id = ?", (status, alert_id))
        if status == "pending" and row["ambulance_id"]:
            # Freed back up so it can be redispatched to this or another alert.
            db.execute("UPDATE ambulances SET status = 'available' WHERE id = ?", (row["ambulance_id"],))

    db.commit()
    row = db.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
    return jsonify(serialize_alert(db, row))


# ---------------------------------------------------------------------------
# API: status summary
# ---------------------------------------------------------------------------
@app.route("/api/status")
def api_status():
    db = get_db()
    total = db.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"]
    pending = db.execute("SELECT COUNT(*) c FROM alerts WHERE status = 'pending'").fetchone()["c"]
    dispatched = db.execute("SELECT COUNT(*) c FROM alerts WHERE status = 'dispatched'").fetchone()["c"]
    resolved = db.execute("SELECT COUNT(*) c FROM alerts WHERE status = 'resolved'").fetchone()["c"]
    amb_total = db.execute("SELECT COUNT(*) c FROM ambulances").fetchone()["c"]
    amb_available = db.execute("SELECT COUNT(*) c FROM ambulances WHERE status = 'available'").fetchone()["c"]
    by_region = db.execute(
        """SELECT r.name, COUNT(a.id) c FROM regions r
           LEFT JOIN alerts a ON a.region_id = r.id
           GROUP BY r.id ORDER BY r.name"""
    ).fetchall()

    return jsonify(
        {
            "total_alerts": total,
            "pending": pending,
            "dispatched": dispatched,
            "resolved": resolved,
            "ambulances_total": amb_total,
            "ambulances_available": amb_available,
            "alerts_by_region": [{"region": r["name"], "count": r["c"]} for r in by_region],
        }
    )


# ---------------------------------------------------------------------------
# API: analytics — powers the "Analytics" tab (date-wise resolved counts,
# per-hospital load, per-region breakdown). Used by both the user page and
# the admin page.
# ---------------------------------------------------------------------------
@app.route("/api/analytics")
def api_analytics():
    db = get_db()

    resolved_by_date = db.execute(
        """SELECT substr(resolved_at, 1, 10) AS d, COUNT(*) c
           FROM alerts WHERE status = 'resolved' AND resolved_at IS NOT NULL
           GROUP BY d ORDER BY d"""
    ).fetchall()

    alerts_by_date = db.execute(
        """SELECT substr(created_at, 1, 10) AS d, COUNT(*) c
           FROM alerts GROUP BY d ORDER BY d"""
    ).fetchall()

    by_hospital = db.execute(
        """SELECT h.name AS hospital, COUNT(a.id) total,
                  SUM(CASE WHEN a.status = 'resolved' THEN 1 ELSE 0 END) resolved
           FROM hospitals h LEFT JOIN alerts a ON a.nearest_hospital_id = h.id
           GROUP BY h.id ORDER BY total DESC"""
    ).fetchall()

    by_region = db.execute(
        """SELECT r.name AS region, COUNT(a.id) total,
                  SUM(CASE WHEN a.status = 'resolved' THEN 1 ELSE 0 END) resolved
           FROM regions r LEFT JOIN alerts a ON a.region_id = r.id
           GROUP BY r.id ORDER BY total DESC"""
    ).fetchall()

    status_counts = db.execute(
        """SELECT status, COUNT(*) c FROM alerts GROUP BY status"""
    ).fetchall()

    return jsonify(
        {
            "resolved_by_date": [{"date": r["d"], "count": r["c"]} for r in resolved_by_date],
            "alerts_by_date": [{"date": r["d"], "count": r["c"]} for r in alerts_by_date],
            "by_hospital": [{"hospital": r["hospital"], "total": r["total"], "resolved": r["resolved"] or 0} for r in by_hospital],
            "by_region": [{"region": r["region"], "total": r["total"], "resolved": r["resolved"] or 0} for r in by_region],
            "status_counts": {r["status"]: r["c"] for r in status_counts},
        }
    )


# ---------------------------------------------------------------------------
# API: training analytics — the "Colab" side of the /admin/analytics page.
# Reads final_updated_data.csv (the 400-row dataset the model was trained
# on) directly rather than through pandas, so this endpoint has no heavy
# dependency beyond the stdlib csv module. Admin-only.
# ---------------------------------------------------------------------------
def _read_training_rows():
    rows = []
    if not os.path.exists(TRAINING_DATA_PATH):
        return rows
    with open(TRAINING_DATA_PATH, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


@app.route("/api/admin/training_analytics")
@require_admin
def api_admin_training_analytics():
    rows = _read_training_rows()
    total = len(rows)
    satisfied_total = sum(1 for r in rows if r.get("satisfied_customer") == "1")
    overall_rate = round(100 * satisfied_total / total, 1) if total else 0

    by_place = {}
    for r in rows:
        p = r.get("place", "Unknown")
        d = by_place.setdefault(p, {"place": p, "total": 0, "satisfied": 0})
        d["total"] += 1
        d["satisfied"] += 1 if r.get("satisfied_customer") == "1" else 0
    by_place_list = sorted(by_place.values(), key=lambda d: -d["total"])

    def rate_by_flag(field):
        out = []
        for val, label in ((1, "Assigned"), (0, "Not assigned")):
            subset = [r for r in rows if r.get(field) == str(val)]
            tot = len(subset)
            sat = sum(1 for r in subset if r.get("satisfied_customer") == "1")
            out.append({"label": label, "total": tot, "satisfied": sat})
        return out

    model = get_satisfaction_model()
    importances = []
    if model is not None and hasattr(model, "feature_importances_"):
        for name, imp in zip(MODEL_FEATURES, model.feature_importances_):
            importances.append({"feature": name.replace("place_", ""), "total": round(float(imp), 4)})
        importances.sort(key=lambda d: -d["total"])

    live_log_count = 0
    if os.path.exists(LIVE_LOG_PATH):
        with open(LIVE_LOG_PATH, newline="") as f:
            live_log_count = max(0, sum(1 for _ in f) - 1)  # minus header row

    return jsonify(
        {
            "total_rows": total,
            "overall_satisfaction_rate": overall_rate,
            "by_place": by_place_list,
            "by_hospital_assigned": rate_by_flag("hospital_assigned"),
            "by_police_assigned": rate_by_flag("police_station_assigned"),
            "by_ambulance_assigned": rate_by_flag("ambulance_assigned"),
            "feature_importances": importances,
            "model_available": model is not None,
            "live_predictions_logged": live_log_count,
        }
    )


def _pearson_corr(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    denom = math.sqrt(var_x * var_y)
    if denom == 0:
        return None
    return cov / denom


@app.route("/api/admin/training_diagnostics")
@require_admin
def api_admin_training_diagnostics():
    """Deeper model diagnostics for the /admin/analytics page: a
    correlation heatmap across the numeric training columns, plus the
    trained model's confusion matrix, ROC curve, and accuracy/precision/
    recall/F1 — all evaluated on final_updated_data.csv itself (the 400
    rows the model was trained on; there's no separate held-out test set
    on disk, so this reads as "how it fits its own training data" rather
    than a generalization estimate).

    Deliberately pandas-free (plain csv + stdlib math for the
    correlation matrix, scikit-learn — already a hard dependency for the
    model itself — for the metrics) so this endpoint can't 500 just
    because pandas isn't installed in a given deployment."""
    rows = _read_training_rows()
    out = {"rows_evaluated": len(rows)}

    numeric_cols = ["latitude", "longitude", "area", "hospital_assigned",
                     "police_station_assigned", "ambulance_assigned", "satisfied_customer"]

    columns = {}
    parse_ok = bool(rows)
    for col in numeric_cols:
        try:
            columns[col] = [float(r[col]) for r in rows]
        except (KeyError, ValueError, TypeError):
            parse_ok = False
            break

    if parse_ok:
        matrix = []
        for c1 in numeric_cols:
            row = []
            for c2 in numeric_cols:
                if c1 == c2:
                    row.append(1.0)
                else:
                    corr = _pearson_corr(columns[c1], columns[c2])
                    row.append(round(corr, 3) if corr is not None else None)
            matrix.append(row)
        out["correlation"] = {"labels": numeric_cols, "matrix": matrix}
    else:
        out["correlation"] = {"labels": numeric_cols, "matrix": []}

    model = get_satisfaction_model()
    out["model_available"] = model is not None

    if model is not None and parse_ok and rows:
        try:
            from sklearn.metrics import (
                confusion_matrix, roc_curve, auc,
                accuracy_score, precision_score, recall_score, f1_score,
            )

            X = []
            y_true = []
            for i, r in enumerate(rows):
                place = r.get("place")
                feat = [1 if place == p else 0 for p in MODEL_PLACES]
                feat += [
                    columns["latitude"][i], columns["longitude"][i], columns["area"][i],
                    columns["hospital_assigned"][i], columns["police_station_assigned"][i],
                    columns["ambulance_assigned"][i],
                ]
                X.append(feat)
                y_true.append(int(columns["satisfied_customer"][i]))

            y_pred = model.predict(X)
            cm = confusion_matrix(y_true, y_pred, labels=[0, 1]).tolist()
            out["confusion_matrix"] = {"labels": ["No", "Yes"], "matrix": cm}
            out["metrics"] = {
                "accuracy": round(float(accuracy_score(y_true, y_pred)), 3),
                "precision": round(float(precision_score(y_true, y_pred, zero_division=0)), 3),
                "recall": round(float(recall_score(y_true, y_pred, zero_division=0)), 3),
                "f1": round(float(f1_score(y_true, y_pred, zero_division=0)), 3),
            }
            if hasattr(model, "predict_proba"):
                y_proba = [p[1] for p in model.predict_proba(X)]
                fpr, tpr, _ = roc_curve(y_true, y_proba)
                roc_auc = auc(fpr, tpr)
                # Thin out to at most ~60 points — 400 rows can produce
                # more threshold steps than a small inline chart needs.
                step = max(1, len(fpr) // 60)
                out["roc"] = {
                    "fpr": [round(float(x), 4) for x in fpr[::step]],
                    "tpr": [round(float(x), 4) for x in tpr[::step]],
                    "auc": round(float(roc_auc), 3),
                }
            yes_count = sum(y_true)
            out["class_balance"] = {"yes": yes_count, "no": len(y_true) - yes_count}
        except Exception as e:
            out["diagnostics_error"] = str(e)

    return jsonify(out)


# ---------------------------------------------------------------------------
# Admin API — add places, hospitals, police stations, ambulances; configure
# each region's Telegram bot. All require the X-Admin-Token header.
# ---------------------------------------------------------------------------
@app.route("/api/admin/verify")
@require_admin
def api_admin_verify():
    return jsonify({"ok": True})


@app.route("/api/admin/regions", methods=["POST"])
@require_admin
def api_admin_add_region():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    try:
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "name, lat, and lon are required"}), 400
    if not name:
        return jsonify({"error": "name, lat, and lon are required"}), 400

    token = (data.get("telegram_bot_token") or "").strip()
    chat_id = (data.get("telegram_chat_id") or "").strip()

    db = get_db()
    cur = db.execute(
        "INSERT INTO regions (name, lat, lon, telegram_bot_token, telegram_chat_id) VALUES (?, ?, ?, ?, ?)",
        (name, lat, lon, token, chat_id),
    )
    db.commit()
    row = db.execute("SELECT * FROM regions WHERE id = ?", (cur.lastrowid,)).fetchone()
    result = dict(row)
    result["telegram_configured"] = bool(token and chat_id)
    result.pop("telegram_bot_token", None)
    return jsonify(result), 201


@app.route("/api/admin/regions/<int:region_id>/telegram", methods=["PATCH"])
@require_admin
def api_admin_update_region_telegram(region_id):
    data = request.get_json(silent=True) or {}
    token = (data.get("telegram_bot_token") or "").strip()
    chat_id = (data.get("telegram_chat_id") or "").strip()

    db = get_db()
    row = db.execute("SELECT * FROM regions WHERE id = ?", (region_id,)).fetchone()
    if row is None:
        return jsonify({"error": "region not found"}), 404

    db.execute(
        "UPDATE regions SET telegram_bot_token = ?, telegram_chat_id = ? WHERE id = ?",
        (token, chat_id, region_id),
    )
    db.commit()
    row = db.execute("SELECT * FROM regions WHERE id = ?", (region_id,)).fetchone()
    result = dict(row)
    result["telegram_configured"] = bool(token and chat_id)
    result.pop("telegram_bot_token", None)
    return jsonify(result)


@app.route("/api/admin/hospitals", methods=["POST"])
@require_admin
def api_admin_add_hospital():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    try:
        region_id = int(data["region_id"])
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "region_id, name, lat, and lon are required"}), 400
    if not name:
        return jsonify({"error": "region_id, name, lat, and lon are required"}), 400

    db = get_db()
    if db.execute("SELECT id FROM regions WHERE id = ?", (region_id,)).fetchone() is None:
        return jsonify({"error": "region not found"}), 404

    cur = db.execute(
        "INSERT INTO hospitals (region_id, name, lat, lon, phone) VALUES (?, ?, ?, ?, ?)",
        (region_id, name, lat, lon, phone),
    )
    db.commit()
    row = db.execute("SELECT * FROM hospitals WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/admin/police_stations", methods=["POST"])
@require_admin
def api_admin_add_police():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    try:
        region_id = int(data["region_id"])
        lat = float(data["lat"])
        lon = float(data["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "region_id, name, lat, and lon are required"}), 400
    if not name:
        return jsonify({"error": "region_id, name, lat, and lon are required"}), 400

    db = get_db()
    if db.execute("SELECT id FROM regions WHERE id = ?", (region_id,)).fetchone() is None:
        return jsonify({"error": "region not found"}), 404

    cur = db.execute(
        "INSERT INTO police_stations (region_id, name, lat, lon, phone) VALUES (?, ?, ?, ?, ?)",
        (region_id, name, lat, lon, phone),
    )
    db.commit()
    row = db.execute("SELECT * FROM police_stations WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row)), 201


@app.route("/api/admin/ambulances", methods=["POST"])
@require_admin
def api_admin_add_ambulance():
    data = request.get_json(silent=True) or {}
    code = (data.get("code") or "").strip()
    try:
        region_id = int(data["region_id"])
        hospital_id = int(data["hospital_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "region_id, hospital_id, and code are required"}), 400
    if not code:
        return jsonify({"error": "region_id, hospital_id, and code are required"}), 400

    db = get_db()
    hospital = db.execute("SELECT * FROM hospitals WHERE id = ? AND region_id = ?", (hospital_id, region_id)).fetchone()
    if hospital is None:
        return jsonify({"error": "hospital not found in that region"}), 404

    # Ambulance starts stationed at its home hospital unless coordinates given.
    lat = data.get("lat")
    lon = data.get("lon")
    lat = float(lat) if lat not in (None, "") else hospital["lat"]
    lon = float(lon) if lon not in (None, "") else hospital["lon"]

    cur = db.execute(
        "INSERT INTO ambulances (hospital_id, region_id, code, status, lat, lon) VALUES (?, ?, ?, 'available', ?, ?)",
        (hospital_id, region_id, code, lat, lon),
    )
    db.commit()
    row = db.execute("SELECT * FROM ambulances WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row)), 201


# ---------------------------------------------------------------------------
# LoRa Receiver - Start in background thread
# ---------------------------------------------------------------------------
def start_lora_receiver():
    """Start the LoRa receiver in a background thread"""
    global lora_receiver, lora_thread

    # LORA_SERIAL_PORT (top of this file) is the single source of truth
    # for the COM port — no second env-var read here.
    lora_receiver = LoRaReceiver(port=LORA_SERIAL_PORT, baudrate=LORA_BAUDRATE)
    if lora_receiver.start():
        lora_thread = threading.current_thread()
        print(f"\n📡 LoRa Receiver started on {LORA_SERIAL_PORT}")
        return True
    else:
        print(f"\n⚠️  LoRa Receiver failed to start on {LORA_SERIAL_PORT}. The Flask app will continue without LoRa support.")
        return False


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    # Ensure database exists
    ensure_db()
    
    # Start LoRa receiver in background
    print("\n" + "=" * 50)
    print("🚑 RESCUE DASHBOARD - FLASK APPLICATION")
    print("=" * 50)
    
    # Start LoRa receiver (non-blocking). NOTE: the reloader is disabled
    # below (use_reloader=False) specifically so this only runs once —
    # Flask's debug reloader normally re-executes this whole __main__
    # block in a second process, which would try to open the same COM
    # port twice and fail with "Access is denied" / "port already in
    # use" on the second attempt.
    start_lora_receiver()
    
    print("\n" + "=" * 50)
    print("🌐 Starting Flask web server...")
    print("   Access the dashboard at: http://localhost:5000")
    print("   Admin page: http://localhost:5000/admin")
    print("   User page: http://localhost:5000/user")
    print("=" * 50 + "\n")
    
    # Run Flask app
    # use_reloader=False: the auto-reloader would otherwise re-run this
    # entire script in a second process on every save, which re-opens
    # the LoRa serial port and fails the second time ("Access is
    # denied" / "port already in use"). debug=True is kept for the
    # in-browser debugger and auto traceback pages; you just won't get
    # auto-restart-on-save. Restart manually (Ctrl+C, then `python
    # app.py` again) after editing this file.
    app.run(debug=True, use_reloader=False, host="0.0.0.0", port=5000)
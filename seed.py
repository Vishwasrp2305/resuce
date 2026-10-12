"""
Seeds the SQLite database with the four coverage regions and their
emergency facilities. Run once (app.py calls this automatically if the
DB file doesn't exist yet), or manually with: python seed.py
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "rescue.db")
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

# Region centers (approximate real-world coordinates)
REGIONS = [
    ("Mangalore", 12.9141, 74.8560),
    ("Padubidri", 13.1268, 74.7674),
    ("Udupi",     13.3409, 74.7421),
    ("Moodbidri", 13.0680, 74.9906),
]

# ---------------------------------------------------------------------------
# Telegram bots — one per place, hardcoded here instead of through the
# admin panel. Replace each placeholder token/chat_id with the real values
# from @BotFather (token) and the group's chat ID (e.g. via @RawDataBot or
# the bot's own getUpdates call) before deploying for real.
#   1. Message @BotFather -> /newbot -> copy the token it gives you.
#   2. Add that bot to the place's responder group, get the group chat ID.
#   3. Paste both below for the matching place name.
# ---------------------------------------------------------------------------
REGION_TELEGRAM_BOTS = {
    "Mangalore": {"token": "8677599303:AAEVWJEbKckL35Fo6SAHkzA_UkYkL9H9Ey8", "chat_id": "6346700821"},
    "Padubidri": {"token": "8677599303:AAEVWJEbKckL35Fo6SAHkzA_UkYkL9H9Ey8", "chat_id": "6346700821"},
    "Udupi":     {"token": "8677599303:AAEVWJEbKckL35Fo6SAHkzA_UkYkL9H9Ey8", "chat_id": "6346700821"},
    "Moodbidri": {"token": "8677599303:AAEVWJEbKckL35Fo6SAHkzA_UkYkL9H9Ey8", "chat_id": "6346700821"},
}

# name, lat_offset, lon_offset, phone
HOSPITALS = {
    "Mangalore": [
        ("Father Muller Medical College Hospital", 12.8926, 74.8489, "0824-2238000"),
        ("KMC Hospital Mangalore",                  12.8698, 74.8420, "0824-2444444"),
    ],
    "Padubidri": [
        ("Padubidri Community Health Centre",       13.1252, 74.7581, "0820-2560022"),
        ("Sri Sai Hospital Padubidri",               13.1310, 74.7702, "0820-2560101"),
    ],
    "Udupi": [
        ("Kasturba Medical College Hospital Udupi",  13.3389, 74.7477, "0820-2922761"),
        ("Dr. TMA Pai Hospital",                      13.3422, 74.7461, "0820-2529881"),
    ],
    "Moodbidri": [
        ("Moodbidri Government Hospital",            13.0705, 74.9885, "08258-236236"),
        ("Sri Dharmasthala Hospital Moodbidri",       13.0648, 74.9942, "08258-236500"),
    ],
}

POLICE = {
    "Mangalore": [
        ("Mangalore City Police Station (Kavoor)",   12.9083, 74.8544, "0824-2220500"),
        ("Bunder Police Station Mangalore",          12.8534, 74.8395, "0824-2440215"),
        ("Panambur Police Station",                  12.9482, 74.8123, "0824-2407100"),
        ("Surathkal Police Station",                 13.0113, 74.7967, "0824-2478100"),
    ],
    "Padubidri": [
        ("Padubidri Police Station",                 13.1245, 74.7658, "0820-2560033"),
        ("Kaup Police Outpost",                       13.2225, 74.7461, "0820-2586200"),
    ],
    "Udupi": [
        ("Udupi Town Police Station",                13.3417, 74.7448, "0820-2520100"),
        ("Manipal Police Station",                    13.3525, 74.7912, "0820-2571100"),
        ("Kalyanpur Police Station",                  13.3757, 74.7273, "0820-2545100"),
    ],
    "Moodbidri": [
        ("Moodbidri Police Station",                 13.0693, 74.9911, "08258-236100"),
        ("Bailoor Police Outpost",                    13.0512, 74.9743, "08258-236300"),
    ],
}

AMBULANCES = {
    "Mangalore": [("KA-19-AB-1001",), ("KA-19-AB-1002",), ("KA-19-AB-1003",), ("KA-19-AB-1004",), ("KA-19-AB-1005",)],
    "Padubidri": [("KA-20-AB-2001",), ("KA-20-AB-2002",), ("KA-20-AB-2003",), ("KA-20-AB-2004",)],
    "Udupi":     [("KA-20-AB-3001",), ("KA-20-AB-3002",), ("KA-20-AB-3003",), ("KA-20-AB-3004",), ("KA-20-AB-3005",)],
    "Moodbidri": [("KA-19-AB-4001",), ("KA-19-AB-4002",), ("KA-19-AB-4003",)],
}


def seed():
    conn = sqlite3.connect(DB_PATH)
    with open(SCHEMA_PATH, "r") as f:
        conn.executescript(f.read())

    region_ids = {}
    for name, lat, lon in REGIONS:
        # Each place's Telegram bot token + chat ID come from the hardcoded
        # REGION_TELEGRAM_BOTS map above instead of the admin panel.
        bot = REGION_TELEGRAM_BOTS.get(name, {})
        cur = conn.execute(
            "INSERT INTO regions (name, lat, lon, telegram_bot_token, telegram_chat_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, lat, lon, bot.get("token", ""), bot.get("chat_id", "")),
        )
        region_ids[name] = cur.lastrowid

    hospital_ids = {}  # region_name -> [hospital ids]
    for region_name, hospitals in HOSPITALS.items():
        hospital_ids[region_name] = []
        for name, lat, lon, phone in hospitals:
            cur = conn.execute(
                "INSERT INTO hospitals (region_id, name, lat, lon, phone) VALUES (?, ?, ?, ?, ?)",
                (region_ids[region_name], name, lat, lon, phone),
            )
            hospital_ids[region_name].append(cur.lastrowid)

    for region_name, stations in POLICE.items():
        for name, lat, lon, phone in stations:
            conn.execute(
                "INSERT INTO police_stations (region_id, name, lat, lon, phone) VALUES (?, ?, ?, ?, ?)",
                (region_ids[region_name], name, lat, lon, phone),
            )

    for region_name, ambs in AMBULANCES.items():
        h_ids = hospital_ids[region_name]
        for i, (code,) in enumerate(ambs):
            home_hospital = h_ids[i % len(h_ids)]
            hrow = conn.execute(
                "SELECT lat, lon FROM hospitals WHERE id = ?", (home_hospital,)
            ).fetchone()
            conn.execute(
                "INSERT INTO ambulances (hospital_id, region_id, code, status, lat, lon) "
                "VALUES (?, ?, ?, 'available', ?, ?)",
                (home_hospital, region_ids[region_name], code, hrow[0], hrow[1]),
            )

    conn.commit()
    conn.close()
    print(f"Seeded database at {DB_PATH}")


if __name__ == "__main__":
    seed()

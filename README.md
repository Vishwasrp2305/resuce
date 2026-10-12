# Smart Emergency Response System (SERS) — Flask + SQLite

Landing page → separate **User** and **Admin** apps, each with its own
live map, alert feed, dispatch status, and analytics — for the ESP32 +
LoRa SOS system. Covers four seeded regions — **Mangalore, Padubidri,
Udupi, Moodbidri** — each with hospitals, police stations, ambulances,
and its **own Telegram bot**.

## Run it

```bash
cd rescue_dashboard
pip install -r requirements.txt
python app.py
```

Open **http://localhost:5000** — that's the landing page. The database
(`rescue.db`) and its seed data are created automatically on first run;
on later runs the app auto-migrates an existing `rescue.db` in place
(adds the `users` table etc.) without losing your alert history. To
reseed from scratch instead: delete `rescue.db` and restart, or run
`python seed.py`.

The **LIVE / OFFLINE** badge in the header reflects the Flask process
itself — it goes red the moment the server stops responding and back to
green the moment it's reachable again, no page reload needed.

## Three pages

- **`/`** — landing page, heading **"Smart Emergency Response System"**,
  with two cards: **I need help** (→ `/user`) and **Admin** (→ `/admin`).
- **`/user`** — sign up or log in (name, phone, password — stored
  hashed in the `users` table), then you get: a map with a **Send SOS**
  button (uses your current location, a map click, or manual
  coordinates), a **Find Help** tab (nearest hospital/police/ambulance
  for a place name, coordinates, or your location), a **My Alerts** tab
  showing only the SOS events raised from your account, and an
  **Analytics** tab. Every account gets a numeric **ID** (shown next to
  your name in the header) that the admin can look up. **You don't have
  to be logged in to press Send SOS** — a guest press (or an SOS coming
  straight from the ESP32/Nano hardware, which never carries an
  account) still goes through; it's just tagged with a random
  **`Guest-####`** label instead of an account name, so it still shows
  up as a real reporter on the admin feed and map rather than blank.
- **`/admin`** — passcode gate (default `admin123`, set your own via the
  `ADMIN_TOKEN` environment variable, e.g. `ADMIN_TOKEN=my-secret python
  app.py`), then a full map plus tabs: **Alerts** (the live feed, with
  **Mark resolved** buttons — each alert shows who reported it: a logged-in
  user's name + ID, or a `Guest-####` label for anyone who fired an SOS
  without logging in or from the hardware directly), **+ Place / +
  Hospital / + Police / + Ambulance** (unchanged from before), a **Users**
  tab (every registered account, their ID, and how many alerts they've
  raised), and **Analytics**.

There's still a **"Simulate SOS"** button on the admin map — click it,
then click anywhere on the map to fire a test alert without hardware.

### Map tiles and non-English labels

By default the map uses plain `tile.openstreetmap.org` tiles (no key, no
watermark). These render each place's raw OpenStreetMap `name` tag as-is
with no language fallback — for large features like the Arabian Sea that
tag is sometimes in Arabic script rather than English. That's expected
upstream OSM behavior (every site using these tiles sees the same thing),
not a bug in this app, and it's cosmetic — it doesn't affect any of the
markers, alerts, or dispatch functionality.

If you'd rather have consistently English/Latin-script labels everywhere
(including at sea), request a free CARTO API key at
[carto.com/basemaps/apikey](https://carto.com/basemaps/apikey) (no card
needed, 5M tile requests/month free) and set it before starting the app:

```
CARTO_API_KEY=your-key-here python app.py
```

With that set, the map automatically switches to CARTO's Voyager basemap
instead. Without it, it falls back to the plain OSM tiles above.

## Analytics tab

Both `/user` and `/admin` have an **Analytics** tab: a line chart of
resolved alerts by date, and bar charts of alerts by hospital and by
place (region), each showing total vs. resolved. These are hand-drawn
inline SVG (`GET /api/analytics`) — no external charting library.

## Admin panel — add places, hospitals, police, ambulances

Unlocking the passcode gate on `/admin` reveals the full tool set as
tabs next to the map (no more slide-in drawer — the map and the tools
now share the screen at all times, which also fixes the map going stale
behind an animating drawer).

- **+ Place** — add a new coverage region (name + coordinates). There's
  still an optional Telegram token/chat ID field here if you want to wire
  a brand-new place's bot right away.
- **+ Hospital** / **+ Police** — add a facility to any existing place
  (including ones you just added).
- **+ Ambulance** — add a vehicle, tied to a home hospital in a place.

Every field with coordinates has a **"Pick on map"** button — click it
(the cursor turns into a crosshair over the map so it's obvious a click
is expected), then click the map to fill in the lat/lon and see a brief
highlight ring at the picked spot. New places, hospitals, police
stations, and ambulances all appear on the map **permanently** — places
show as amber dots, right alongside the existing hospital/police/
ambulance pins.

## Find nearby help — the `/user` page's Find Help tab

Enter a place name (matched against known places/hospitals/police
stations first, then falls back to OpenStreetMap's free geocoder), or
type coordinates, tap **"Use my current location,"** or **"Pick on
map."** Submitting shows the nearest hospital, police station, and an
available ambulance — each with distance and phone number — and plots
them on the map with a line back to your point.

### About the "Couldn't get your location" message

Browsers only allow the Geolocation API on a **secure context** — an
`https://` page, or `http://localhost` — never plain `http://` on a LAN
IP (e.g. `http://192.168.x.x:5000`). If you were opening this over
`http://` on a phone or another computer's IP, that's almost certainly
why it failed; the app now detects that case specifically and says so.
Location can also fail if the site's location permission was denied, or
if the device's Location/GPS service is off — the app now distinguishes
these and tells you which one it is instead of one generic error. Serve
the app over HTTPS (e.g. behind a reverse proxy with a certificate, or a
tool like `ngrok`/Tailscale Funnel) for geolocation to work from other
devices on the network.

## Telegram — one bot per place, set in code

Each place's Telegram bot token + chat ID are now **hardcoded directly in
`seed.py`** (the `REGION_TELEGRAM_BOTS` dict near the top) instead of
being entered through the admin panel — the admin panel's dedicated
"Telegram bots" tab has been removed. To wire up the four seeded places:

1. Message **@BotFather** on Telegram → `/newbot` → copy the token it gives you.
2. Add that bot to the Telegram group your responders use for that place, and get the group's chat ID (e.g. via `@RawDataBot` or the `getUpdates` API call after sending the bot a message).
3. Open `seed.py`, find `REGION_TELEGRAM_BOTS`, and replace that place's placeholder `token` / `chat_id` with the real values.
4. Delete `rescue.db` and restart (or run `python seed.py`) to reseed with the new bots.

Adding a **new** place through the admin panel still has an optional
token/chat-ID field if you want to set its bot immediately; otherwise you
can add it to `REGION_TELEGRAM_BOTS` and reseed, or use the existing
`PATCH /api/admin/regions/<id>/telegram` endpoint directly (e.g. via
curl) — that route is still there even though the admin UI tab is gone.

When an SOS comes in, the backend detects the victim's nearest place and
sends the alert **only to that place's own bot/group** — hospital name,
police station, ambulance code, and a Google Maps link — so the right
local responders see it, not everyone. Sending happens in a background
thread, so a slow or failed Telegram call never blocks or delays the SOS
response the ESP32 receives. Each alert card in the feed shows whether
its Telegram message was sent, failed, or has no bot configured yet for
that place.

## How an SOS flows through the system

1. Victim presses the SOS button → ESP32 reads GPS lat/lon from the NEO-6M.
2. ESP32 sends the alert over **LoRa** to the Arduino Nano receiver
   (always), and, if Wi-Fi is up, also POSTs it directly to this
   dashboard's `/api/sos` (see the Hardware section above).
3. In parallel, the Nano relays what it heard over LoRa to whoever's
   running `lora_bridge.py` on its Serial/USB connection, and that
   script also POSTs to `/api/sos` — so the alert lands even on a pure
   LoRa path with zero Wi-Fi at the victim's end.
4. `/api/sos`:
   - finds the nearest **place** (region)
   - finds the nearest **hospital** and **police station** by GPS distance
   - finds and dispatches the nearest **available** ambulance in that
     place (falls back to the nearest available ambulance anywhere)
   - saves the alert, fires that place's **Telegram bot** in the
     background, and returns the full assignment
5. The dashboard polls every few seconds and updates the map, alert feed,
   and status strip live — on *every* open `/user` and `/admin` page,
   on any computer, since they're all just polling this one server.

## API

| Method | Endpoint                                   | Purpose                                      |
|--------|----------------------------------------------|-----------------------------------------------|
| POST   | `/api/signup`                                 | Create a user account: `{name, phone, password}` |
| POST   | `/api/login`                                  | Log in: `{phone, password}`                    |
| POST   | `/api/sos`                                    | Ingest a new SOS: `{lat, lon, device_id, message, user_id}` (`user_id` optional) |
| GET    | `/api/alerts`                                 | Latest 100 alerts, newest first, incl. Telegram delivery status and reporting user. Add `?user_id=<id>` to filter to one account's alerts |
| POST   | `/api/alerts/<id>/resolve`                    | Mark an alert resolved, free its ambulance    |
| GET    | `/api/facilities`                             | All places/hospitals/police/ambulances        |
| POST   | `/api/lookup`                                 | Public: nearest hospital/police/ambulance for `{lat, lon}` or `{place}` |
| GET    | `/api/status`                                 | Counts + per-place totals                     |
| GET    | `/api/analytics`                              | Resolved-by-date, alerts-by-hospital, alerts-by-region, status counts |
| GET    | `/api/admin/verify`                           | Check an `X-Admin-Token` header is valid       |
| GET    | `/api/admin/users`                            | List every registered user, their ID, and alert count (admin) |
| POST   | `/api/admin/regions`                          | Add a place (admin)                            |
| PATCH  | `/api/admin/regions/<id>/telegram`            | Set/update a place's bot token + chat ID (admin, no UI tab anymore — call directly) |
| POST   | `/api/admin/hospitals`                        | Add a hospital to a place (admin)              |
| POST   | `/api/admin/police_stations`                  | Add a police station to a place (admin)        |
| POST   | `/api/admin/ambulances`                       | Add an ambulance to a hospital (admin)         |

All `/api/admin/*` routes require header `X-Admin-Token: <passcode>`.
`/api/lookup`, `/api/signup`, and `/api/login` are public — no token
required. User passwords are hashed with Werkzeug's `generate_password_hash`
before storage — never stored in plain text. This is still a demo-grade
auth layer with no sessions/cookies/CSRF protection (the browser just
remembers `{id, name, phone}` in `localStorage`) — fine for a small
trusted deployment, but swap in real sessions before this is public-facing.

## Hardware — ESP32 sender + Nano/LoRa receiver, wired into this dashboard

`hardware/` in this zip has the two sketches, both now integrated with
the dashboard so an SOS reaches it **two independent ways** — whichever
one has connectivity at the moment:

- **`hardware/esp32_sender/esp32_sender.ino`** — the victim's ESP32.
  Reads the NEO-6M GPS, and when the SOS button is pressed it (1) always
  sends the fix over **LoRa** to the Nano, and (2) if Wi-Fi is up, also
  **POSTs straight to this dashboard's `/api/sos`** and to Telegram — all
  three, every button press. Set `DASHBOARD_HOST` (and `DASHBOARD_PORT`
  if you're not using 5000) near the top of the file to the LAN IP of
  whichever computer runs `python app.py` — find it there with
  `ipconfig` / `ifconfig`. This is the fast path: as long as the ESP32
  has internet, the alert appears on both `/user` and `/admin` (red
  blinking marker + alert card) within a second or two of the button
  press, regardless of which computers those two pages happen to be
  open on — they're both just polling the same dashboard server.

- **`hardware/nano_receiver/nano_receiver.ino`** — the Arduino Nano +
  LoRa receiver. Unchanged behavior-wise (still logs every packet to
  Serial for the monitor), but now also prints one extra clean line per
  SOS packet — `$SOS,<lat>,<lon>,<rssi>` (or `$SOS,NOGPS,NOGPS,<rssi>`
  if the ESP32 had no GPS fix) — meant to be read by `lora_bridge.py`
  (below), not typed by a person.

- **`lora_bridge.py`** (project root) — run this on whichever computer
  the Nano is plugged into over USB. It watches the Nano's serial port
  for that `$SOS,...` line and POSTs it to the dashboard's `/api/sos`,
  same as the ESP32's own Wi-Fi path does. **This is the backup path**:
  it's what gets an SOS onto the dashboard even when the victim's ESP32
  has no Wi-Fi/internet at all — LoRa has much longer range than Wi-Fi,
  so this is often the path that actually works in the field.

  ```bash
  pip install -r bridge_requirements.txt
  # optional — otherwise the script lists ports and asks you to pick one
  export SERIAL_PORT=/dev/ttyUSB0        # or COM30 on Windows
  export DASHBOARD_URL=http://192.168.1.50:5000   # wherever app.py runs
  python lora_bridge.py
  ```

  `DASHBOARD_URL` doesn't have to point at `localhost` — the Nano, the
  dashboard server, and the browsers looking at `/user` and `/admin` can
  all be four different computers, as long as each can reach the
  dashboard server over the network (same LAN, or the internet if it's
  exposed via something like Tailscale Funnel or ngrok — see the HTTPS
  note above, which applies to browser geolocation, not to these two
  hardware paths).

Both paths call the exact same `/api/sos` endpoint the rest of this
README describes, so everything downstream (nearest hospital/police/
ambulance, the region's Telegram bot, the AI satisfaction prediction,
the live-polling map and alert feed) works identically no matter which
path an alert came in through — the admin feed just also shows which
`device_id` reported it (`ESP32-01` for the direct Wi-Fi path,
`NANO-LORA-BRIDGE` for the LoRa-relayed one) so responders can tell.

If you're flashing fresh boards: the LoRa settings (`433E6` frequency,
spreading factor 7, etc.) in both sketches must match each other — they
already do out of the box here.

## Notes / next steps worth considering

- **Admin passcode**: the built-in check is a single shared passcode —
  fine for a small trusted team, but move to real accounts before this
  goes anywhere public.
- **Ambulance GPS**: ambulance markers sit at their home hospital and
  only flip available/dispatched. Feed live tracker coordinates into the
  `ambulances` table if your vehicles carry their own GPS.
- **Geocoding**: the "Find Help" panel falls back to OpenStreetMap's free
  Nominatim API for place names it doesn't already know — that needs the
  server to have outbound internet access, and Nominatim's usage policy
  expects light, non-bulk traffic.
- **Production DB**: SQLite is fine for one dashboard instance; move to
  Postgres for multiple writers or higher reliability.

# LSC Smart Plug – local controller

A small Python program that controls one or more **LSC Smart Connect power
plugs with energy meter** (sold by Action; a Tuya device, protocol 3.5) directly
over your Wi-Fi. No Home Assistant, no Tuya cloud at runtime. One Python file, one HTML
page, a SQLite log. Runs on Windows, Linux, a Raspberry Pi or in Docker
(~120 MB image, ~40 MB RAM).

It exposes every native feature of the plug and adds the energy side the Tuya
app does badly: a local kWh log you own, cost, charts, per-device totals,
budget alerts and a monthly report.

## Features

**On the plug** (keep working when this program is off):

- On / off
- Countdown (switch after N minutes)
- Auto-off after switching on ("inching")
- Cycle timer (on X min / off Y min inside a time window)
- Random timer ("away mode")
- Relay status after a power cut: off / on / last state
- LED mode: on with relay, on when relay is off, always off, always on
  (this controls the red relay LED only; the blue Wi-Fi LED cannot be turned off)
- Child lock
- Overcharge protection (plug switches off when the load stays below ~3 W for
  40 minutes; leave it off for devices with standby, like an AC)
- Fault flags (overcurrent, overvoltage, overpower, …)

**In this program** (needs it running):

- Several plugs in one program: a tab per plug at the top of the page with its
  on/off state and live watts; rename plugs on the page
- "Turn on?" / "Turn off?" confirmation before switching, so a stray tap never
  switches a sensitive device (per plug, on by default; untick "Ask before
  switching on or off" under the button to switch with one tap)
- Live W / V / A, refreshed every 5 s
- Energy log: one row per poll in SQLite, kWh integrated from the readings
- Cost per hour, per 24 h, today, this month, total, plus a month projection
- Charts like the Tuya app: Day (per hour), Week (per weekday), Month (per day),
  with ‹ › navigation and "Compare with previous" grey bars
- Runtime today / 7 days / 30 days
- "Plugged in now" label: every reading is stored with the name of the device
  in the plug, so usage stays separate when you move the plug (AC in summer,
  heater in winter). Filter totals, chart and export per device. "Fix a stretch"
  relabels readings between two times if you forgot to change the name.
- Weekly on/off schedule at fixed times (the Tuya app runs these from the cloud;
  here the program runs them)
- Alerts on the page and to Discord (webhook) or ntfy: power above a limit, plug
  fault, plug unreachable, back online, monthly budget exceeded
- Monthly report on the 1st (kWh, cost, hours on, busiest day, per-device split)
- CSV export of a plug's readings (optionally one device) or of all plugs
- "Delete all measured data" of a plug (type RESET)
- Finds a plug again by itself if its IP changes
- Demo mode with two simulated plugs, so you can try the page without hardware

## Screens

The page is one column on a phone and two on a desktop:

| Left | Right |
|---|---|
| Live (switch, W/V/A, cost at this draw, fault) | Countdown |
| Energy & cost (totals, device label, chart, price, export, danger zone) | Auto-off |
| Alerts (limit, budget, monthly report, notify URL) | Cycle timer, Random timer |
| | Weekly schedule |
| | Plug settings (relay status, LED, child lock, overcharge) |

## Quick start

### Try it without a plug

```bash
pip install -r requirements.txt
python plug_server.py --demo
```

Open http://localhost:8081. Demo mode simulates two plugs (a 1.4 kW heater and
a 90 W fridge) and writes to separate `demo_*` files, so it never touches real
data.

### One-time setup: get the plug's local key

Tuya encrypts local traffic with a per-device **local key**. You fetch it once
from Tuya's cloud; after that the cloud is not used.

1. Install the **Smart Life** app, create an account, pair the plug
   (2.4 GHz Wi-Fi only).
2. Create a free developer account at https://iot.tuya.com:
   - Cloud → Create Cloud Project. Pick the data center of your region
     (Central Europe for the EU).
   - Devices → Link Tuya App Account → scan the QR code with Smart Life.
     The plug appears in the project.
   - Note the project's **Access ID** and **Access Secret**.
3. Run the wizard on your PC:

   ```bash
   pip install tinytuya
   python -m tinytuya wizard
   ```

   Enter Access ID, Access Secret, any device ID from the app's device info
   page and your region (`eu`). Say yes to the network scan. It writes
   `devices.json` and `snapshot.json` with the plug's `id`, `key`, `ip` and
   `version`.
4. Copy `config.example.json` to `config.json` and fill in `device_id`,
   `local_key`, `ip` and `version` (3.5 for this plug) for each plug. Delete the
   second entry of `plugs` if you have only one plug.
5. Give the plug a **DHCP reservation** in your router so its IP stays fixed.
   (The program can re-find it on a changed IP, but a reservation avoids gaps.)
6. Delete the wizard files afterwards (`tinytuya.json`, `devices.json`,
   `tuya-raw.json`, `snapshot.json` in your home folder). They contain the API
   secret and the key. You can also delete the cloud project; that does not
   reset the plug.

### Run it

```bash
python plug_server.py
```

Open http://localhost:8080, or `http://<this machine's IP>:8080` from a phone on
the same Wi-Fi. On Windows, `start_server.bat` does the same and installs the
packages on first run.

### Adding another plug later

1. Pair it in Smart Life, then rerun `python -m tinytuya wizard`. It lists every
   plug on the account with its `id`, `key` and `ip`.
2. Add an entry to `plugs` in `config.json` with a new `id` (e.g. `"2"`), the
   plug's `device_id`, `local_key`, `ip`, `version` and a `name`.
3. Restart the program. The plug gets its own tab; readings, schedules, the
   "plugged in now" label and the power alert are kept separately per plug.

Keep the `id` of a plug once it has data: readings are stored under it. An old
single-plug `config.json` (`device_id`, `local_key`, `ip` at the top level)
still works and is plug `"1"`; when you switch to the `plugs` list, give that
plug `"id": "1"` so it keeps its history. The first start after updating from
the single-plug version moves all existing readings and settings to plug `"1"`.

## Docker

`docker/` has a `Dockerfile` (python:3.14-alpine, runs as uid 1000) and a
`docker-compose.yml`. Layout on the host:

```
smart-plug/
  docker-compose.yml
  Dockerfile
  .dockerignore
  plug_server.py  index.html  requirements.txt
  data/            # volume: config.json (key!), state.json, energy.db
```

```bash
mkdir -p data && cp config.json data/
docker compose up -d --build
```

Notes:

- `DATA_DIR=/data` is where config, state and the database live. Mount it.
- Set `TZ` to your time zone; schedules, charts and the monthly report use it.
- The compose file attaches the container to an external `proxy-net` network for
  a reverse proxy (Nginx Proxy Manager, Traefik, …) and publishes no host port.
  Without a proxy, remove the network block and uncomment `ports`.
- The plug is found by a TCP sweep of its /24 on port 6668, so the container
  does not need host networking.
- Only events are logged (switches, alerts, IP changes), not every request:
  `docker logs smart-plug`.
- Keep `port` at 8080 in Docker: the healthcheck and the reverse proxy expect it.
- One container handles all plugs; there is no need for a second one.
- After a code change: copy `plug_server.py` / `index.html` and run
  `docker compose up -d --build`.
- Prebuilt image: `ghcr.io/ayhamo/lsc-smart-plug:latest` (amd64 and arm64),
  built by the GitHub workflow after the unit tests pass. It is rebuilt on every
  push and on the 1st of each month, picking up the newest python:3.14-alpine,
  Flask 3.x and tinytuya 1.x. A server using the image updates with
  `docker compose pull && docker compose up -d`; your `data/` is not touched.

**Stop any other copy of the program first.** Two programs polling the same
plug at once make it drop connections.

### Reverse proxy

Point a host at `http://smart-plug:8080` (container name) with websockets on.
The page has **no login**. Keep it reachable only from your LAN or VPN
(Tailscale, WireGuard). If you ever expose it to the internet, put an access
list or auth in front of it.

## How the energy numbers work

- **Readings.** Every 5 s the program asks each plug for W, V and A and stores
  them (each plug has its own thread, so a slow plug never delays another). Energy (Wh) is the power integrated over the time since the previous
  reading. Gaps longer than 3 polls (program off, plug unreachable) count as
  zero, so a gap never inflates the total.
- **Price.** Set your per-kWh price (brutto working price) on the page. The
  fixed monthly base fee is left out on purpose: you pay it whether the device
  runs or not.
- **"Logged X % of today"** shows how much of today was actually measured for
  this plug. Energy is only counted while the program runs and the plug answers.
- **Month projection** takes the average draw over the measured time this month
  and scales it to the whole month. It needs at least one hour of data; trust
  it after about a week.
- **Accuracy.** The plug's meter is roughly ±1–2 %. The plug's own draw
  (~0.5 W) is not measured. Wiring losses are well under 1 %. For a household
  bill comparison the number is as good as the meter.
- **Size.** One row every 5 s is about 17 000 rows a day per plug, roughly
  300 MB per plug and year. The CSV export streams, so even a big log exports
  without using much memory.
- **Page totals** are recalculated at most every 30 s; live W / V / A and cost
  per hour are always current.

## Plugged in now

Set the name of the device that is in the plug before you move it. Every reading
is stored with that label. "Show" filters totals and the chart to one device,
the per-device table lists runtime, kWh and cost for each, and the CSV export
has a `device` column. "Fix a stretch" relabels all readings between two times.

## Alerts, budget and report

In the Alerts box:

- **Power above N W** (per plug) → alert when the live draw exceeds it. It
  clears only below 95 % of the limit, so a load hovering around the limit does
  not alert again and again.
- **Monthly budget** → one alert per month when this month's cost (all plugs)
  reaches the limit. Checked every 5 minutes; remembered across restarts.
- **Monthly report** → on the first run in a new month, last month's kWh, cost,
  hours on, busiest day and the split per plug and device. If sending fails it
  is retried every 5 minutes for an hour. "Send report now" sends it immediately.
- **Notify URL** → a Discord webhook URL or an ntfy topic URL. "Send test"
  checks it. Also used for fault / unreachable / back-online / failed-schedule
  alerts. The URL works like a password, so the page and the API only show it
  masked (`https://discord.com/•••`); saving the form without retyping it keeps it.
- The same alert is sent at most once every 10 minutes (it is still listed on the
  page each time).

## HTTP API

Everything the page does goes through these endpoints, so you can script it
(curl, Home Assistant REST, a shortcut on your phone). Requests about one plug
take its id as `?plug=ID` (GET) or `"plug": "ID"` in the JSON body (POST);
without it they use the first plug. Errors come back as
`{"ok": false, "error": "…"}` with HTTP 400 (bad value), 404 (unknown plug or
schedule) or 502 (the plug did not answer).

| Method | Path | What |
|---|---|---|
| GET | `/api/plugs` | every plug: id, name, on, power_w, error |
| GET | `/api/status?plug=&device=` | live state, energy summary, schedules, alert settings, recent alerts, all plugs |
| GET | `/api/info?plug=` | plug name, model, ID, IP, protocol |
| GET | `/api/chart?plug=&range=day\|week\|month&date=YYYY-MM-DD&device=` | chart buckets, totals, previous period |
| GET | `/api/export/readings.csv?plug=&device=` | readings as CSV (all plugs without `plug`) |
| POST | `/api/switch` `{"plug":"2","on": true}` | switch |
| POST | `/api/device` `{"plug", "setting": "countdown\|power_on\|led\|child_lock\|overcurrent\|inching\|cycle\|random", "value": …}` | on-plug setting |
| POST | `/api/schedules` `{"plug","time":"07:00","action":"on","days":["mon",…]}` / `{"plug","delete": id}` / `{"plug","toggle": id}` | weekly schedule |
| POST | `/api/settings` `{"plug", "plug_name", "device", "power_above_w", "price_per_kwh", "budget_eur", "monthly_report", "notify_url"}` | any subset; the first three are per plug |
| POST | `/api/relabel` `{"plug","from":"YYYY-MM-DDTHH:MM","to":…,"device":"name"}` | relabel readings |
| POST | `/api/test-notify`, `/api/test-report` | send a test message / last month's report |
| POST | `/api/reset` `{"plug","confirm":"RESET"}` | delete all readings of a plug |

## Files

| File | What it is |
|---|---|
| `plug_server.py` | the program: Flask server, one poll loop per plug, schedules, alerts |
| `index.html` | the web page (no build step, no framework) |
| `config.example.json` | template; copy to `config.json` |
| `config.json` | plug IDs, IPs, **local keys – never share or commit** |
| `state.json` | plug names, schedules, price, device labels, alert settings (created automatically) |
| `energy.db` | SQLite log of readings (created automatically) |
| `demo_state.json`, `demo_energy.db` | demo-mode counterparts |
| `start_server.bat` | Windows one-click start |
| `docker/` | Dockerfile, docker-compose.yml, .dockerignore |
| `tests/` | unit tests: `python -m unittest discover -s tests -v` (simulated plugs, no network) |

`config.json` fields: `poll_seconds` (5), `port` (8080), `price_per_kwh` (only
the starting value; the page's price is kept in `state.json`), `title` (name in
notifications, default "Smart Plug") and `plugs`, a list with one entry per
plug: `id` (letters, digits, `-`, `_`; keep it once the plug has data), `name`
(changeable on the page later), `device_id`, `local_key`, `ip`, `version` and
optionally `dps` (data point numbers: switch 1, current 18, power 19, voltage
20). If the wizard's `snapshot.json` shows different DP numbers for your plug,
edit `dps`. Besides the four above, `dps` also accepts
`countdown` (9), `fault` (26), `power_on` (38), `overcurrent` (39), `led` (40),
`child_lock` (41), `cycle` (42), `random` (43) and `inching` (44); leave out any
you don't need to change.

## Things not to do

- **Don't remove the plug from the Smart Life app and don't delete that
  account** while the plug can reach the internet. Removing it sends a factory
  reset, which generates a new local key. You would have to pair again and rerun
  the wizard. Keeping the app is harmless: it, Google Home and this program can
  all control the plug at the same time.
- **Don't long-press the plug's button** (5 s+). Same reset.
- If your router can block a device's internet access, you *can* cut the cloud:
  confirm this program works, block the plug, and only then remove it from the
  app. The reset command never arrives and the key stays valid.

## Changing the router

The plug remembers the Wi-Fi name and password, not the router.

- Same name, same password, 2.4 GHz on: the plugs reconnect by themselves. They
  may get new IPs; the program finds each within about 30 s and updates
  `config.json`. Re-add the DHCP reservations.
- Different name or password: pair again in Smart Life, which creates a new
  local key. Rerun the wizard and update `config.json`.

## Limitations

- Fixed-time weekly schedules live in this program, not on the plug (same as
  the Tuya app, which runs them from the cloud). If the program is off, they
  don't fire. The on-plug timers (countdown, cycle, random, inching) do. A
  schedule missed by up to 15 minutes (slow plug, DST change) still runs, and a
  failed switch is retried 3 times, then alerted.
- Energy is only logged while the program runs. There is no import from the
  Tuya cloud to fill gaps.
- No authentication on the page.

## Running permanently without Docker

Windows: Task Scheduler, "at logon", run `pythonw plug_server.py` in this folder.

Linux / Raspberry Pi, a systemd unit:

```ini
[Unit]
Description=Smart plug controller
After=network.target
[Service]
WorkingDirectory=/home/pi/smart-plug
ExecStart=/usr/bin/python3 plug_server.py
Restart=always
[Install]
WantedBy=multi-user.target
```

## Thanks

Built on [tinytuya](https://github.com/jasonacox/tinytuya) and Flask.

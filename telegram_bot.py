"""Telegram alert bot (stdlib only).

* pushes a message + map pin whenever a box changes stage (AGING / CRITICAL) and on LOST, RECOVERED,
  ANOMALY, BATT_LOW, RESTART - with box id, product, temperature, predicted remaining shelf life,
  time in stage, last-seen, battery/RSSI, packets missed and the location of the receiver that heard it
* commands: /start <passcode>  /stop  /status  /box <id>  /where <id>  /dispatch  /alerts  /help
* reads the shared database, so it works with app.py, bridge.py or any other writer

Environment:
  TELEGRAM_BOT_TOKEN     from @BotFather (never commit it)
  PULSE_BOT_PASSCODE     users must send "/start <passcode>" to subscribe   (or)
  TELEGRAM_ALLOWED_CHATS comma-separated chat ids that may subscribe without a passcode
  PULSE_GATEWAYS         path to gateways.json (default: ./gateways.json)
  PULSE_BOT_KINDS        alert kinds to push (default AGING,CRITICAL,LOST,RECOVERED,ANOMALY,BATT_LOW,RESTART)
  PULSE_TZ_OFFSET_MIN    display time zone offset in minutes (default 330 = IST)
  TELEGRAM_API_BASE      override for testing (default https://api.telegram.org)
"""
import html
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from .protocol import STAGES

EMOJI = {"FRESH": "🟢", "AGING": "🟡", "CRITICAL": "🔴"}
KIND_ICON = {"AGING": "🟡", "CRITICAL": "🔴", "LOST": "📡", "RECOVERED": "✅", "ANOMALY": "⚠️",
             "BATT_LOW": "🔋", "RESTART": "🔄", "NEW_TAG": "🆕"}
ACTION = {
    "CRITICAL": "Quarantine this unit and inspect before any dispatch or sale.",
    "AGING": "Move up in dispatch priority (FEFO) and check the cold chain for this unit.",
    "LOST": "Locate the box - no heartbeat for 2 periods. Last known location below.",
    "RECOVERED": "Tag is reporting again. Verify the box condition.",
    "ANOMALY": "Stage went backwards without an authenticated restart - possible brown-out or tampering. Inspect.",
    "BATT_LOW": "Replace the tag's coin cell soon.",
    "RESTART": "Tag was reset with the password (reuse for next trip). Logged in the audit trail.",
    "NEW_TAG": "New tag registered.",
}
DEFAULT_KINDS = "AGING,CRITICAL,LOST,RECOVERED,ANOMALY,BATT_LOW,RESTART"


# ------------------------------------------------------------------ Telegram HTTP client
class Telegram:
    def __init__(self, token, base=None):
        self.url = f"{base or os.environ.get('TELEGRAM_API_BASE', 'https://api.telegram.org')}/bot{token}/"

    def call(self, method, timeout=35, **params):
        for attempt in range(3):
            req = urllib.request.Request(self.url + method, data=json.dumps(params).encode(),
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode()).get("result")
            except urllib.error.HTTPError as e:
                body = {}
                try:
                    body = json.loads(e.read().decode())
                except Exception:
                    pass
                if e.code == 429:
                    time.sleep(body.get("parameters", {}).get("retry_after", 2) + 0.5)
                    continue
                raise RuntimeError(f"Telegram {method} failed: {e.code} {body.get('description', '')}")
            except urllib.error.URLError:
                time.sleep(1 + attempt)
        raise RuntimeError(f"Telegram {method}: network unreachable")

    def send(self, chat_id, text):
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                         disable_web_page_preview=True)

    def send_location(self, chat_id, lat, lon):
        return self.call("sendLocation", chat_id=chat_id, latitude=lat, longitude=lon)


# ------------------------------------------------------------------ formatting
def fmt_duration(hours):
    if hours is None:
        return "n/a"
    if hours <= 0:
        return "0 h"
    d, h = divmod(int(round(hours)), 24)
    return f"{d} d {h} h" if d else f"{h} h"


class Formatter:
    def __init__(self, gateways=None, tz_offset_min=330):
        self.gateways = gateways or {}
        self.tz = timezone(timedelta(minutes=tz_offset_min))

    def when(self, ts):
        return datetime.fromtimestamp(ts, self.tz).strftime("%d %b %Y %H:%M")

    def location(self, store, box_id):
        loc = store.get_location(box_id)
        if not loc:
            return None
        g = self.gateways.get(loc["gateway"], {})
        return {"gateway": loc["gateway"], "name": g.get("name", loc["gateway"]),
                "lat": g.get("lat"), "lon": g.get("lon"), "ts": loc["ts"]}

    def box_lines(self, store, box_id, now):
        t = store.tag(box_id)
        if not t:
            return None, None
        pr = store.pred(box_id)
        rd = store.last_reading(box_id)
        loc = self.location(store, box_id)
        stage = STAGES[t["stage"]]
        lines = [f"Box ID: <code>{box_id:08X}</code>",
                 f"Product: {html.escape(str(t['product']))}",
                 f"Stage: {EMOJI[stage]} <b>{stage}</b>" + (f" (since {self.when(t['stage_since'])})" if t["stage_since"] else "")]
        if rd and rd["temp_c"] is not None:
            lines.append(f"Temperature: <b>{rd['temp_c']:.0f} °C</b> (at {self.when(rd['ts'])})")
        if pr is not None:
            rem = pr["remaining_h"]
            if t["stage"] == 2:
                lines.append("Remaining shelf life: <b>0 h - do not dispatch</b>")
            else:
                lines.append(f"Remaining shelf life: <b>{fmt_duration(rem)}</b> "
                             f"(≈ until {self.when(now + rem * 3600)} if kept at 5 °C)")
            lines.append(f"Prediction model: {pr['model']}")
        lines.append(f"Last heard: {self.when(t['last_seen'])}" + ("  📡 <b>LOST</b>" if t["lost"] else ""))
        lines.append(f"Battery: {'LOW 🔋' if t['batt_low'] else 'OK'} | RSSI: {t['rssi']} | "
                     f"packets: {t['pkts']} (missed {t['missed']}) | restarts: {t['wr_count']}")
        if loc:
            lines.append(f"Location: <b>{html.escape(loc['name'])}</b> (heard {self.when(loc['ts'])})")
            if loc["lat"] is not None:
                lines.append(f"Map: https://maps.google.com/?q={loc['lat']},{loc['lon']}")
        else:
            lines.append("Location: unknown (no receiver location reported yet)")
        return lines, loc

    def alert(self, store, a, now):
        lines, loc = self.box_lines(store, a["box_id"], now)
        head = f"{KIND_ICON.get(a['kind'], '🔔')} <b>{a['kind']}</b> - {html.escape(a['detail'])}"
        if lines is None:
            return head, None
        body = "\n".join(lines)
        return f"{head}\n\n{body}\n\n➡️ {ACTION.get(a['kind'], '')}", loc


# ------------------------------------------------------------------ notifier + commands
def parse_box_id(text):
    t = text.strip().lower().replace("0x", "")
    try:
        return int(t, 16)
    except ValueError:
        return None


class Notifier:
    def __init__(self, pipeline, token=None, gateways=None, kinds=None, passcode=None, allowed=None,
                 tg=None, tz_offset_min=None):
        self.pl, self.store = pipeline, pipeline.store
        token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.tg = tg or Telegram(token)
        if gateways is None:
            path = os.environ.get("PULSE_GATEWAYS", "gateways.json")
            gateways = json.load(open(path)) if os.path.exists(path) else {}
        self.fmt = Formatter(gateways, tz_offset_min if tz_offset_min is not None
                             else int(os.environ.get("PULSE_TZ_OFFSET_MIN", "330")))
        self.kinds = set((kinds or os.environ.get("PULSE_BOT_KINDS", DEFAULT_KINDS)).split(","))
        self.passcode = passcode or os.environ.get("PULSE_BOT_PASSCODE")
        env_allowed = os.environ.get("TELEGRAM_ALLOWED_CHATS", "")
        self.allowed = set(allowed or [int(x) for x in env_allowed.split(",") if x.strip()])
        self.stop = threading.Event()

    # ---- push alerts
    def init_cursor(self, replay=False):
        if self.store.get_state("last_alert_id") is None:
            self.store.set_state("last_alert_id", 0 if replay else self.store.max_alert_id())

    def push(self, chat, text, loc):
        self.tg.send(chat, text)
        if loc and loc.get("lat") is not None:
            self.tg.send_location(chat, loc["lat"], loc["lon"])

    def poll_alerts(self, now=None):
        """Send every new alert (once). Returns number of alerts sent."""
        self.init_cursor()
        last = int(self.store.get_state("last_alert_id", 0))
        sent = 0
        for a in self.store.alerts_after(last):
            if a["kind"] in self.kinds:
                text, loc = self.fmt.alert(self.store, a, now or time.time())
                for chat in self.store.subscribers():
                    try:
                        self.push(chat, text, loc)
                    except Exception as e:                      # one bad chat must not block the rest
                        print("telegram send failed:", e)
                sent += 1
            self.store.set_state("last_alert_id", a["id"])
        return sent

    # ---- commands
    def authorised(self, chat_id):
        return chat_id in self.store.subscribers() or chat_id in self.allowed

    def handle(self, chat_id, name, text):
        cmd, _, arg = text.strip().partition(" ")
        cmd = cmd.split("@")[0].lower()
        now = time.time()
        if cmd == "/start":
            if chat_id in self.allowed or (self.passcode and arg.strip() == self.passcode):
                self.store.add_subscriber(chat_id, name, now)
                return self.tg.send(chat_id, "✅ Subscribed to PULSE-Box alerts.\n"
                                    "Commands: /status /box &lt;id&gt; /where &lt;id&gt; /dispatch /alerts /stop /help")
            if not self.passcode and not self.allowed:
                return self.tg.send(chat_id, f"Bot not configured. Admin: set PULSE_BOT_PASSCODE or "
                                    f"TELEGRAM_ALLOWED_CHATS. Your chat id is <code>{chat_id}</code>.")
            return self.tg.send(chat_id, "Send <code>/start &lt;passcode&gt;</code> to subscribe.")
        if cmd == "/help":
            return self.tg.send(chat_id, "/start &lt;passcode&gt; - subscribe\n/stop - unsubscribe\n/status - all boxes\n"
                                "/box &lt;id&gt; - full details + map pin\n/where &lt;id&gt; - last location\n"
                                "/dispatch - FEFO priority list\n/alerts - last 5 alerts")
        if not self.authorised(chat_id):
            return self.tg.send(chat_id, "Not authorised. Send <code>/start &lt;passcode&gt;</code>.")
        if cmd == "/stop":
            self.store.remove_subscriber(chat_id)
            return self.tg.send(chat_id, "Unsubscribed.")
        if cmd == "/status":
            tags = self.store.tags()
            if not tags:
                return self.tg.send(chat_id, "No boxes reported yet.")
            rows = []
            for t in tags:
                pr = self.store.pred(t["box_id"]); rd = self.store.last_reading(t["box_id"])
                st = STAGES[t["stage"]]
                rem = "0 h" if t["stage"] == 2 else fmt_duration(pr["remaining_h"] if pr else None)
                temp = f"{rd['temp_c']:.0f}°C" if rd and rd["temp_c"] is not None else "-"
                rows.append(f"{EMOJI[st]} <code>{t['box_id']:08X}</code> {st} | {temp} | left {rem}"
                            + (" | 📡LOST" if t["lost"] else ""))
            c = [STAGES[t["stage"]] for t in tags]
            head = (f"<b>{len(tags)} boxes</b>: 🔴 {c.count('CRITICAL')}  🟡 {c.count('AGING')}  "
                    f"🟢 {c.count('FRESH')}  📡 {sum(bool(t['lost']) for t in tags)} lost\n\n")
            return self.tg.send(chat_id, head + "\n".join(rows[:40]))
        if cmd in ("/box", "/where"):
            bid = parse_box_id(arg) if arg else None
            if bid is None or not self.store.tag(bid):
                return self.tg.send(chat_id, "Usage: <code>/box B0A00001</code> (hex box id). Unknown or missing id.")
            lines, loc = self.fmt.box_lines(self.store, bid, now)
            self.tg.send(chat_id, "\n".join(lines) if cmd == "/box" else "\n".join(
                [l for l in lines if l.startswith(("Box ID", "Location", "Map", "Last heard"))]))
            if loc and loc.get("lat") is not None:
                self.tg.send_location(chat_id, loc["lat"], loc["lon"])
            return
        if cmd == "/dispatch":
            rank = self.pl.dispatch_ranking()[:10]
            if not rank:
                return self.tg.send(chat_id, "No boxes reported yet.")
            rows = [f"{r['rank']}. <code>{r['box_id']}</code> {EMOJI[r['stage']]} {r['action']}"
                    + ("" if r["remaining_days"] is None else f" - {r['remaining_days']} d left") for r in rank]
            return self.tg.send(chat_id, "<b>Dispatch priority (FEFO)</b>\n" + "\n".join(rows))
        if cmd == "/alerts":
            rows = self.store.alerts(5)
            if not rows:
                return self.tg.send(chat_id, "No alerts yet.")
            return self.tg.send(chat_id, "<b>Last alerts</b>\n" + "\n".join(
                f"{KIND_ICON.get(a['kind'], '🔔')} {self.fmt.when(a['ts'])} <code>{a['box_id']:08X}</code> "
                f"{html.escape(a['detail'])}" for a in rows))
        return self.tg.send(chat_id, "Unknown command. /help")

    def poll_updates(self, timeout=25):
        off = int(self.store.get_state("tg_offset", 0))
        req = urllib.request.Request(self.tg.url + "getUpdates",
                                     data=json.dumps({"offset": off, "timeout": timeout}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout + 10) as r:
            ups = json.loads(r.read().decode()).get("result", [])
        for u in ups:
            self.store.set_state("tg_offset", u["update_id"] + 1)
            m = u.get("message") or {}
            if "text" in m:
                try:
                    self.handle(m["chat"]["id"], m["chat"].get("first_name") or m["chat"].get("title") or "",
                                m["text"])
                except Exception as e:
                    print("command failed:", e)
        return len(ups)

    # ---- run loops
    def run_forever(self, alert_interval=3):
        threading.Thread(target=self._alert_loop, args=(alert_interval,), daemon=True).start()
        self._update_loop()

    def start_background(self, alert_interval=3):
        threading.Thread(target=self._alert_loop, args=(alert_interval,), daemon=True).start()
        threading.Thread(target=self._update_loop, daemon=True).start()

    def _alert_loop(self, interval):
        while not self.stop.is_set():
            try:
                self.poll_alerts()
            except Exception as e:
                print("alert poll error:", e)
            time.sleep(interval)

    def _update_loop(self):
        while not self.stop.is_set():
            try:
                self.poll_updates()
            except Exception as e:
                print("update poll error:", e)
                time.sleep(3)

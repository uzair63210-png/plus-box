"""Telegram alert bot (stdlib only).

* pushes a message + map pin whenever a box changes stage (AGING / CRITICAL) and on LOST, RECOVERED,
  ANOMALY, BATT_LOW, RESTART - with box id, product, temperature, predicted remaining shelf life,
  time in stage, last-seen, battery/RSSI, packets missed and the location of the receiver that heard it
* CRITICAL / LOST / ANOMALY alerts carry an [Acknowledge] button; if nobody acknowledges within
  PULSE_ESCALATE_MIN minutes the alert is re-sent as an ESCALATION (up to PULSE_ESCALATE_MAX times)
* bursts (>= PULSE_GROUP_MIN same-kind alerts in one batch, e.g. a whole truck going CRITICAL) are sent as ONE
  grouped message with one pin per receiver location instead of dozens of messages
* daily digest at PULSE_DIGEST_HOUR (local hour, e.g. 8)
* commands: /start <passcode>  /stop  /status  /box <id>  /where <id>  /product <id> <name>  /ask <question> (AI, read-only)
  /dispatch  /alerts  /help
* reads the shared database, so it works with app.py, bridge.py or any other writer

Environment:
  TELEGRAM_BOT_TOKEN     from @BotFather (never commit it)
  PULSE_BOT_PASSCODE     users must send "/start <passcode>" to subscribe   (or)
  TELEGRAM_ALLOWED_CHATS comma-separated chat ids that may subscribe without a passcode
  PULSE_GATEWAYS         path to gateways.json (default: ./gateways.json)
  PULSE_BOT_KINDS        alert kinds to push (default AGING,CRITICAL,LOST,RECOVERED,ANOMALY,BATT_LOW,RESTART,FREEZE,HEAT_LIMIT)
  PULSE_TZ_OFFSET_MIN    display time zone offset in minutes (default 330 = IST)
  PULSE_ESCALATE_MIN / PULSE_ESCALATE_MAX / PULSE_GROUP_MIN / PULSE_DIGEST_HOUR   see above
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
from .shelf_life import PRODUCTS, mkt_c
from .assistant import Assistant

EMOJI = {"FRESH": "🟢", "AGING": "🟡", "CRITICAL": "🔴"}
KIND_ICON = {"AGING": "🟡", "CRITICAL": "🔴", "LOST": "📡", "RECOVERED": "✅", "ANOMALY": "⚠️",
             "BATT_LOW": "🔋", "RESTART": "🔄", "NEW_TAG": "🆕", "FREEZE": "🧊", "HEAT_LIMIT": "🌡️"}
ACTION = {
    "CRITICAL": "Quarantine this unit and inspect before any dispatch or sale.",
    "AGING": "Move up in dispatch priority (FEFO) and check the cold chain for this unit.",
    "LOST": "Locate the box - no heartbeat for 2 periods. Last known location below.",
    "RECOVERED": "Tag is reporting again. Verify the box condition.",
    "ANOMALY": "Stage went backwards without an authenticated restart - possible brown-out or tampering. Inspect.",
    "BATT_LOW": "Replace the tag's coin cell soon.",
    "RESTART": "Tag was reset with the password (reuse for next trip). Logged in the audit trail.",
    "NEW_TAG": "New tag registered.",
    "FREEZE": "Freeze risk: quarantine and follow your freeze-damage SOP (e.g. shake test). Heat-based tag stage does not show this.",
    "HEAT_LIMIT": "Product limit exceeded (WHO PQS vaccine rule: never above +20 °C). Quarantine and check the vaccine vial monitor / SOP.",
}
ACK_KINDS = {"CRITICAL", "LOST", "ANOMALY", "FREEZE", "HEAT_LIMIT"}
DEFAULT_KINDS = "AGING,CRITICAL,LOST,RECOVERED,ANOMALY,BATT_LOW,RESTART,FREEZE,HEAT_LIMIT"


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

    def send(self, chat_id, text, markup=None):
        extra = {"reply_markup": markup} if markup else {}
        return self.call("sendMessage", chat_id=chat_id, text=text[:4000], parse_mode="HTML",
                         disable_web_page_preview=True, **extra)

    def answer_callback(self, cb_id, text):
        return self.call("answerCallbackQuery", callback_query_id=cb_id, text=text)

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
                rng = ""
                if pr.get("low_h") is not None and pr.get("high_h") is not None and pr["high_h"] - pr["low_h"] >= 1:
                    rng = f" (likely range {fmt_duration(pr['low_h'])} - {fmt_duration(pr['high_h'])})"
                lines.append(f"Remaining shelf life: <b>{fmt_duration(rem)}</b>{rng} "
                             f"- ≈ until {self.when(now + rem * 3600)} if kept at 5 °C")
            lines.append(f"Prediction model: {pr['model']}")
        temps = [r["temp_c"] for r in store.series(box_id, 300) if r["temp_c"] is not None]
        if len(temps) >= 2:
            lines.append(f"MKT: {mkt_c(temps):.1f} °C over {len(temps)} samples (heat-weighted; WHO Ea 83.1 kJ/mol)")
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

    def short_line(self, store, box_id, now):
        t = store.tag(box_id)
        if not t:
            return f"• <code>{box_id:08X}</code>"
        rd, pr, loc = store.last_reading(box_id), store.pred(box_id), self.location(store, box_id)
        temp = f"{rd['temp_c']:.0f}°C" if rd and rd["temp_c"] is not None else "-"
        rem = "0 h" if t["stage"] == 2 else fmt_duration(pr["remaining_h"] if pr else None)
        return (f"• <code>{box_id:08X}</code> {html.escape(str(t['product']))} | {temp} | left {rem}"
                + (f" | 📍{html.escape(loc['name'])}" if loc else ""))

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
                 tg=None, tz_offset_min=None, escalate_min=None, escalate_max=None, group_min=None,
                 digest_hour=None):
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
        self.escalate_s = 60 * float(escalate_min if escalate_min is not None
                                     else os.environ.get("PULSE_ESCALATE_MIN", "15"))
        self.escalate_max = int(escalate_max if escalate_max is not None
                                else os.environ.get("PULSE_ESCALATE_MAX", "3"))
        self.group_min = int(group_min if group_min is not None else os.environ.get("PULSE_GROUP_MIN", "4"))
        dh = digest_hour if digest_hour is not None else os.environ.get("PULSE_DIGEST_HOUR")
        self.digest_hour = None if dh in (None, "") else int(dh)
        self.assistant = Assistant(pipeline, gateways=self.fmt.gateways)
        self.stop = threading.Event()

    # ---- helpers
    def _markup(self, gid):
        return {"inline_keyboard": [[{"text": "✅ Acknowledge", "callback_data": f"ack:{gid}"}]]}

    def _pending(self):
        return json.loads(self.store.get_state("esc_pending", "{}"))

    def _save_pending(self, d):
        self.store.set_state("esc_pending", json.dumps(d))

    def broadcast(self, text, loc_list=(), markup=None):
        for chat in self.store.subscribers():
            try:
                self.tg.send(chat, text, markup)
                for lat, lon in loc_list:
                    self.tg.send_location(chat, lat, lon)
            except Exception as e:                      # one bad chat must not block the rest
                print("telegram send failed:", e)

    # ---- push alerts
    def init_cursor(self, replay=False):
        if self.store.get_state("last_alert_id") is None:
            self.store.set_state("last_alert_id", 0 if replay else self.store.max_alert_id())

    def poll_alerts(self, now=None):
        """Send every new alert once (grouped on bursts). Returns the number of alerts handled."""
        now = now or time.time()
        self.init_cursor()
        last = int(self.store.get_state("last_alert_id", 0))
        new = self.store.alerts_after(last)
        by_kind = {}
        for a in new:
            if a["kind"] in self.kinds:
                by_kind.setdefault(a["kind"], []).append(a)
        for kind, items in by_kind.items():
            if len(items) >= self.group_min:
                self._send_group(kind, items, now)
            else:
                for a in items:
                    self._send_single(a, now)
        if new:
            self.store.set_state("last_alert_id", new[-1]["id"])
        self.escalate(now)
        self.maybe_digest(now)
        return sum(len(v) for v in by_kind.values())

    def _track(self, gid, ids, text, locs, now):
        pend = self._pending()
        pend[str(gid)] = {"ids": ids, "n": 0, "t": now, "first": now, "text": text, "locs": locs}
        self._save_pending(pend)

    def _send_single(self, a, now):
        text, loc = self.fmt.alert(self.store, a, now)
        locs = [[loc["lat"], loc["lon"]]] if loc and loc.get("lat") is not None else []
        needs_ack = a["kind"] in ACK_KINDS
        self.broadcast(text, locs, self._markup(a["id"]) if needs_ack else None)
        if needs_ack:
            self._track(a["id"], [a["id"]], text, locs, now)

    def _send_group(self, kind, items, now):
        lines = [self.fmt.short_line(self.store, a["box_id"], now) for a in items[:30]]
        if len(items) > 30:
            lines.append(f"... and {len(items) - 30} more (use /status)")
        pins, seen = [], set()
        for a in items:
            loc = self.fmt.location(self.store, a["box_id"])
            if loc and loc.get("lat") is not None and loc["gateway"] not in seen and len(pins) < 3:
                seen.add(loc["gateway"]); pins.append([loc["lat"], loc["lon"]])
        gws = {(self.fmt.location(self.store, a["box_id"]) or {}).get("name") for a in items}
        hint = ""
        if len(gws) == 1 and None not in gws and kind in ("AGING", "CRITICAL", "FREEZE", "HEAT_LIMIT"):
            hint = (f"\n\n⚠️ <b>All {len(items)} boxes were heard at {html.escape(next(iter(gws)))}</b> - a shared cause "
                    "(refrigeration/compressor, power, door left open, vehicle) is more likely than individual boxes. "
                    "Check the room or vehicle first.")
        text = (f"{KIND_ICON.get(kind, '🔔')} <b>{len(items)} boxes - {kind}</b>\n\n" + "\n".join(lines) + hint
                + f"\n\n➡️ {ACTION.get(kind, '')}\nDetails: /box &lt;id&gt;")
        needs_ack = kind in ACK_KINDS
        gid = items[0]["id"]
        self.broadcast(text, pins, self._markup(gid) if needs_ack else None)
        if needs_ack:
            self._track(gid, [a["id"] for a in items], text, pins, now)

    # ---- escalation
    def escalate(self, now):
        pend = self._pending()
        changed, sent = False, 0
        for gid, e in list(pend.items()):
            if all(self.store.is_acked(i) for i in e["ids"]) or e["n"] >= self.escalate_max:
                del pend[gid]; changed = True
                continue
            if now - e["t"] >= self.escalate_s:
                e["n"] += 1; e["t"] = now; changed = True; sent += 1
                mins = int((now - e["first"]) / 60)
                self.broadcast(f"⏰ <b>ESCALATION {e['n']}/{self.escalate_max}</b> - not acknowledged for {mins} min\n\n"
                               + e["text"], [tuple(x) for x in e["locs"]], self._markup(gid))
        if changed:
            self._save_pending(pend)
        return sent

    def handle_callback(self, cb):
        data, who = cb.get("data", ""), cb["from"]
        name = who.get("first_name") or who.get("username") or str(who["id"])
        if not data.startswith("ack:"):
            return self.tg.answer_callback(cb["id"], "Unknown action")
        if not self.authorised(who["id"]):
            return self.tg.answer_callback(cb["id"], "Not authorised")
        gid = data[4:]
        ids = self._pending().get(gid, {}).get("ids") or [int(gid)]
        already = all(self.store.is_acked(i) for i in ids)
        for i in ids:
            self.store.ack_alert(i)
        self.tg.answer_callback(cb["id"], "Already acknowledged" if already else "Acknowledged ✅")
        if not already:
            self.broadcast(f"✅ Alert{'s' if len(ids) > 1 else ''} {', '.join('#' + str(i) for i in ids[:6])}"
                           f"{' ...' if len(ids) > 6 else ''} acknowledged by <b>{html.escape(name)}</b> - escalation stopped.")

    # ---- digest
    def status_text(self):
        tags = self.store.tags()
        if not tags:
            return "No boxes reported yet."
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
        return head + "\n".join(rows[:40])

    def maybe_digest(self, now):
        if self.digest_hour is None:
            return False
        local = datetime.fromtimestamp(now, self.fmt.tz)
        key = local.strftime("%Y-%m-%d")
        if local.hour < self.digest_hour or self.store.get_state("digest_date") == key:
            return False
        self.store.set_state("digest_date", key)
        open_alerts = [a for a in self.store.alerts(200) if a["kind"] in ACK_KINDS and not a["acknowledged"]]
        self.broadcast(f"🗓 <b>Daily digest {local.strftime('%d %b %Y')}</b>\n\n{self.status_text()}\n\n"
                       f"Unacknowledged critical alerts: <b>{len(open_alerts)}</b>")
        return True

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
                                    "Commands: /status /box &lt;id&gt; /where &lt;id&gt; /product &lt;id&gt; &lt;name&gt; "
                                    "/dispatch /alerts /stop /help")
            if not self.passcode and not self.allowed:
                return self.tg.send(chat_id, f"Bot not configured. Admin: set PULSE_BOT_PASSCODE or "
                                    f"TELEGRAM_ALLOWED_CHATS. Your chat id is <code>{chat_id}</code>.")
            return self.tg.send(chat_id, "Send <code>/start &lt;passcode&gt;</code> to subscribe.")
        if cmd == "/help":
            return self.tg.send(chat_id, "/start &lt;passcode&gt; - subscribe\n/stop - unsubscribe\n/status - all boxes\n"
                                "/box &lt;id&gt; - full details + map pin\n/where &lt;id&gt; - last location\n"
                                "/product &lt;id&gt; &lt;name&gt; - set product (dairy, frozen, vaccine, packaged)\n"
                                "/ask &lt;question&gt; - AI assistant (read-only)\n/dispatch - FEFO priority list\n/alerts - last 5 alerts")
        if not self.authorised(chat_id):
            return self.tg.send(chat_id, "Not authorised. Send <code>/start &lt;passcode&gt;</code>.")
        if cmd == "/stop":
            self.store.remove_subscriber(chat_id)
            return self.tg.send(chat_id, "Unsubscribed.")
        if cmd == "/status":
            return self.tg.send(chat_id, self.status_text())
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
        if cmd == "/product":
            parts = arg.split()
            bid = parse_box_id(parts[0]) if len(parts) == 2 else None
            if bid is None or not self.store.tag(bid) or parts[1].lower() not in PRODUCTS:
                return self.tg.send(chat_id, "Usage: <code>/product B0A00001 vaccine</code>\nProducts: "
                                    + ", ".join(sorted(PRODUCTS)))
            hours = self.pl.set_product(bid, parts[1].lower())
            return self.tg.send(chat_id, f"✅ <code>{bid:08X}</code> is now <b>{parts[1].lower()}</b>. "
                                f"Remaining shelf life recalculated: <b>{fmt_duration(hours)}</b>.")
        if cmd == "/ask":
            if not arg.strip():
                return self.tg.send(chat_id, "Usage: <code>/ask which boxes at cold-room-1 will expire first?</code>")
            if not self.assistant.enabled:
                return self.tg.send(chat_id, "AI assistant is off. Admin: set ANTHROPIC_API_KEY.")
            self.tg.send(chat_id, "🤔 checking the data ...")
            ans = self.assistant.ask(arg, chat_id)
            return self.tg.send(chat_id, "🤖 " + html.escape(ans) + "\n\n<i>AI answer from live data; estimates are model "
                                "outputs. Follow your SOP for sell/discard decisions.</i>")
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
                f"{html.escape(a['detail'])}" + (" ✅" if a["acknowledged"] else "") for a in rows))
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
            try:
                if "callback_query" in u:
                    self.handle_callback(u["callback_query"])
                    continue
                m = u.get("message") or {}
                if "text" in m:
                    self.handle(m["chat"]["id"], m["chat"].get("first_name") or m["chat"].get("title") or "",
                                m["text"])
            except Exception as e:
                print("update handling failed:", e)
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

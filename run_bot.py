"""Run the Telegram bot on its own (shares the database with app.py / bridge.py).

PowerShell:
  $env:TELEGRAM_BOT_TOKEN = "123456:ABC..."        # from @BotFather
  $env:PULSE_BOT_PASSCODE = "choose-a-secret"
  python run_bot.py                                # then open the bot in Telegram: /start choose-a-secret
  python run_bot.py --test <chat_id>               # send one sample alert to check the setup
  python run_bot.py --replay                       # also send alerts that already exist in the DB
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from pulse_rx.db import Store
from pulse_rx.service import Pipeline
from pulse_rx.telegram_bot import Notifier

ap = argparse.ArgumentParser()
ap.add_argument("--db", default=os.environ.get("DATABASE_URL", "sqlite:///pulse_rx.db"))
ap.add_argument("--replay", action="store_true")
ap.add_argument("--test", type=int, metavar="CHAT_ID")
a = ap.parse_args()

if not os.environ.get("TELEGRAM_BOT_TOKEN"):
    sys.exit("Set TELEGRAM_BOT_TOKEN first (create a bot with @BotFather).")
pl = Pipeline(Store(a.db), train_if_missing=False)
bot = Notifier(pl)
if not (bot.passcode or bot.allowed):
    print("WARNING: set PULSE_BOT_PASSCODE (or TELEGRAM_ALLOWED_CHATS) - nobody can subscribe otherwise.")

if a.test:
    from pulse_rx.protocol import Packet
    pl.ingest_packet(Packet(0xB0A0FFFF, 0, 1, False, False, False, 6, 0), time.time(), "cold-room-1")
    pl.ingest_packet(Packet(0xB0A0FFFF, 2, 1, False, False, False, 41, 1), time.time() + 1, "cold-room-1")
    t = pl.store.alerts(1)[0]
    text, loc = bot.fmt.alert(pl.store, t, time.time())
    bot.push(a.test, "🧪 TEST MESSAGE\n\n" + text, loc)
    sys.exit("test alert sent")

bot.init_cursor(replay=a.replay)
print("Bot running. Press Ctrl+C to stop.")
try:
    bot.run_forever()
except KeyboardInterrupt:
    pass

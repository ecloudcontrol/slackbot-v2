import os
import re
import json
import logging
from datetime import datetime
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ===============================
# CONFIG
# ===============================
LOG_FILE = os.environ.get("LOG_FILE", "/appz/log/slackbot.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE)]
)

APP_TOKEN = os.environ["APP_TOKEN"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
TARGET_CHANNEL = os.environ["TARGET_CHANNEL_ID"]
CHANNEL_IDS = [c.strip() for c in os.environ["CHANNEL_IDS"].split(",")]

PATTERN_FILE = os.environ.get("PATTERNS_PATH", "/appz/scripts/webapps/patterns.json")

# ===============================
# LOAD PATTERNS
# ===============================
with open(PATTERN_FILE) as f:
    patterns = json.load(f)

INCLUDE_PATTERNS = [re.compile(p, re.I) for p in patterns["include_patterns"]]
EXCLUDE_PATTERNS = [re.compile(p, re.I) for p in patterns.get("exclude_patterns", [])]

# ===============================
# ALERT STATE
# ===============================
alert_state = {}
# structure:
# {
#   "alert_name": {
#       "triggered": bool,
#       "retriggered": bool,
#       "recovered": bool,
#       "sources": set(),
#       "message_ts": str
#   }
# }

# ===============================
# HELPERS
# ===============================
def extract_alert(text):
    m = re.search(r"(Triggered|Re-Triggered|Recovered):\s*(.+)", text, re.I)
    if not m:
        return None, None
    return m.group(1).title(), m.group(2).strip()


def should_forward(alert, state, channel):
    entry = alert_state.setdefault(alert, {
        "triggered": False,
        "retriggered": False,
        "recovered": False,
        "sources": set(),
        "message_ts": None
    })

    entry["sources"].add(channel)

    if state == "Triggered":
        if entry["triggered"]:
            return False
        entry["triggered"] = True
        return True

    if state == "Re-Triggered":
        if not entry["triggered"] or entry["retriggered"]:
            return False
        entry["retriggered"] = True
        return True

    if state == "Recovered":
        if not entry["triggered"] or entry["recovered"]:
            return False
        entry["recovered"] = True
        return True

    return False


def format_sources(alert):
    return ", ".join(f"<#{c}>" for c in sorted(alert_state[alert]["sources"]))


# ===============================
# SLACK APP
# ===============================
app = App(token=BOT_TOKEN)


@app.message(re.compile("|".join(p.pattern for p in INCLUDE_PATTERNS)))
def handle_message(message, say):
    text = message.get("text", "")
    channel = message["channel"]
    ts = message["ts"]

    if channel not in CHANNEL_IDS:
        return

    if any(p.search(text) for p in EXCLUDE_PATTERNS):
        return

    state, alert = extract_alert(text)
    if not state:
        return

    if not should_forward(alert, state, channel):
        return

    permalink = app.client.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]

    sources = format_sources(alert)
    content = f"<{permalink}|{text}>\nSources: {sources}"

    blocks = [{
        "type": "section",
        "text": {"type": "mrkdwn", "text": content}
    }]

    # Add button only on recovery
    if state == "Recovered":
        blocks.append({
            "type": "actions",
            "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "Have you fixed it?"},
                "action_id": "confirm_recovery"
            }]
        })

    res = app.client.chat_postMessage(
        channel=TARGET_CHANNEL,
        blocks=blocks,
        unfurl_links=False
    )

    alert_state[alert]["message_ts"] = res["ts"]


@app.action("confirm_recovery")
def handle_confirm(ack, body, client):
    ack()
    channel = body["channel"]["id"]
    ts = body["message"]["ts"]

    client.reactions_add(
        channel=channel,
        timestamp=ts,
        name="white_check_mark"
    )


# ===============================
# START
# ===============================
if __name__ == "__main__":
    logging.info("🚀 Alert aggregation service started")
    SocketModeHandler(app, APP_TOKEN).start()

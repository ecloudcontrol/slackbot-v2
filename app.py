import os
import re
import json
import logging
from datetime import datetime
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# =====================================================
# CONFIG
# =====================================================
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

# =====================================================
# LOAD PATTERNS
# =====================================================
with open(PATTERN_FILE) as f:
    data = json.load(f)

INCLUDE_PATTERNS = [re.compile(p, re.I) for p in data["include_patterns"]]
EXCLUDE_PATTERNS = [re.compile(p, re.I) for p in data.get("exclude_patterns", [])]

# =====================================================
# STATE MANAGEMENT
# =====================================================
# alert_name -> { triggered, retriggered, recovered }
alert_state = {}

# =====================================================
# HELPERS
# =====================================================
def extract_alert(text):
    m = re.search(r"(Triggered|Re-Triggered|Recovered):\s*(.+)", text, re.I)
    if not m:
        return None, None
    return m.group(1).title(), m.group(2).strip()


def should_forward(alert, state):
    state = state.lower()
    entry = alert_state.setdefault(alert, {
        "triggered": False,
        "retriggered": False,
        "recovered": False
    })

    # Triggered
    if state == "triggered":
        if entry["triggered"]:
            return False
        entry["triggered"] = True
        return True

    # Re-triggered
    if state == "re-triggered":
        if not entry["triggered"] or entry["retriggered"]:
            return False
        entry["retriggered"] = True
        return True

    # Recovered
    if state == "recovered":
        if not entry["triggered"] or entry["recovered"]:
            return False
        entry["recovered"] = True
        return True

    return False


def get_permalink(app, channel, ts):
    try:
        return app.client.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]
    except Exception:
        return ""


# =====================================================
# SLACK APP
# =====================================================
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

    if not should_forward(alert, state):
        return

    permalink = get_permalink(app, channel, ts)

    blocks = [{
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"<{permalink}|{text}>"
        }
    }]

    # Show button only for recovered
    if state == "Recovered":
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Have you fixed it?"},
                    "action_id": "confirm_recovery"
                }
            ]
        })

    app.client.chat_postMessage(
        channel=TARGET_CHANNEL,
        blocks=blocks,
        unfurl_links=False
    )


@app.action("confirm_recovery")
def handle_confirm(ack, body, client):
    ack()
    channel = body["channel"]["id"]
    ts = body["message"]["ts"]

    client.reactions_add(
        channel=channel,
        name="white_check_mark",
        timestamp=ts
    )


# =====================================================
# START BOT
# =====================================================
if __name__ == "__main__":
    logging.info("🚀 Alert forwarding bot started")
    SocketModeHandler(app, APP_TOKEN).start()

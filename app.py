import os
import sys
import re
import json
import logging
from datetime import datetime, timedelta
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ---------------- CONFIGURATION ---------------- #
LOG_FILE = os.environ.get("LOG_FILE", "/appz/log/slackbot.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")]
)
logger = logging.getLogger(__name__)

# ---------------- ENV ---------------- #
REQUIRED_ENVS = ["APP_TOKEN", "BOT_TOKEN", "TARGET_CHANNEL_ID", "CHANNEL_IDS"]
env = {k: os.environ.get(k) for k in REQUIRED_ENVS}
missing = [k for k, v in env.items() if not v]
if missing:
    logger.error(f"Missing environment variables: {missing}")
    sys.exit(1)

app_token = env["APP_TOKEN"]
bot_token = env["BOT_TOKEN"]
target_channel_id = env["TARGET_CHANNEL_ID"]
channel_ids = [c.strip() for c in env["CHANNEL_IDS"].split(",") if c.strip()]

# ---------------- PATTERNS ---------------- #
patterns_path = os.environ.get("PATTERNS_PATH", "/appz/scripts/webapps/patterns.json")

def load_patterns(path):
    try:
        with open(path) as f:
            data = json.load(f)
            return data.get("include_patterns", []), data.get("exclude_patterns", [])
    except Exception as e:
        logger.error(f"Pattern load failed: {e}")
        sys.exit(1)

include_patterns, exclude_patterns = load_patterns(patterns_path)
INCLUDE_REGEX = [re.compile(p, re.IGNORECASE) for p in include_patterns]
EXCLUDE_REGEX = [re.compile(p, re.IGNORECASE) for p in exclude_patterns]

# ---------------- SLACK APP ---------------- #
app = App(token=bot_token)

# ---------------- CONSTANTS ---------------- #
TIME_WINDOWS = {
    "Triggered": timedelta(minutes=60),
    "Re-Triggered": timedelta(minutes=60),
    "Warn": timedelta(minutes=15),
    "Recovered": timedelta(minutes=5),
}

ALERT_REGEX = re.compile(
    r'(?i)\b(triggered|recovered|re-triggered|warn)\b\s*:?[\s\-]*(.+)'
)

# ---------------- CACHE ---------------- #
recent_messages_cache = {}

def cleanup_cache(ttl_minutes=1440):
    now = datetime.utcnow()
    for k in list(recent_messages_cache.keys()):
        if now - recent_messages_cache[k]["first_seen"] > timedelta(minutes=ttl_minutes):
            del recent_messages_cache[k]

# ---------------- HELPERS ---------------- #
def extract_alert(text):
    m = ALERT_REGEX.search(text)
    if not m:
        return None, None
    return m.group(1).title(), m.group(2).strip()


def should_forward(alert, state, channel):
    now = datetime.utcnow()
    entry = recent_messages_cache.setdefault(alert, {
        "states": {},
        "channels": set(),
        "active": False,
        "first_seen": now
    })

    # 🔒 Prevent duplicate triggers across channels
    if state == "Triggered" and entry["active"]:
        return False

    # Prevent duplicate recovery
    if state == "Recovered" and not entry["active"]:
        return False

    last_seen = entry["states"].get(state)
    if last_seen and (now - last_seen) <= TIME_WINDOWS.get(state, timedelta(minutes=5)):
        return False

    entry["states"][state] = now
    entry["channels"].add(channel)

    if state == "Triggered":
        entry["active"] = True
    elif state == "Recovered":
        entry["active"] = False

    return True


def get_permalink(channel, ts):
    try:
        res = app.client.chat_getPermalink(channel=channel, message_ts=ts)
        return res["permalink"]
    except Exception:
        return f"slack://channel?channel={channel}&message_ts={ts}"


def send_to_target(original_message, channel_id, message_ts, state, alert_name):
    permalink = get_permalink(channel_id, message_ts)
    sources = format_sources(alert_name)

    text = f"<{permalink}|{original_message}>\nSources: {sources}"

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        }
    ]

    # ✅ Add interactive button ONLY for non-recovered alerts
    if state != "Recovered":
        blocks[0]["accessory"] = {
            "type": "button",
            "text": {"type": "plain_text", "text": "Have you fixed it?"},
            "action_id": "button_click",
            "value": json.dumps({
                "channel_id": channel_id,
                "message_ts": message_ts
            })
        }

    try:
        app.client.chat_postMessage(
            channel=target_channel_id,
            blocks=blocks,
            attachments=[
                {
                    "color": STATE_COLORS.get(state, "#CCCCCC")
                }
            ],
            unfurl_links=False,
        )
    except Exception as e:
        logger.error(f"Send failed: {e}")


# ---------------- MESSAGE HANDLERS ---------------- #
@app.message(re.compile("|".join(include_patterns), re.IGNORECASE))
def handle_text(message, say):
    text = message.get("text", "")
    handle_alert(text, message["channel"], message["ts"])


@app.event("message")
def handle_attachments(event, say):
    if event.get("subtype") == "message_deleted":
        return
    if event.get("channel") not in channel_ids:
        return

    for att in event.get("attachments", []):
        text = att.get("text") or att.get("fallback")
        if not text:
            continue
        if any(r.search(text) for r in INCLUDE_REGEX):
            handle_alert(text, event["channel"], event["ts"])


def handle_alert(text, channel, ts):
    state, alert = extract_alert(text)
    if not state or not alert:
        return

    if any(r.search(text) for r in EXCLUDE_REGEX):
        return

    if not should_forward(alert, state, channel):
        return

    send_alert(alert, channel, ts, state)
    cleanup_cache()


@app.action("mark_fixed")
def mark_fixed(ack, body, client):
    ack()
    data = json.loads(body["actions"][0]["value"])
    channel, ts = data["channel"], data["ts"]

    try:
        result = client.conversations_history(channel=channel, latest=ts, inclusive=True, limit=1)
        original = result["messages"][0]["text"]
        client.chat_update(channel=channel, ts=ts, text=f"✅ {original}")
    except Exception as e:
        logger.error(f"Failed to update message: {e}")


# ---------------- START ---------------- #
if __name__ == "__main__":
    logger.info("🚀 Slack Alert Forwarder started")
    SocketModeHandler(app, app_token).start()

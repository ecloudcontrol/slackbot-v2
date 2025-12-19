import os
import sys
import re
import json
import logging
from datetime import datetime
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ---------------- LOGGING ---------------- #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler("/appz/log/slackbot.log", mode="a", encoding="utf-8")]
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ---------------- ENV ---------------- #

app_token = os.environ.get("APP_TOKEN")
bot_token = os.environ.get("BOT_TOKEN")
target_channel_id = os.environ.get("TARGET_CHANNEL_ID")
channel_ids = [c.strip() for c in os.environ.get("CHANNEL_IDS", "").split(",") if c.strip()]

if not all([app_token, bot_token, target_channel_id, channel_ids]):
    logger.error("Missing required environment variables. Aborting...")
    sys.exit(1)

# ---------------- SLACK APP ---------------- #

app = App(token=bot_token)

# ---------------- CACHE ---------------- #

recent_messages_cache = {}

# ---------------- ALERT REGEX ---------------- #

ALERT_REGEX = re.compile(
    r'(?i)(Triggered|Recovered|Re-Triggered|Warn):\s*(?:\[[^\]]+\]\s*)*(.+)'
)

# ---------------- PATTERN LOADING ---------------- #

def load_filter_patterns(path):
    try:
        with open(path) as f:
            data = json.load(f)
            logger.info("patterns.json loaded successfully")
            return data.get("include_patterns", []), data.get("exclude_patterns", [])
    except Exception as e:
        logger.error(f"Failed to load filter patterns: {e}")
        sys.exit(1)

include_patterns, exclude_patterns = load_filter_patterns(
    "/appz/scripts/webapps/patterns.json"
)

# ---------------- HELPERS ---------------- #

def extract_alert(text):
    match = ALERT_REGEX.search(text)
    if not match:
        return None, None
    alert_name = match.group(2).split("\n")[0].strip()
    return match.group(1), alert_name


def should_forward_alert(alert_name, state, channel_id):
    entry = recent_messages_cache.get(alert_name)

    if not entry:
        entry = {
            "triggered": False,
            "retriggered": False,
            "warn": False,
            "recovered": False,
            "channels": set(),
            "first_seen": datetime.now()
        }
        recent_messages_cache[alert_name] = entry

    entry["channels"].add(channel_id)

    key_map = {
        "Triggered": "triggered",
        "Re-Triggered": "retriggered",
        "Warn": "warn",
        "Recovered": "recovered"
    }

    key = key_map.get(state)
    if key and entry[key]:
        return False

    if key:
        entry[key] = True
        return True

    return False


def format_sources(alert_name):
    channels = recent_messages_cache.get(alert_name, {}).get("channels", [])
    return ", ".join(f"<#{cid}>" for cid in sorted(channels))


def get_channel_name(channel_id):
    try:
        resp = app.client.conversations_info(channel=channel_id)
        return resp["channel"]["name"]
    except Exception:
        return channel_id


def send_to_target(original_message, channel_id, message_ts, state, alert_name):
    channel_name = get_channel_name(channel_id)

    permalink = app.client.chat_getPermalink(
        channel=channel_id,
        message_ts=message_ts
    )["permalink"]

    final_message = (
        f"*{original_message}*\n"
        f"Sources: {format_sources(alert_name)}\n"
        f"Link: <{permalink}|View message>\n"
        f"Channel: <#{channel_id}|{channel_name}>"
    )

    color_map = {
        "Triggered": "#E01E5A",
        "Re-Triggered": "#E01E5A",
        "Warn": "#ECB22E",
        "Recovered": "#2EB67D"
    }

    app.client.chat_postMessage(
        channel=target_channel_id,
        attachments=[{
            "color": color_map.get(state, "#CCCCCC"),
            "text": final_message,
            "mrkdwn_in": ["text"]
        }],
        unfurl_links=False
    )

# ---------------- CORE ---------------- #

def handle_alert(text, channel_id, ts):
    state, alert_name = extract_alert(text)
    if not state:
        return

    if not should_forward_alert(alert_name, state, channel_id):
        logger.info(f"Suppressed (merged): {state} | {alert_name}")
        return

    send_to_target(text, channel_id, ts, state, alert_name)
    logger.info(f"Forwarded: {state} | {alert_name}")

    if state == "Recovered":
        recent_messages_cache.pop(alert_name, None)

# ---------------- MESSAGE HANDLERS ---------------- #

@app.message(re.compile("|".join(include_patterns)))
def handle_plain_messages(message, client):
    channel_id = message.get("channel")

    if channel_id not in channel_ids:
        logger.info(f"Ignoring message from channel {channel_id}")
        return

    text = message.get("text", "")
    if any(re.search(p, text) for p in exclude_patterns):
        return

    handle_alert(text, channel_id, message["ts"])


@app.event("message")
def handle_message_events(body, logger, client):
    event = body.get("event", {})
    channel_id = event.get("channel")

    if channel_id not in channel_ids:
        logger.info(f"Ignoring event from channel {channel_id}")
        return

    text = event.get("text", "")
    subtype = event.get("subtype")

    # Handle Datadog bot messages (text-only)
    if subtype == "bot_message" and text:
        if any(re.search(p, text) for p in exclude_patterns):
            return
        if any(re.search(p, text) for p in include_patterns):
            handle_alert(text, channel_id, event["ts"])

    # Handle attachments (snapshots / graphs)
    for attachment in event.get("attachments", []):
        fallback = attachment.get("fallback", "")
        if not fallback:
            continue

        if any(re.search(p, fallback) for p in exclude_patterns):
            continue
        if any(re.search(p, fallback) for p in include_patterns):
            handle_alert(fallback, channel_id, event["ts"])

# ---------------- MAIN ---------------- #

if __name__ == "__main__":
    logger.info("Starting Slackbot (final merged-alert version)")
    SocketModeHandler(app, app_token).start()

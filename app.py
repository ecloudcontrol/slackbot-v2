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
channel_ids = os.environ.get("CHANNEL_IDS", "").split(",")

# ---- REQUIRED WARNING SECTION ---- #

if not app_token:
    logger.warning("APP_TOKEN not found in the vault.")

if not bot_token:
    logger.warning("BOT_TOKEN not found in the vault.")

if not target_channel_id:
    logger.warning("TARGET_CHANNEL_ID not found in env.")

if not channel_ids or channel_ids == [""]:
    logger.warning("CHANNEL_IDS not found in env.")

if not all([app_token, bot_token, target_channel_id]) or channel_ids == [""]:
    logger.error("Missing required environment variables. Aborting...")
    sys.exit(1)

# ---------------- SLACK APP ---------------- #

app = App(token=bot_token)

# ---------------- CACHE ---------------- #
# alert_name -> state + merged channels + slack_ts

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
    return match.group(1), match.group(2).split("\n")[0].strip()


def format_sources(channels):
    return ", ".join(f"<#{cid}>" for cid in sorted(channels))


def get_channel_name(channel_id):
    return app.client.conversations_info(channel=channel_id)["channel"]["name"]

# ---------------- CORE MERGE LOGIC ---------------- #

def should_forward_or_update(alert_name, state, channel_id):
    """
    Decide whether to send a new message, update existing one, or ignore.
    """

    entry = recent_messages_cache.get(alert_name)

    if not entry:
        entry = {
            "triggered": False,
            "retriggered": False,
            "warn": False,
            "recovered": False,
            "channels": set(),
            "slack_ts": None,
            "title": None,
            "state": None,
            "first_seen": datetime.now()
        }
        recent_messages_cache[alert_name] = entry

    # 🔑 Detect if this is a NEW source channel
    is_new_channel = channel_id not in entry["channels"]
    entry["channels"].add(channel_id)

    # 🔄 If already sent AND a new channel appears → UPDATE
    if entry["slack_ts"] and is_new_channel:
        return "update"

    # 🟢 First-send rules
    if state == "Triggered" and not entry["triggered"]:
        entry["triggered"] = True
        entry["state"] = state
        return "send"

    if state == "Re-Triggered" and not entry["retriggered"]:
        entry["retriggered"] = True
        entry["state"] = state
        return "send"

    if state == "Warn" and not entry["warn"]:
        entry["warn"] = True
        entry["state"] = state
        return "send"

    if state == "Recovered" and not entry["recovered"]:
        entry["recovered"] = True
        entry["state"] = state
        return "send"

    return "ignore"

# ---------------- SLACK SEND / UPDATE ---------------- #

def build_message(title, state, alert_name):
    entry = recent_messages_cache[alert_name]
    sources = format_sources(entry["channels"])

    return (
        f"*{state}: {title}*\n"
        f"Sources: {sources}"
    )


def send_new_message(original_message, channel_id, message_ts, state, alert_name):
    permalink = app.client.chat_getPermalink(
        channel=channel_id,
        message_ts=message_ts
    )["permalink"]

    title = f"<{permalink}|{original_message}>"

    recent_messages_cache[alert_name]["title"] = title
    recent_messages_cache[alert_name]["state"] = state

    message_text = build_message(title, state, alert_name)

    color_map = {
        "Triggered": "#E01E5A",
        "Re-Triggered": "#E01E5A",
        "Warn": "#ECB22E",
        "Recovered": "#2EB67D"
    }

    resp = app.client.chat_postMessage(
        channel=target_channel_id,
        attachments=[
            {
                "color": color_map.get(state, "#CCCCCC"),
                "text": message_text,
                "mrkdwn_in": ["text"]
            }
        ],
        unfurl_links=False
    )

    recent_messages_cache[alert_name]["slack_ts"] = resp["ts"]


def update_existing_message(alert_name):
    entry = recent_messages_cache[alert_name]

    updated_text = build_message(
        entry["title"],
        entry["state"],
        alert_name
    )

    color_map = {
        "Triggered": "#E01E5A",
        "Re-Triggered": "#E01E5A",
        "Warn": "#ECB22E",
        "Recovered": "#2EB67D"
    }

    app.client.chat_update(
        channel=target_channel_id,
        ts=entry["slack_ts"],
        attachments=[
            {
                "color": color_map.get(entry["state"], "#CCCCCC"),
                "text": updated_text,
                "mrkdwn_in": ["text"]
            }
        ]
    )

# ---------------- MAIN HANDLER ---------------- #

def handle_alert(original_message, channel_id, message_ts):
    state, alert_name = extract_alert(original_message)
    if not state:
        return

    action = should_forward_or_update(alert_name, state, channel_id)

    if action == "send":
        send_new_message(original_message, channel_id, message_ts, state, alert_name)
        logger.info(f"Sent new alert: {alert_name}")

    elif action == "update":
        update_existing_message(alert_name)
        logger.info(f"Updated alert sources: {alert_name}")

    # 🔁 Reset lifecycle AFTER merged recovery
    if state == "Recovered":
        recent_messages_cache.pop(alert_name, None)

# ---------------- SLACK HANDLERS ---------------- #

@app.message(re.compile("|".join(include_patterns)))
def handle_plain_messages(message, client):
    if message["channel"] not in channel_ids:
        return

    text = message["text"]
    if any(re.search(p, text) for p in exclude_patterns):
        return

    handle_alert(text, message["channel"], message["ts"])


@app.event("message")
def handle_attachment_messages(body, logger, client):
    event = body["event"]

    if event.get("channel") not in channel_ids:
        return

    for attachment in event.get("attachments", []):
        fallback = attachment.get("fallback", "")
        if not fallback:
            continue

        if any(re.search(p, fallback) for p in exclude_patterns):
            continue

        if any(re.search(p, fallback) for p in include_patterns):
            handle_alert(fallback, event["channel"], event["ts"])

# ---------------- MAIN ---------------- #

if __name__ == "__main__":
    logger.info("Starting Slackbot (merged + update-in-place FIXED)")
    SocketModeHandler(app, app_token).start()

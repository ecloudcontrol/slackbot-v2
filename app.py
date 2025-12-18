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

# ---------------- EMOJIS ---------------- #

EMOJI_MAP = {
    "Triggered": "🚨",       # :siren:
    "Re-Triggered": "🚨",    # :siren:
    "Recovered": "✅"
}

# ---------------- REQUIRED WARNINGS ---------------- #

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

# ---------------- ALERT STATE CACHE ---------------- #
# {
#   alert_name: {
#       "triggered_sent": bool,
#       "retriggered_sent": bool
#   }
# }

recent_messages_cache = {}

# ---------------- ALERT REGEX ---------------- #
# Handles:
# Triggered: [TEST] Something
# Re-Triggered: Something
# Recovered: Something

ALERT_REGEX = re.compile(
    r'(?i)\b(Triggered|Re-Triggered|Recovered|Warn):\s*(?:\[[^\]]+\]\s*)*(.+)'
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
    """
    Extract alert state and alert name.
    """
    match = ALERT_REGEX.search(text)
    if not match:
        return None, None

    alert_name = match.group(2).strip()
    alert_name = alert_name.split("\n")[0].strip()
    return match.group(1), alert_name


def should_forward_alert(alert_name, state):
    """
    FINAL lifecycle rules:

    - Triggered → forward ONCE
    - Re-Triggered → forward ONCE
    - Recovered → always forward (resets state)
    - Everything else → suppress
    """

    entry = recent_messages_cache.get(alert_name)

    # Initialize state
    if not entry:
        recent_messages_cache[alert_name] = {
            "triggered_sent": False,
            "retriggered_sent": False
        }
        entry = recent_messages_cache[alert_name]

    # -------- Triggered -------- #
    if state == "Triggered":
        if entry["triggered_sent"]:
            return False
        entry["triggered_sent"] = True
        return True

    # -------- Re-Triggered -------- #
    if state == "Re-Triggered":
        if entry["retriggered_sent"]:
            return False
        entry["retriggered_sent"] = True
        return True

    # -------- Recovered -------- #
    if state == "Recovered":
        recent_messages_cache.pop(alert_name, None)
        return True

    # -------- Warn or anything else -------- #
    return False


def get_channel_name(channel_id):
    resp = app.client.conversations_info(channel=channel_id)
    return resp["channel"]["name"]


def send_to_target(original_message, channel_id, message_ts, state):
    channel_name = get_channel_name(channel_id)

    permalink = app.client.chat_getPermalink(
        channel=channel_id,
        message_ts=message_ts
    )["permalink"]

    emoji = EMOJI_MAP.get(state, "ℹ️")

    final_message = (
        f"{emoji} *{original_message}*\n"
        f"Link: <{permalink}|View message>\n"
        f"Channel: <#{channel_id}|{channel_name}>"
    )

    color_map = {
        "Triggered": "#E01E5A",
        "Re-Triggered": "#E01E5A",
        "Recovered": "#2EB67D"
    }

    app.client.chat_postMessage(
        channel=target_channel_id,
        attachments=[
            {
                "color": color_map.get(state, "#CCCCCC"),
                "text": final_message,
                "mrkdwn_in": ["text"]
            }
        ],
        unfurl_links=False
    )

# ---------------- CORE HANDLER ---------------- #

def handle_alert(original_message, channel_id, message_ts):
    state, alert_name = extract_alert(original_message)

    if not state:
        logger.info("Not a valid alert format, skipping")
        return

    if not should_forward_alert(alert_name, state):
        logger.info(f"Suppressed: {state} | {alert_name}")
        return

    send_to_target(original_message, channel_id, message_ts, state)
    logger.info(f"Forwarded: {state} | {alert_name}")

# ---------------- SLACK MESSAGE HANDLERS ---------------- #

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

    if "attachments" not in event:
        return

    for attachment in event["attachments"]:
        fallback = attachment.get("fallback", "")
        if not fallback:
            continue

        if any(re.search(p, fallback) for p in exclude_patterns):
            continue

        if any(re.search(p, fallback) for p in include_patterns):
            handle_alert(fallback, event["channel"], event["ts"])

# ---------------- BUTTON HANDLER ---------------- #

@app.action("button_click")
def button_click(ack, body, client):
    ack()
    client.reactions_add(
        channel=body["channel"]["id"],
        name="white_check_mark",
        timestamp=body["message"]["ts"]
    )

# ---------------- MAIN ---------------- #

if __name__ == "__main__":
    logger.info("Starting Slackbot (final lifecycle-controlled version)")
    SocketModeHandler(app, app_token).start()

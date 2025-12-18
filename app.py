import os
import sys
import re
import json
import logging
from datetime import datetime, timedelta
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

# ---- REQUIRED WARNING SECTION (AS REQUESTED) ---- #

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
recent_messages_cache = {}

# ---------------- CANONICAL ALERT REGEX ---------------- #
# Matches:
# Triggered: [TEST] Something
# Recovered: [CRITICAL] Something
# Warn: Something

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
    """
    Extracts the FIRST alert line from a multiline Datadog message.
    Returns (state, alert_text)
    """
    match = ALERT_REGEX.search(text)
    if not match:
        return None, None

    alert_text = match.group(2).strip()
    alert_text = alert_text.split("\n")[0].strip()  # 🔥 important

    return match.group(1), alert_text



def cache_key(state, alert_text):
    return f"{state}:{alert_text}"


def is_recent(key, minutes=60):
    entry = recent_messages_cache.get(key)
    if not entry:
        return False
    return datetime.now() - entry["time"] <= timedelta(minutes=minutes)


def update_cache(key):
    recent_messages_cache[key] = {"time": datetime.now()}


def get_channel_name(channel_id):
    resp = app.client.conversations_info(channel=channel_id)
    return resp["channel"]["name"]


def send_to_target(original_message, channel_id, message_ts):
    channel_name = get_channel_name(channel_id)
    permalink = app.client.chat_getPermalink(
        channel=channel_id, message_ts=message_ts
    )["permalink"]

    final_message = (
        f"{original_message}\n"
        f"Link: <{permalink}|View message>\n"
        f"Channel: <#{channel_id}|{channel_name}>"
    )

    app.client.chat_postMessage(
        channel=target_channel_id,
        text=final_message,
        unfurl_links=False
    )

# ---------------- CORE HANDLER ---------------- #

def handle_alert(original_message, channel_id, message_ts):
    state, alert_text = extract_alert(original_message)

    if not state:
        logger.info("Not a valid alert format, skipping")
        return

    key = cache_key(state, alert_text)

    # Deduplication rules
    if state in ["Triggered", "Re-Triggered", "Warn"]:
        if is_recent(key):
            logger.info(f"Duplicate suppressed: {key}")
            return
        update_cache(key)

    elif state == "Recovered":
        update_cache(key)

    send_to_target(original_message, channel_id, message_ts)
    logger.info(f"Forwarded alert: {key}")

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
    logger.info("Starting Slackbot (final simplified version)")
    SocketModeHandler(app, app_token).start()

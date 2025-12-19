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
channel_ids = [c.strip() for c in os.environ.get("CHANNEL_IDS", "").split(",") if c.strip()]

if not all([app_token, bot_token, target_channel_id, channel_ids]):
    logger.error("Missing required environment variables. Aborting...")
    sys.exit(1)

# ---------------- SLACK APP ---------------- #

app = App(token=bot_token)

# ---------------- CACHE ---------------- #

recent_messages_cache = {}

# ---------------- PATTERNS ---------------- #

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

# ---------------- LEGACY LOGIC (UNCHANGED) ---------------- #

def extract_triggered_message(original_message, pattern):
    match1 = re.search(pattern, original_message)
    match2 = re.search(r'(Name:(.+\n.+)())', original_message)

    if match1:
        return match1.group(2), match1.group(3)
    elif match2:
        return match2.group(2), 'Issue'
    return None, None


def is_triggered_message_cached(triggered_message, original_message):
    alert, key = triggered_message
    if not alert or not key:
        return False

    if "Issue" in original_message:
        if key in recent_messages_cache and alert in recent_messages_cache[key]:
            timestamp = recent_messages_cache[key][alert]['time']
            return (datetime.now() - timestamp) <= timedelta(minutes=15)

    elif "Triggered" in original_message:
        if key in recent_messages_cache and alert in recent_messages_cache[key]:
            timestamp = recent_messages_cache[key][alert]['time']
            return (datetime.now() - timestamp) <= timedelta(minutes=60)

    return False


def update_recent_messages_cache(triggered_message, unstable=False):
    alert, key = triggered_message
    if not alert or not key:
        return

    recent_messages_cache.setdefault(key, {})
    recent_messages_cache[key].setdefault(alert, {
        "time": datetime.now(),
        "trigger_count": 0
    })

    if unstable:
        recent_messages_cache[key][alert]["trigger_count"] += 1

    recent_messages_cache[key][alert]["time"] = datetime.now()


def reset_sequence(triggered_message, original_message):
    alert, key = triggered_message
    try:
        if "Recovered" in original_message:
            recent_messages_cache.pop(key, None)
        else:
            recent_messages_cache.get(key, {}).pop(alert, None)
    except Exception as e:
        logger.error(e)


def get_channel_name(channel_id):
    try:
        return app.client.conversations_info(channel=channel_id)["channel"]["name"]
    except Exception:
        return channel_id


def send_message_to_channel(original_message, channel_id, message_ts, channel_name):
    permalink = app.client.chat_getPermalink(
        channel=channel_id,
        message_ts=message_ts
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

def handle_filtered_message(original_message, channel_id, message_ts):
    channel_name = get_channel_name(channel_id)
    triggers = ["Disaster", "High"]

    if "Triggered" in original_message or ("started" in original_message and any(t in original_message for t in triggers)):
        if "increased lag on Kafka" in original_message:
            pattern = r'(Triggered:)(\s*([\w]+)\s*(.+))'
        else:
            pattern = r'(Triggered:)(.+)'

        triggered_message = extract_triggered_message(original_message, pattern)

        if not is_triggered_message_cached(triggered_message, original_message):
            send_message_to_channel(original_message, channel_id, message_ts, channel_name)
            update_recent_messages_cache(triggered_message)
        else:
            update_recent_messages_cache(triggered_message, unstable=True)

    elif "Recovered" in original_message or ("resolved" in original_message and any(t in original_message for t in triggers)):
        pattern = r'(Recovered:)(.+)'
        triggered_message = extract_triggered_message(original_message, pattern)
        reset_sequence(triggered_message, original_message)
        send_message_to_channel(original_message, channel_id, message_ts, channel_name)

# ---------------- MESSAGE HANDLERS ---------------- #

@app.message(re.compile("|".join(include_patterns)))
def handle_plain_messages(message, client):
    channel_id = message["channel"]
    if channel_id not in channel_ids:
        logger.info(f"Ignoring message from channel {channel_id}")
        return

    text = message.get("text", "")
    if any(re.search(p, text) for p in exclude_patterns):
        return

    handle_filtered_message(text, channel_id, message["ts"])


@app.event("message")
def handle_event_messages(body, logger, client):
    event = body["event"]
    channel_id = event.get("channel")

    if channel_id not in channel_ids:
        logger.info(f"Ignoring event from channel {channel_id}")
        return

    text = event.get("text", "")
    subtype = event.get("subtype")

    # Datadog bot_message
    if subtype == "bot_message" and text:
        if any(re.search(p, text) for p in include_patterns):
            handle_filtered_message(text, channel_id, event["ts"])

    # Attachments (graphs)
    for attachment in event.get("attachments", []):
        fallback = attachment.get("fallback", "")
        if fallback and any(re.search(p, fallback) for p in include_patterns):
            handle_filtered_message(fallback, channel_id, event["ts"])

# ---------------- MAIN ---------------- #

if __name__ == "__main__":
    logger.info("Starting Slackbot (legacy logic + fixed handlers)")
    SocketModeHandler(app, app_token).start()

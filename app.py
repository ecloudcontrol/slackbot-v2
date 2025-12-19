import os
import sys
import re
import json
import logging
from datetime import datetime, timedelta
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


# ---------------- CONFIGURATION ---------------- #
# Logging
LOG_FILE = os.environ.get("LOG_FILE", "/appz/log/slackbot.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)  # Ensure log dir exists
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")]
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Environment Variables
REQUIRED_ENVS = ["APP_TOKEN", "BOT_TOKEN", "TARGET_CHANNEL_ID", "CHANNEL_IDS"]
env_vars = {k: os.environ.get(k) for k in REQUIRED_ENVS}
app_token = env_vars["APP_TOKEN"]
bot_token = env_vars["BOT_TOKEN"]
target_channel_id = env_vars["TARGET_CHANNEL_ID"]
channel_ids_raw = env_vars["CHANNEL_IDS"]
channel_ids = [cid.strip() for cid in channel_ids_raw.split(",") if cid.strip()]

patterns_path = os.environ.get("PATTERNS_PATH", "/appz/scripts/webapps/patterns.json")

if not all([app_token, bot_token, target_channel_id, channel_ids]):
    logger.error("Missing required environment variables. Aborting...")
    sys.exit(1)

# ---------------- SLACK APP ---------------- #
app = App(token=bot_token)


# ---------------- CONSTANTS ---------------- #
TIME_WINDOWS = {
    "Triggered": timedelta(minutes=60),
    "Re-Triggered": timedelta(minutes=60),
    "Warn": timedelta(minutes=15),
    "Recovered": timedelta(minutes=5),
}

STATE_COLORS = {
    "Triggered": "#E01E5A",
    "Re-Triggered": "#E01E5A",
    "Warn": "#ECB22E",
    "Recovered": "#2EB67D"
}

# ✅ Emoji mapping (NEW)
STATE_EMOJIS = {
    "Triggered": "🚨",
    "Re-Triggered": "🚨",
    "Warn": "⚠️",
    "Recovered": "✅",
}

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


include_patterns, exclude_patterns = load_filter_patterns(patterns_path)


# ---------------- HELPERS ---------------- #
def now_utc():
    """Return current UTC datetime for consistency."""
    return datetime.utcnow()


def extract_alert(text):
    """Extract alert state and name from text."""
    match = ALERT_REGEX.search(text)
    if not match:
        return None, None
    state = match.group(1).strip().title()
    alert_name = match.group(2).split("\n")[0].strip()
    return state, alert_name


def should_forward_alert(alert_name, state, channel_id):
    """
    Determine if alert should be forwarded based on lifecycle rules.
    """
    now = now_utc()
    entry = recent_messages_cache.get(alert_name)
    if not entry:
        entry = {
            "states": {},
            "channels": set(),
            "incident_active": False,
            "first_seen": now
        }
        recent_messages_cache[alert_name] = entry

    entry["channels"].add(channel_id)

    if state == "Warn" and entry["incident_active"]:
        return False

    if state == "Recovered" and not entry["incident_active"]:
        return False

    last_seen = entry["states"].get(state)
    window = TIME_WINDOWS.get(state)
    if last_seen and window and (now - last_seen) <= window:
        return False

    entry["states"][state] = now

    if state in ("Triggered", "Re-Triggered"):
        entry["incident_active"] = True

    return True


def format_sources(alert_name):
    channels = recent_messages_cache.get(alert_name, {}).get("channels", set())
    return ", ".join(f"<#{cid}>" for cid in sorted(channels))


def get_permalink(channel_id, message_ts):
    try:
        response = app.client.chat_getPermalink(
            channel=channel_id,
            message_ts=message_ts
        )
        return response["permalink"]
    except Exception as e:
        logger.error(f"Failed to get permalink: {e}")
        return f"slack://channel?team=T00000000&id={channel_id}&msg={message_ts}"


def send_to_target(original_message, channel_id, message_ts, state, alert_name):
    """Send formatted alert to target channel."""
    permalink = get_permalink(channel_id, message_ts)
    sources = format_sources(alert_name)

    # ✅ Add emoji prefix (NEW)
    emoji = STATE_EMOJIS.get(state, "")
    final_message = f"{emoji} <{permalink}|{original_message}>\nSources: {sources}"

    try:
        app.client.chat_postMessage(
            channel=target_channel_id,
            attachments=[
                {
                    "color": STATE_COLORS.get(state, "#CCCCCC"),
                    "text": final_message,
                    "mrkdwn_in": ["text"]
                }
            ],
            unfurl_links=False
        )
    except Exception as e:
        logger.error(f"Failed to post message: {e}")


# ---------------- CACHE ---------------- #
recent_messages_cache = {}


# ---------------- CORE HANDLER ---------------- #
def handle_alert(original_message, channel_id, message_ts):
    state, alert_name = extract_alert(original_message)
    if not state or not alert_name:
        return

    if channel_id not in channel_ids:
        return

    if any(re.search(p, original_message, re.IGNORECASE) for p in exclude_patterns):
        return

    if not should_forward_alert(alert_name, state, channel_id):
        return

    send_to_target(original_message, channel_id, message_ts, state, alert_name)

    if state == "Recovered":
        recent_messages_cache.pop(alert_name, None)


# ---------------- MESSAGE HANDLERS ---------------- #
@app.message(re.compile("|".join(include_patterns), re.IGNORECASE))
def handle_plain_messages(message, say):
    text = message.get("text", "")
    handle_alert(text, message["channel"], message["ts"])


@app.event("message")
def handle_attachment_messages(event, say):
    if "attachments" not in event or event.get("subtype") == "message_deleted":
        return

    channel_id = event["channel"]
    if channel_id not in channel_ids:
        return

    text = event.get("text", "")
    if text and re.search("|".join(include_patterns), text, re.IGNORECASE):
        return

    for attachment in event.get("attachments", []):
        alert_text = (
            attachment.get("fallback")
            or attachment.get("title")
            or attachment.get("text")
        )
        if not alert_text:
            continue

        if any(re.search(p, alert_text, re.IGNORECASE) for p in exclude_patterns):
            continue

        if any(re.search(p, alert_text, re.IGNORECASE) for p in include_patterns):
            handle_alert(alert_text, channel_id, event["ts"])


# ---------------- MAIN ---------------- #
if __name__ == "__main__":
    logger.info("Starting Slackbot (lifecycle-managed with emojis)")
    SocketModeHandler(app, app_token).start()

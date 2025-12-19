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
    """Extract alert state and name from text. Normalize state case."""
    match = ALERT_REGEX.search(text)
    if not match:
        return None, None
    # Normalize state to title case (e.g., "warn" -> "Warn")
    state = match.group(1).strip().title()
    # Take first line for alert name, strip whitespace
    alert_name = match.group(2).split("\n")[0].strip()
    return state, alert_name


def should_forward_alert(alert_name, state, channel_id):
    """
    Determine if alert should be forwarded based on lifecycle rules:
    - Suppress Warn after incident active.
    - Block Recovered without prior active incident.
    - Suppress within time window.
    - Merge multi-channel sources.
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

    # Merge channel sources
    entry["channels"].add(channel_id)

    # Suppress Warn after incident active
    if state == "Warn" and entry["incident_active"]:
        logger.info(f"Suppressed Warn after incident active: {alert_name}")
        return False

    # Block Recovered if incident never active
    if state == "Recovered" and not entry["incident_active"]:
        logger.info(f"Suppressed Recovered without active incident: {alert_name}")
        return False

    # Time-window suppression
    last_seen = entry["states"].get(state)
    window = TIME_WINDOWS.get(state)
    if last_seen and window and (now - last_seen) <= window:
        logger.info(f"Suppressed by time-window: {state} | {alert_name}")
        return False

    # Record state timestamp
    entry["states"][state] = now

    # Mark incident active
    if state in ("Triggered", "Re-Triggered"):
        entry["incident_active"] = True

    return True


def format_sources(alert_name):
    """Format channel sources as Slack mentions."""
    channels = recent_messages_cache.get(alert_name, {}).get("channels", set())
    return ", ".join(f"<#{cid}>" for cid in sorted(channels))


def get_permalink(channel_id, message_ts):
    """Get permalink for message, with error handling."""
    try:
        response = app.client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
        return response["permalink"]
    except Exception as e:
        logger.error(f"Failed to get permalink for {channel_id}/{message_ts}: {e}")
        # Fallback: Construct a pseudo-link
        return f"slack://channel?team=T00000000&id={channel_id}&msg={message_ts}"


def send_to_target(original_message, channel_id, message_ts, state, alert_name):
    """Send formatted alert to target channel as attachment."""
    permalink = get_permalink(channel_id, message_ts)
    sources = format_sources(alert_name)
    final_message = f"<{permalink}|{original_message}>\nSources: {sources}"

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
        logger.error(f"Failed to post message for {alert_name}: {e}")


# ---------------- CACHE ---------------- #
recent_messages_cache = {}


# ---------------- CORE HANDLER ---------------- #
def handle_alert(original_message, channel_id, message_ts):
    """Core logic to process and forward alerts."""
    state, alert_name = extract_alert(original_message)
    if not state or not alert_name:
        return

    if channel_id not in channel_ids:
        return

    # Check excludes first
    if any(re.search(p, original_message, re.IGNORECASE) for p in exclude_patterns):
        logger.debug(f"Excluded by pattern: {alert_name}")
        return

    if not should_forward_alert(alert_name, state, channel_id):
        logger.info(f"Suppressed: {state} | {alert_name}")
        return

    send_to_target(original_message, channel_id, message_ts, state, alert_name)
    logger.info(f"Forwarded: {state} | {alert_name}")

    # End lifecycle after recovery
    if state == "Recovered":
        recent_messages_cache.pop(alert_name, None)


# ---------------- MESSAGE HANDLERS ---------------- #
# Handler for plain text messages matching include patterns
@app.message(re.compile("|".join(include_patterns), re.IGNORECASE))
def handle_plain_messages(message, say):
    """Handle plain text alert messages."""
    text = message.get("text", "")
    handle_alert(text, message["channel"], message["ts"])


# Handler for messages with attachments
@app.event("message")
def handle_attachment_messages(event, say):
    """Handle messages with attachments, skipping if plain text already processed."""
    if "attachments" not in event or event.get("subtype") == "message_deleted":
        return

    channel_id = event["channel"]
    if channel_id not in channel_ids:
        return

    text = event.get("text", "")
    # Skip if plain text matches include (avoids duplicates)
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

        # Check excludes
        if any(re.search(p, alert_text, re.IGNORECASE) for p in exclude_patterns):
            continue

        # Check includes
        if any(re.search(p, alert_text, re.IGNORECASE) for p in include_patterns):
            handle_alert(alert_text, channel_id, event["ts"])


# ---------------- MAIN ---------------- #
if __name__ == "__main__":
    logger.info("Starting Slackbot (lifecycle-managed with dedup & filtering)")
    SocketModeHandler(app, app_token).start()

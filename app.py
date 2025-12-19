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

if not all([app_token, bot_token, target_channel_id]) or channel_ids == [""]:
    logger.error("Missing required environment variables. Aborting...")
    sys.exit(1)

# ---------------- SLACK APP ---------------- #

app = App(token=bot_token)

# ---------------- CACHE ---------------- #
# alert_name -> lifecycle + timestamps + channels

recent_messages_cache = {}

# ---------------- TIME WINDOWS ---------------- #

TIME_WINDOWS = {
    "Triggered": timedelta(minutes=60),
    "Re-Triggered": timedelta(minutes=60),
    "Warn": timedelta(minutes=15),
    "Recovered": timedelta(minutes=5),
}

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
    """
    Rules:
    - Time-window suppression
    - Only first alert per lifecycle state
    - Recovered allowed ONLY if incident was active
      (Triggered or Re-Triggered forwarded)
    - Merge alerts from multiple channels
    """
    now = datetime.now()
    entry = recent_messages_cache.get(alert_name)

    if not entry:
        entry = {
            "states": {},
            "channels": set(),
            "incident_active": False,
            "first_seen": now
        }
        recent_messages_cache[alert_name] = entry

    # merge channel source
    entry["channels"].add(channel_id)

    # 🚫 Block recovered if incident never active
    if state == "Recovered" and not entry["incident_active"]:
        logger.info(f"Suppressed Recovered without active incident: {alert_name}")
        return False

    last_seen = entry["states"].get(state)
    window = TIME_WINDOWS.get(state)

    if last_seen and window and (now - last_seen) <= window:
        logger.info(f"Suppressed by time-window: {state} | {alert_name}")
        return False

    # record state
    entry["states"][state] = now

    if state in ("Triggered", "Re-Triggered"):
        entry["incident_active"] = True

    return True


def format_sources(alert_name):
    channels = recent_messages_cache.get(alert_name, {}).get("channels", set())
    return ", ".join(f"<#{cid}>" for cid in sorted(channels))


def send_to_target(original_message, channel_id, message_ts, state, alert_name):
    permalink = app.client.chat_getPermalink(
        channel=channel_id,
        message_ts=message_ts
    )["permalink"]

    sources = format_sources(alert_name)

    final_message = (
        f"<{permalink}|{original_message}>\n"
        f"Sources: {sources}"
    )

    color_map = {
        "Triggered": "#E01E5A",
        "Re-Triggered": "#E01E5A",
        "Warn": "#ECB22E",
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
        return

    if not should_forward_alert(alert_name, state, channel_id):
        logger.info(f"Suppressed: {state} | {alert_name}")
        return

    send_to_target(original_message, channel_id, message_ts, state, alert_name)
    logger.info(f"Forwarded: {state} | {alert_name}")

    # 🔁 End lifecycle after recovery
    if state == "Recovered":
        recent_messages_cache.pop(alert_name, None)

# ---------------- MESSAGE HANDLERS ---------------- #

@app.message(re.compile("|".join(include_patterns)))
def handle_plain_messages(message, client):
    if message["channel"] not in channel_ids:
        return

    text = message.get("text", "")

    if any(re.search(p, text) for p in exclude_patterns):
        return

    handle_alert(text, message["channel"], message["ts"])


@app.event("message")
def handle_attachment_messages(body, logger, client):
    event = body["event"]

    if event.get("channel") not in channel_ids:
        return

    for attachment in event.get("attachments", []):
        alert_text = (
            attachment.get("fallback")
            or attachment.get("title")
            or attachment.get("text")
        )

        if not alert_text:
            continue

        if any(re.search(p, alert_text) for p in exclude_patterns):
            continue

        if any(re.search(p, alert_text) for p in include_patterns):
            handle_alert(alert_text, event["channel"], event["ts"])

# ---------------- MAIN ---------------- #

if __name__ == "__main__":
    logger.info("Starting Slackbot (final lifecycle + time-window + recovery gated)")
    SocketModeHandler(app, app_token).start()

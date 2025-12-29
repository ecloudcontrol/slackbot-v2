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

# ---------------- ENVIRONMENT ---------------- #
REQUIRED_ENVS = ["APP_TOKEN", "BOT_TOKEN", "TARGET_CHANNEL_ID", "CHANNEL_IDS"]
env = {k: os.environ.get(k) for k in REQUIRED_ENVS}

app_token = env["APP_TOKEN"]
bot_token = env["BOT_TOKEN"]
target_channel_id = env["TARGET_CHANNEL_ID"]
channel_ids = [c.strip() for c in env["CHANNEL_IDS"].split(",") if c.strip()]

patterns_path = os.environ.get("PATTERNS_PATH", "/appz/scripts/webapps/patterns.json")

if not all([app_token, bot_token, target_channel_id, channel_ids]):
    logger.error("Missing required environment variables. Aborting.")
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
    "Recovered": "#2EB67D",
}

STATE_EMOJIS = {
    "Recovered": "✅",
}

ALERT_REGEX = re.compile(
    r'(?i)(Triggered|Recovered|Re-Triggered|Warn):\s*(?:\[[^\]]+\]\s*)*(.+)'
)

# ---------------- PATTERNS ---------------- #
def load_filter_patterns(path):
    try:
        with open(path) as f:
            data = json.load(f)
            logger.info("patterns.json loaded")
            return data.get("include_patterns", []), data.get("exclude_patterns", [])
    except Exception as e:
        logger.error(f"Failed to load patterns.json: {e}")
        sys.exit(1)

include_patterns, exclude_patterns = load_filter_patterns(patterns_path)

# ---------------- HELPERS ---------------- #
def now_utc():
    return datetime.utcnow()

def extract_alert(text):
    match = ALERT_REGEX.search(text)
    if not match:
        return None, None
    state = match.group(1).title()
    alert_name = match.group(2).split("\n")[0].strip()
    return state, alert_name

def get_permalink(channel_id, message_ts):
    try:
        r = app.client.chat_getPermalink(
            channel=channel_id,
            message_ts=message_ts
        )
        return r["permalink"]
    except Exception as e:
        logger.error(f"Permalink error: {e}")
        return f"slack://channel?id={channel_id}&message={message_ts}"

# ---------------- INCIDENT CACHE ---------------- #
recent_messages_cache = {}

def get_incident(alert_name):
    return recent_messages_cache.setdefault(
        alert_name,
        {
            "states": {},
            "observed_channels": set(),
            "origin_channel": None,
            "incident_active": False,
            "first_seen": now_utc(),
        }
    )

def should_forward_alert(alert_name, state):
    now = now_utc()
    incident = recent_messages_cache[alert_name]

    # Lifecycle rules
    if state == "Warn" and incident["incident_active"]:
        logger.info(f"[DEDUP] Suppressing Warn during active incident: {alert_name}")
        return False

    if state == "Recovered" and not incident["incident_active"]:
        logger.info(f"[DEDUP] Suppressing orphan Recovered: {alert_name}")
        return False

    last_seen = incident["states"].get(state)
    window = TIME_WINDOWS.get(state)

    if last_seen and window and (now - last_seen) <= window:
        logger.info(
            f"[DEDUP] Suppressing {state} within window "
            f"({(now - last_seen).seconds}s): {alert_name}"
        )
        return False

    incident["states"][state] = now

    if state in ("Triggered", "Re-Triggered"):
        incident["incident_active"] = True

    return True

def format_sources(alert_name):
    incident = recent_messages_cache.get(alert_name, {})
    chans = incident.get("observed_channels", set())
    return ", ".join(f"<#{c}>" for c in sorted(chans))

# ---------------- SEND MESSAGE ---------------- #
def send_to_target(original_message, channel_id, message_ts, state, alert_name):
    permalink = get_permalink(channel_id, message_ts)
    sources = format_sources(alert_name)
    emoji = STATE_EMOJIS.get(state, "")

    text = (
        f"{emoji} <{permalink}|{original_message}>\n"
        f"*Sources:* {sources}"
    )

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": text},
        }
    ]

    if state != "Recovered":
        blocks[0]["accessory"] = {
            "type": "button",
            "text": {"type": "plain_text", "text": "Have you fixed it?"},
            "action_id": "button_click",
            "value": json.dumps(
                {"channel_id": channel_id, "message_ts": message_ts}
            ),
        }

    app.client.chat_postMessage(
        channel=target_channel_id,
        blocks=blocks,
        attachments=[{"color": STATE_COLORS.get(state, "#CCCCCC")}],
        unfurl_links=False,
    )

    logger.info(
        f"[FORWARDED] {state} | {alert_name} | "
        f"sources={sources}"
    )

# ---------------- CORE HANDLER ---------------- #
def handle_alert(original_message, channel_id, message_ts):
    state, alert_name = extract_alert(original_message)
    if not state or not alert_name:
        return

    if channel_id not in channel_ids:
        return

    if any(re.search(p, original_message, re.IGNORECASE) for p in exclude_patterns):
        return

    incident = get_incident(alert_name)

    # Always record observed channel
    incident["observed_channels"].add(channel_id)

    # Set origin channel once
    if not incident["origin_channel"] and state in ("Triggered", "Re-Triggered"):
        incident["origin_channel"] = channel_id

    logger.info(
        f"[OBSERVED] {state} | {alert_name} | channel={channel_id} | "
        f"observed={incident['observed_channels']}"
    )

    if not should_forward_alert(alert_name, state):
        return

    send_to_target(original_message, channel_id, message_ts, state, alert_name)

    if state == "Recovered":
        logger.info(f"[INCIDENT CLOSED] {alert_name}")
        recent_messages_cache.pop(alert_name, None)

# ---------------- MESSAGE HANDLERS ---------------- #
@app.message(re.compile("|".join(include_patterns), re.IGNORECASE))
def handle_plain_messages(message, say):
    handle_alert(message.get("text", ""), message["channel"], message["ts"])

@app.event("message")
def handle_attachment_messages(event, say):
    if "attachments" not in event or event.get("subtype") == "message_deleted":
        return

    channel_id = event["channel"]
    if channel_id not in channel_ids:
        return

    for att in event.get("attachments", []):
        alert_text = att.get("fallback") or att.get("title") or att.get("text")
        if not alert_text:
            continue

        if any(re.search(p, alert_text, re.IGNORECASE) for p in exclude_patterns):
            continue

        if any(re.search(p, alert_text, re.IGNORECASE) for p in include_patterns):
            handle_alert(alert_text, channel_id, event["ts"])

# ---------------- BUTTON ACTION ---------------- #
@app.action("button_click")
def handle_button_click(ack, body, client, logger):
    ack()
    try:
        payload = json.loads(body["actions"][0]["value"])
        client.reactions_add(
            channel=payload["channel_id"],
            timestamp=payload["message_ts"],
            name="white_check_mark",
        )
        logger.info("[ACTION] white_check_mark added via button")
    except Exception as e:
        logger.error(f"Reaction failed: {e}")

# ---------------- MAIN ---------------- #
if __name__ == "__main__":
    logger.info("Starting Slackbot (incident lifecycle + aggregation + debug)")
    SocketModeHandler(app, app_token).start()

import os
import sys
import re
import json
import time
import logging
from datetime import datetime, timedelta
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ---------------- CONFIGURATION ---------------- #
LOG_FILE = os.environ.get("LOG_FILE", "/appz/log/slackbot.log")
PATTERNS_PATH = os.environ.get("PATTERNS_PATH", "/appz/scripts/webapps/patterns.json")

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

if not all(env.values()):
    logger.error("Missing required environment variables.")
    sys.exit(1)

app_token = env["APP_TOKEN"]
bot_token = env["BOT_TOKEN"]
target_channel_id = env["TARGET_CHANNEL_ID"]
channel_ids = [c.strip() for c in env["CHANNEL_IDS"].split(",") if c.strip()]

# ---------------- SLACK APP ---------------- #
app = App(token=bot_token)

# ---------------- GLOBAL STATE ---------------- #
CACHE_TTL = timedelta(hours=24)
STATE_EMOJIS = {"Recovered": "✅"}

recent_messages_cache = {}
processed_messages = set()

compiled_includes = []
compiled_excludes = []
last_pattern_load = 0
PATTERN_RELOAD_INTERVAL = 60  # seconds

# ---------------- UTILITIES ---------------- #
def now_utc():
    return datetime.utcnow()


def log_timing(label, start):
    elapsed = (time.time() - start) * 1000
    logger.debug(f"{label} executed in {elapsed:.2f}ms")


# ---------------- PATTERN LOADING ---------------- #
def load_patterns():
    global compiled_includes, compiled_excludes, last_pattern_load

    try:
        with open(PATTERNS_PATH) as f:
            data = json.load(f)
            compiled_includes = [
                re.compile(p, re.IGNORECASE) for p in data.get("include_patterns", [])
            ]
            compiled_excludes = [
                re.compile(p, re.IGNORECASE) for p in data.get("exclude_patterns", [])
            ]
            last_pattern_load = time.time()
            logger.info("Pattern file loaded successfully")
    except Exception as e:
        logger.error(f"Failed to load patterns: {e}")


def reload_patterns_if_needed():
    if time.time() - last_pattern_load > 60:
        load_patterns()


# ---------------- CACHE MANAGEMENT ---------------- #
def cleanup_cache():
    now = now_utc()
    for alert in list(recent_messages_cache.keys()):
        if now - recent_messages_cache[alert]["first_seen"] > CACHE_TTL:
            del recent_messages_cache[alert]


def already_processed(ts):
    if ts in processed_messages:
        return True
    processed_messages.add(ts)
    return False


# ---------------- ALERT LOGIC ---------------- #
def extract_alert(text):
    match = re.search(r"(Triggered|Recovered|Re-Triggered|Warn):\s*(.+)", text, re.I)
    if not match:
        return None, None
    return match.group(1), match.group(2)


def should_forward(alert, state, channel):
    cleanup_cache()

    now = now_utc()
    entry = recent_messages_cache.setdefault(alert, {
        "states": {},
        "channels": set(),
        "first_seen": now,
        "active": False
    })

    entry["channels"].add(channel)

    if state == "Warn" and entry["active"]:
        return False
    if state == "Recovered" and not entry["active"]:
        return False

    last = entry["states"].get(state)
    if last and (now - last) < timedelta(minutes=5):
        return False

    entry["states"][state] = now
    if state in ("Triggered", "Re-Triggered"):
        entry["active"] = True

    return True


def get_permalink(channel, ts):
    try:
        return app.client.chat_getPermalink(channel=channel, message_ts=ts)["permalink"]
    except Exception:
        return f"slack://channel?id={channel}&message={ts}"


def send_to_target(text, channel, ts, state, alert):
    emoji = STATE_EMOJIS["Recovered"] if state == "Recovered" else ""
    permalink = get_permalink(channel, ts)
    message = f"{emoji} <{permalink}|{text}>\nSources: <#{channel}>"

    app.client.chat_postMessage(
        channel=target_channel_id,
        attachments=[{
            "color": "#2EB67D" if state == "Recovered" else "#E01E5A",
            "text": message,
            "mrkdwn_in": ["text"]
        }],
        unfurl_links=False
    )


# ---------------- HANDLERS ---------------- #
@app.message(re.compile(".*"))
def handle_messages(message, say):
    start = time.time()
    reload_patterns_if_needed()

    if already_processed(message["ts"]):
        return

    text = message.get("text", "")
    if not text:
        return

    if not any(p.search(text) for p in compiled_includes):
        return

    if any(p.search(text) for p in compiled_excludes):
        return

    state, alert = extract_alert(text)
    if not state:
        return

    if should_forward(alert, state, message["channel"]):
        send_to_target(text, message["channel"], message["ts"], state, alert)

    log_timing("Message processed", start)


# ---------------- START ---------------- #
if __name__ == "__main__":
    load_patterns()
    logger.info("🚀 Slack Alert Processor Started")
    SocketModeHandler(app, app_token).start()

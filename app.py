import os
import re
import sys
import json
import logging
from datetime import datetime, timedelta
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler("/appz/log/slackbot.log", encoding="utf-8")]
)
logger = logging.getLogger()

# ---------------- ENV ----------------
app_token = os.environ.get("APP_TOKEN")
bot_token = os.environ.get("BOT_TOKEN")
target_channel_id = os.environ.get("TARGET_CHANNEL_ID")
channel_ids = os.environ.get("CHANNEL_IDS", "").split(",")

if not all([app_token, bot_token, target_channel_id, channel_ids]):
    logger.error("Missing required environment variables")
    sys.exit(1)

# ---------------- GLOBAL CACHE ----------------
recent_messages_cache = {}

# ---------------- LOAD PATTERNS ----------------
def load_filter_patterns(path):
    with open(path) as f:
        data = json.load(f)
    return data.get("include_patterns", []), data.get("exclude_patterns", [])

include_patterns, exclude_patterns = load_filter_patterns(
    "/appz/scripts/webapps/patterns.json"
)

# ---------------- HELPERS ----------------
def extract_triggered_message(text, pattern):
    match = re.search(pattern, text, re.I)
    if not match:
        return text, "unknown"

    groups = match.groups()
    if len(groups) >= 2:
        return groups[0], groups[1]
    elif len(groups) == 1:
        return groups[0], "unknown"
    return text, "unknown"


def update_cache(key, source, unstable=False):
    if key not in recent_messages_cache:
        recent_messages_cache[key] = {
            "sources": set(),
            "time": datetime.now(),
            "trigger_count": 0
        }

    recent_messages_cache[key]["sources"].add(source)
    if unstable:
        recent_messages_cache[key]["trigger_count"] += 1
    recent_messages_cache[key]["time"] = datetime.now()


def is_duplicate(key, window=900):
    entry = recent_messages_cache.get(key)
    if not entry:
        return False
    return (datetime.now() - entry["time"]) <= timedelta(seconds=window)


# ---------------- SLACK APP ----------------
app = App(token=bot_token)

# ---------------- MAIN HANDLER ----------------
@app.event("message")
def handle_message(event, say, logger):
    text = event.get("text", "")
    channel = event.get("channel")

    if not text or channel not in channel_ids:
        return

    if not any(re.search(p, text, re.I) for p in include_patterns):
        return

    if any(re.search(p, text, re.I) for p in exclude_patterns):
        return

    # detect alert
    triggered = bool(re.search(r"(triggered|increased|high|down)", text, re.I))

    pattern = next((p for p in include_patterns if re.search(p, text, re.I)), None)
    if not pattern:
        return

    key, _ = extract_triggered_message(text, pattern)

    # dedupe
    if is_duplicate(key):
        update_cache(key, channel)
        return

    update_cache(key, channel)

    sources = ", ".join(recent_messages_cache[key]["sources"])

    msg = (
        f"*Alert Detected*\n"
        f"{text}\n\n"
        f"*Sources:* {sources}"
    )

    say(text=msg)

# ---------------- START ----------------
if __name__ == "__main__":
    SocketModeHandler(app, app_token).start()

import os
import re
import json
import logging
import sys
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# -------------------------------------------------
# Logging
# -------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("/appz/log/slackbot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# -------------------------------------------------
# Environment
# -------------------------------------------------
APP_TOKEN = os.getenv("APP_TOKEN")
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHANNEL_ID = os.getenv("TARGET_CHANNEL_ID")
CHANNEL_IDS = os.getenv("CHANNEL_IDS", "")

if not all([APP_TOKEN, BOT_TOKEN, TARGET_CHANNEL_ID, CHANNEL_IDS]):
    logger.error("Missing required environment variables")
    sys.exit(1)

SOURCE_CHANNEL_IDS = [c.strip() for c in CHANNEL_IDS.split(",") if c.strip()]

# -------------------------------------------------
# Load patterns
# -------------------------------------------------
PATTERN_FILE = "/appz/scripts/webapps/patterns.json"

def load_patterns():
    with open(PATTERN_FILE) as f:
        data = json.load(f)
    include = data.get("include_patterns", [])
    exclude = data.get("exclude_patterns", [])
    logger.info(f"Loaded patterns: include={include}, exclude={exclude}")
    return (
        [re.compile(p, re.IGNORECASE) for p in include],
        [re.compile(p, re.IGNORECASE) for p in exclude],
    )

INCLUDE_REGEX, EXCLUDE_REGEX = load_patterns()

# -------------------------------------------------
# Slack App
# -------------------------------------------------
app = App(token=BOT_TOKEN)
BOT_ID = app.client.auth_test()["bot_id"]
logger.info(f"Bot ID: {BOT_ID}")

# -------------------------------------------------
# Helpers
# -------------------------------------------------
def matches_any(regex_list, text):
    return any(rx.search(text) for rx in regex_list)

def forward(text):
    app.client.chat_postMessage(
        channel=TARGET_CHANNEL_ID,
        text=text,
        unfurl_links=False,
    )
    logger.info("Forwarded message to target channel")

# -------------------------------------------------
# Message listener (THIS IS THE CORE)
# -------------------------------------------------
@app.event("message")
def handle_message(event, logger):
    logger.info("=== MESSAGE EVENT RECEIVED ===")
    logger.info(json.dumps(event, indent=2))

    channel = event.get("channel")
    text = event.get("text", "")
    event_bot_id = event.get("bot_id")

    # 1. Only source channels
    if channel not in SOURCE_CHANNEL_IDS:
        return

    # 2. Ignore empty messages
    if not text:
        return

    # 3. Prevent infinite loop
    if event_bot_id == BOT_ID:
        return

    # 4. Exclude patterns
    if matches_any(EXCLUDE_REGEX, text):
        logger.info("Message excluded by exclude pattern")
        return

    # 5. Include patterns
    if matches_any(INCLUDE_REGEX, text):
        logger.info("Message matched include pattern")
        forward(text)
    else:
        logger.info("Message did not match include patterns")

# -------------------------------------------------
# Start
# -------------------------------------------------
if __name__ == "__main__":
    logger.info("Starting Slackbot (Socket Mode)")
    SocketModeHandler(app, APP_TOKEN).start()

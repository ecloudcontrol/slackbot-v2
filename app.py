import os
import logging, sys, re, json
from datetime import datetime, timedelta
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.FileHandler('/appz/log/slackbot.log', mode='a', encoding='utf-8')]
)
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initializes your app with your bot token and socket mode handler
app_token = os.environ.get("APP_TOKEN")
bot_token = os.environ.get("BOT_TOKEN")
target_channel_id = os.environ.get("TARGET_CHANNEL_ID")
channel_ids = os.environ.get("CHANNEL_IDS").split(",")
python_encoding = os.environ.get("PYTHONIOENCODING")

if not app_token:
    logger.warning('APP_TOKEN not found in the vault.')

if not bot_token:
    logger.warning('BOT_TOKEN not found in the vault.')

if not target_channel_id:
    logger.warning('TARGET_CHANNEL_ID not found in env.')

if not channel_ids:
    logger.warning('CHANNEL_IDS not found in env.')

if not all([app_token, bot_token, target_channel_id, channel_ids]):
    logger.warning('Missing required environment variables. Aborting...')
    sys.exit(1)

def load_filter_patterns(patterns_file):
    try:
        with open(patterns_file, 'r') as file:
            data = json.load(file)
            pattern = data.get('patterns', [])
            logger.info('loaded patterns: {}'.format(pattern))
            return pattern
    except Exception as err:
        logger.error("Failed to load filter patterns.{}".format(err))
        sys.exit(1)

def get_channel_name(channel_id):
    response = app.client.conversations_info(channel=channel_id)
    return response['channel']['name']

def extract_triggered_message(original_message):
    # Extract the Triggered message from the original message
    logger.info("{}".format("Matching message"))
    match1 = re.search(r'(Triggered:|Recovered:)(.+)>', original_message)
    match2 = re.search(r'(Name:(.+\n.+))', original_message)
    
    if match1:
        match = match1
    elif match2:
        match = match2
    else:
        match = None
    
    if match:
        logger.info("Match output: {}".format(match.group(2)))
        return match.group(2)
    else:
        return None

def is_triggered_message_cached(triggered_message, original_message):
    if "Issue" in original_message:
        if triggered_message in recent_messages_cache:
            timestamp = recent_messages_cache[triggered_message]['time']
            if (datetime.now() - timestamp) <= timedelta(minutes=15):
                logger.info("{}".format("Triggered within 15mins"))
                return True
            else:
                del recent_messages_cache[triggered_message]
                logger.info("recent_messages_cache after delete: {}".format(recent_messages_cache))
                return False
        else:
            return False
    elif "Triggered" in original_message:
        if triggered_message in recent_messages_cache:
            timestamp = recent_messages_cache[triggered_message]['time']
            if (datetime.now() - timestamp) <= timedelta(minutes=60):
                logger.info("{}".format("Triggered within 1hr"))
                return True
            else:
                del recent_messages_cache[triggered_message]
                logger.info("recent_messages_cache after delete: {}".format(recent_messages_cache))
                return False
        else:
            return False
    else:
        return False

def update_recent_messages_cache(triggered_message, unstable=False):
    # Update the cache with the current timestamp and recovery status for the triggered message
    if triggered_message not in recent_messages_cache:
        recent_messages_cache[triggered_message] = {}
        recent_messages_cache[triggered_message]['time'] = datetime.now()
        recent_messages_cache[triggered_message]['trigger_count'] = 1
    else:
        recent_messages_cache[triggered_message]['time'] = datetime.now()
        recent_messages_cache[triggered_message]['trigger_count'] += 1 if unstable else 0
def reset_sequence(triggered_message):
    try:
        logger.info("Popping message: {}".format(triggered_message))
        pop_value = recent_messages_cache.pop(triggered_message, None)
        logger.info("recent_messages_cache after reset: {}".format(recent_messages_cache))
        logger.info("Popped value: {}".format(pop_value))
    except Exception as err:
        logger.error("{}".format(err))

def send_message_to_channel(app, logger, message, original_message, channel_name, target_channel_id, triggers):
    triggered_message = extract_triggered_message(original_message)

    try:
        logger.info("sending message to target channel: {}".format(original_message))
        channel_id = message['channel']
        response = app.client.chat_getPermalink(channel=message['channel'], message_ts=message['ts'])
        original_message_permalink = response['permalink']
        original_message_link = "<{}|View message>".format(original_message_permalink)
        channel_link = "<#{}|{}>".format(channel_id, channel_name)
        final_message = "{}\n Link: {}\n Channel: {}".format(original_message, original_message_link, channel_link)

        if "Recovered" not in original_message and "resolved" not in original_message:
            # Post the message in the target channel and update the recent messages cache
            app.client.chat_postMessage(
                channel=target_channel_id,
                text=final_message,
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": final_message},
                        "accessory": {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "Have you fixed it?"
                            },
                            "action_id": "button_click"
                        }
                    }
                ],
                unfurl_links=False
            )
            # Update the recent messages cache
            if "started" in original_message and any(trigger in original_message for trigger in triggers):
                triggered_message = extract_triggered_message(original_message)
                update_recent_messages_cache(triggered_message, unstable=True)
            else:
                update_recent_messages_cache(triggered_message)
            logger.info("recent_messages_cache after update: {}".format(recent_messages_cache))
        else:
            # Post the message in the target channel without updating the cache
            app.client.chat_postMessage(
                channel=target_channel_id,
                text=final_message,
                blocks=[
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": final_message} 
                    }
                ],
                unfurl_links=False
            )
            logger.info("Recovered or resolved message: {}".format(original_message))
    except Exception:
        logging.error("Exception handled, ", exc_info=True)

def handle_filtered_message(message, client):
    # Get the original message text
    original_message = message['text']
    attachments = message.get('attachments', [])
    title = attachments[0]['title'] if attachments else None
    triggered_message = title if title else original_message
    channel_id = message['channel']
    channel_name = get_channel_name(channel_id)
    logger.info(f"Received message: {original_message}")
    #triggers = ["Triggered"]
    #recovers = ["Recovered","resolved"]
    triggers = ["Disaster", "High"]
    
    #for any trigger:
    if "Triggered" in triggered_message or ("started" in original_message and any(trigger in original_message for trigger in triggers)):
        triggered_message = extract_triggered_message(original_message)
        if not is_triggered_message_cached(triggered_message, original_message):
            send_message_to_channel(app, logger, message, original_message, channel_name, target_channel_id, triggers)
        else:
            if "started" in original_message and any(trigger in original_message for trigger in triggers):
                triggered_message = extract_triggered_message(original_message)
                update_recent_messages_cache(triggered_message, unstable=True)
            else:
                update_recent_messages_cache(triggered_message)
            logger.info("recent_messages_cache after update: {}".format(recent_messages_cache))

    elif "Recovered" in triggered_message or ("resolved" in original_message and any(trigger in original_message for trigger in triggers)):
        triggered_message = extract_triggered_message(original_message)
        if "resolved" in original_message and any(trigger in original_message for trigger in triggers) and recent_messages_cache[triggered_message]['trigger_count'] < 3 :
            logger.info("Skipping due to trigger count < 3: {}".format(recent_messages_cache[triggered_message]['trigger_count']))
        else:
            logger.info("Resetting message: {}".format(original_message))
            reset_sequence(triggered_message)
            send_message_to_channel(app, logger, message, original_message, channel_name, target_channel_id, triggers)

    logger.info("{}".format("Finished session"))


try:
    app = App(token=bot_token)
    recent_messages_cache = {}
except Exception as err:
    logger.error('{}'.format(err))
else:
    app.debug = True

@app.message(re.compile("|".join(load_filter_patterns("/appz/scripts/webapps/patterns.json"))))
def filter_messages(message, client):
    if message['channel'] in channel_ids:
        handle_filtered_message(message, client)

@app.action("button_click")
def action_button_click(body, ack, client):
    # Acknowledge the action
    ack()
    app.logger.info(body)

    # Get the original message's timestamp
    original_timestamp = body["message"]["ts"]

    # Add a white check mark reaction to the original message
    client.reactions_add(
        channel=body["channel"]["id"],
        name="white_check_mark",
        timestamp=original_timestamp
    )

@app.event("message")
def handle_message_events(body, logger):
    logger.info(body)

if __name__ == "__main__":
    SocketModeHandler(app, app_token).start()

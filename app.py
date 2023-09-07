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
            include_patterns = data.get('include_patterns', [])
            exclude_patterns = data.get('exclude_patterns', [])
            logger.info('Loaded patterns: Include={}, Exclude={}'.format(include_patterns, exclude_patterns))
            return include_patterns, exclude_patterns
    except Exception as err:
        logger.error("Failed to load filter patterns. {}".format(err))
        sys.exit(1)

def get_channel_name(channel_id):
    response = app.client.conversations_info(channel=channel_id)
    return response['channel']['name']

def extract_triggered_message(original_message, pattern):
    # Extract the Triggered message from the original message
    logger.info("{}".format("Matching message"))
    match1 = re.search(pattern, original_message)
    match2 = re.search(r'(Name:(.+\n.+)())', original_message)
    
    if match1:
        match = match1
        #logger.info("Match output: {}".format(match.group(2)))
        return match.group(2),match.group(3)
    elif match2:
        match = match2
        #logger.info("Match output: {}".format(match.group(2)))
        return match.group(2), 'Issue'

def is_triggered_message_cached(triggered_message, original_message):
    if "Issue" in original_message:
        #logger.info("{}".format("issue in original_message"))
        if triggered_message[1] in recent_messages_cache:
            if triggered_message[0] in recent_messages_cache[triggered_message[1]]:
                logger.info("{}".format("issue in recent cache"))
                timestamp = recent_messages_cache[triggered_message[1]][triggered_message[0]]['time']
                if (datetime.now() - timestamp) <= timedelta(minutes=15):
                    logger.info("{}".format("Triggered within 15mins"))
                    return True
                else:
                    del recent_messages_cache[triggered_message[1]][triggered_message[0]]
                    logger.info("recent_messages_cache after delete: {}".format(recent_messages_cache))
                    return False
        else:
            return False
    elif "Triggered" in original_message:
        if triggered_message[1] in recent_messages_cache and triggered_message[0] in recent_messages_cache[triggered_message[1]]:
            timestamp = recent_messages_cache[triggered_message[1]][triggered_message[0]]['time']
            if (datetime.now() - timestamp) <= timedelta(minutes=60):
                logger.info("{}".format("Triggered within 1hr"))
                return True
            else:
                del recent_messages_cache[triggered_message[1]][triggered_message[0]]
                logger.info("recent_messages_cache after delete: {}".format(recent_messages_cache))
                return False
        else:
            return False
    else:
        return False

def update_recent_messages_cache(triggered_message, unstable=False):
    if triggered_message[1] not in recent_messages_cache:
        recent_messages_cache[triggered_message[1]] = {}
    if triggered_message[0] not in recent_messages_cache[triggered_message[1]]:
        recent_messages_cache[triggered_message[1]][triggered_message[0]] = {}
        recent_messages_cache[triggered_message[1]][triggered_message[0]]['time'] = datetime.now()
        recent_messages_cache[triggered_message[1]][triggered_message[0]]['trigger_count'] = 0  # Initialize here
    if unstable:
        recent_messages_cache[triggered_message[1]][triggered_message[0]]['trigger_count'] += 1
        recent_messages_cache[triggered_message[1]][triggered_message[0]]['time'] = datetime.now()
        
def reset_sequence(triggered_message, original_message):
    try:
        logger.info("Popping message: {}".format(triggered_message))
        if "Recovered" in original_message:
            pop_value = recent_messages_cache.pop(triggered_message[1], 'Nothing to pop')
        else:
            pop_value = recent_messages_cache[triggered_message[1]].pop(triggered_message[0], 'Nothing to clear')

        logger.info("recent_messages_cache after reset: {}".format(recent_messages_cache))
        logger.info("Popped value: {}".format(pop_value))
    except Exception as err:
        logger.error("{}".format(err))


def send_message_to_channel(app, logger, message, original_message, channel_name, target_channel_id, triggers, pattern, channel_id, message_ts):
    triggered_message = extract_triggered_message(original_message, pattern)

    try:
        logger.info("sending message to target channel: {}".format(original_message))
        response = app.client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
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
                triggered_message = extract_triggered_message(original_message, pattern)
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
            #logger.info("Recovered or resolved message: {}".format(original_message))
    except Exception:
        logging.error("Exception handled, ", exc_info=True)

def handle_filtered_message(message, client, event_message, event_channel, event_ts):
    # Get the original message text
    if event_message:
        #logger.info(f"this is event: {event_message}")
        triggered_message = event_message
        channel_id = event_channel
        message_ts = event_ts
        original_message = event_message
    else:
        original_message = message['text']
        #logger.info(f"normal message: {original_message}")
        triggered_message = original_message
        channel_id = message['channel']
        message_ts = message['ts']
    channel_name = get_channel_name(channel_id)
    triggers = ["Disaster", "High"]
    
    #for any trigger:
    if "Triggered" in triggered_message or ("started" in original_message and any(trigger in original_message for trigger in triggers)):
        pattern = r'(Triggered:)(.+[ ](.+)[ ].+)'
        triggered_message = extract_triggered_message(original_message, pattern)
        if not is_triggered_message_cached(triggered_message, original_message):
            send_message_to_channel(app, logger, message, original_message, channel_name, target_channel_id, triggers, pattern, channel_id, message_ts)
        else:
            if "started" in original_message and any(trigger in original_message for trigger in triggers):
                triggered_message = extract_triggered_message(original_message, pattern)
                update_recent_messages_cache(triggered_message, unstable=True)
            else:
                update_recent_messages_cache(triggered_message)
            logger.info("recent_messages_cache after update: {}".format(recent_messages_cache))

    elif "Recovered" in triggered_message or ("resolved" in original_message and any(trigger in original_message for trigger in triggers)):
        pattern = r'(Recovered:)(.+[ ](.+))'
        triggered_message = extract_triggered_message(original_message,pattern)
        try:
            if "resolved" in original_message and any(trigger in original_message for trigger in triggers) and recent_messages_cache[triggered_message[1]][triggered_message[0]]['trigger_count'] < 3 :
                logger.info("Skipping due to trigger count < 3: {}".format(recent_messages_cache[triggered_message[1]][triggered_message[0]]['trigger_count']))
            else:
                logger.info("Resetting message: {}".format(original_message))
                reset_sequence(triggered_message, original_message)
                send_message_to_channel(app, logger, message, original_message, channel_name, target_channel_id, triggers, pattern, channel_id, message_ts)
        except Exception:
            logging.error("Exception handled, ", exc_info=True)

    logger.info("{}".format("Finished session"))


try:
    app = App(token=bot_token)
    recent_messages_cache = {}
except Exception as err:
    logger.error('{}'.format(err))
else:
    app.debug = True

include_patterns, exclude_patterns = load_filter_patterns("/appz/scripts/webapps/patterns.json")


@app.message(re.compile("|".join(include_patterns)))
def filter_messages(message, client):
    if message['channel'] in channel_ids:
        logger.info(f"fetching: {message}")
        original_message = message['text']
        if not any(re.search(pattern, original_message) for pattern in exclude_patterns):
            handle_filtered_message(message, client, event_message=None, event_channel=None, event_ts=None)

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
def handle_message_events(body, logger, client):
    logger.info("event_log: {}".format(body))
    event_data = body['event']
    event_channel = body['event']['channel'] 
    event_ts = body['event']['ts']
    if 'attachments' in event_data:
        # Assuming there could be multiple attachments, process each one
        for attachment in event_data['attachments']:
            title = attachment.get('fallback', '') 
            #logger.info("title: {}".format(title))
            event_message = title
            logger.info(f"event message: {event_message}")
            handle_filtered_message(None, None, event_message, event_channel, event_ts)
    else:
        logger.info("No 'events' found")

if __name__ == "__main__": 
    SocketModeHandler(app, app_token).start()

import os
import logging
import sys
import re
import json
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

def extract_triggered_message(original_message, pattern=None):
    # Extract the Triggered message from the original message
    logger.info("{}".format("Matching message"))
    
    # Handle each include pattern specifically for stable keys
    # 1. Prod parser down: (prod\\sparser\\sis\\sdown) -> extract parser ID
    if re.search(r'prod\s+parser\s+is\s+down', original_message):
        match = re.search(r'([^\s]+)\s+prod parser is down', original_message)
        if match:
            return match.group(1), "PROD_PARSER"
    
    # 2. Issue: (Issue)(.+)\\n(.+)\\n(.+) -> use first post-newline group as desc, "Issue" as type
    if "Issue" in original_message:
        issue_pattern = r'(Issue)(.+)\n(.+)\n(.+)'
        match = re.search(issue_pattern, original_message)
        if match:
            desc = match.group(2).strip()  # First .+ after Issue
            return desc, "Issue"
    
    # 3. Prod Full Node Down: (Prod\\s-\\sFull\\sNode\\sDown\\s-\\sProd) -> fixed type
    if re.search(r'Prod\s-\sFull\sNode\sDown\s-\sProd', original_message):
        return "full-node", "PROD_NODE_DOWN"  # Stable key
    
    # 4. Kafka lag: (\\b\\w+\\s+increased\\s+lag\\s+on\\s+Kafka\\b) -> extract topic
    if re.search(r'\b\w+\s+increased\s+lag\s+on\s+Kafka\b', original_message):
        match = re.search(r'(\w+)\s+increased\s+lag\s+on\s+Kafka', original_message)
        if match:
            return match.group(1), "KAFKA_LAG"
    
    # 5. RDS CPU: (\[AWS\]\s+RDS\s+CPU\s+utilization\s+is\s+(?:high|back\s+to\s+normal)\s+on\s+dbinstanceidentifier:[\w-]+)
    if "[AWS] RDS CPU utilization" in original_message:
        rds_pattern = r'on\s+dbinstanceidentifier:([\w-]+)'
        match = re.search(rds_pattern, original_message)
        if match:
            return match.group(1), "RDS_CPU"  # DB ID as desc, shared for high/normal
    
    # Fallback: Use provided pattern (e.g., for custom Triggered: lines)
    if pattern:
        match = re.search(pattern, original_message)
        if match:
            g2 = match.group(2) if len(match.groups()) >= 2 else ""
            g3 = match.group(3) if len(match.groups()) >= 3 else "Generic"
            return g2.strip(), g3
    
    return "", ""  # No match

def is_triggered_message_cached(triggered_message, original_message):
    if not triggered_message[0] or not triggered_message[1]:
        return False  # Invalid extraction, don't cache
    
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
    if not triggered_message[0] or not triggered_message[1]:
        return  # Invalid, skip
    
    if triggered_message[1] not in recent_messages_cache:
        recent_messages_cache[triggered_message[1]] = {}
    if triggered_message[0] not in recent_messages_cache[triggered_message[1]]:
        recent_messages_cache[triggered_message[1]][triggered_message[0]] = {}
        recent_messages_cache[triggered_message[1]][triggered_message[0]]['time'] = datetime.now()
        recent_messages_cache[triggered_message[1]][triggered_message[0]]['trigger_count'] = 0 # Initialize here
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
        is_recovered = "Recovered" in original_message or "resolved" in original_message
        if not is_recovered:
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
    except Exception as e:
        logger.error("Exception in send_message_to_channel", exc_info=True)

def handle_filtered_message(message, client, event_message, event_channel, event_ts):
    # Get the original message text
    if event_message:
        #logger.info(f"this is event: {event_message}")
        alert_text = event_message
        channel_id = event_channel
        message_ts = event_ts
        original_message = event_message
    else:
        original_message = message['text']
        #logger.info(f"normal message: {original_message}")
        alert_text = original_message
        channel_id = message['channel']
        message_ts = message['ts']
    channel_name = get_channel_name(channel_id)
    triggers = ["Disaster", "High"]
   
    # Extract key once for use in both branches
    extracted_key = extract_triggered_message(original_message)
    if not extracted_key[0]:
        logger.info("No valid extraction, skipping")
        return
   
    # For triggered or started
    if "Triggered" in alert_text or ("started" in original_message and any(trigger in original_message for trigger in triggers)):
        # Set pattern based on content (for fallback general matching)
        if "prod parser is down" in original_message:
            pattern = r'(Triggered:)(\s*([^\s]+)\s+(.+))'
        elif "increased lag on Kafka" in original_message:
            pattern = r'(Triggered:)(\s*([\w]+)\s*(.+))'
        elif "[AWS] RDS CPU utilization" in original_message:
            pattern = r'(Triggered:)(.+)'  # Fallback; extraction handled specially
        elif re.search(r'Prod\s-\sFull\sNode\sDown\s-\sProd', original_message):
            pattern = r'(Triggered:)(.+)'  # Fallback
        else:
            pattern = r'(Triggered:)(.+[ ](.+)[ ].+)'
        
        # Use extracted_key (already computed, specials handled)
        if not is_triggered_message_cached(extracted_key, original_message):
            send_message_to_channel(app, logger, message, original_message, channel_name, target_channel_id, triggers, pattern, channel_id, message_ts)
        else:
            if "started" in original_message and any(trigger in original_message for trigger in triggers):
                update_recent_messages_cache(extracted_key, unstable=True)
            else:
                update_recent_messages_cache(extracted_key)
            logger.info("recent_messages_cache after update: {}".format(recent_messages_cache))
    # For recovered or resolved
    elif "Recovered" in alert_text or ("resolved" in original_message and any(trigger in original_message for trigger in triggers)):
        # Set pattern for recovered (similar logic)
        if "prod parser is down" in original_message:
            pattern = r'(Recovered:)(\s*([^\s]+)\s+(.+))'
        elif "increased lag on Kafka" in original_message:
            pattern = r'(Recovered:)(\s*([\w]+)\s*(.+))'
        elif "[AWS] RDS CPU utilization" in original_message:
            pattern = r'(Recovered:)(.+)'  # Fallback
        elif re.search(r'Prod\s-\sFull\sNode\sDown\s-\sProd', original_message):
            pattern = r'(Recovered:)(.+)'  # Fallback
        else:
            pattern = r'(Recovered:)(.+[ ](.+))'
        
        # Use extracted_key
        try:
            cache_entry = recent_messages_cache.get(extracted_key[1], {}).get(extracted_key[0], {})
            trigger_count = cache_entry.get('trigger_count', 0)
            if "resolved" in original_message and any(trigger in original_message for trigger in triggers) and trigger_count < 3:
                logger.info("Skipping due to trigger count < 3: {}".format(trigger_count))
            else:
                logger.info("Resetting message: {}".format(original_message))
                reset_sequence(extracted_key, original_message)
                send_message_to_channel(app, logger, message, original_message, channel_name, target_channel_id, triggers, pattern, channel_id, message_ts)
        except Exception as e:
            logger.error("Exception in recovered handling", exc_info=True)
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
    event_data = body['event']
    event_channel = event_data['channel']
    event_ts = event_data['ts']
    logger.info(f"EVENT DEBUG: ...")  # From above
    
    if event_channel not in channel_ids:
        logger.info(f"Channel {event_channel} not monitored, skipping")
        return
    
    # Extract text: Prefer attachments fallback, else event text
    event_text = ""
    if 'attachments' in event_data:
        for att in event_data['attachments']:
            event_text += att.get('fallback', '') + "\n"
    elif 'text' in event_data:
        event_text = event_data['text']
    else:
        logger.info("No text or attachments, skipping")
        return
    
    # Check patterns on full text
    if any(re.search(pat, event_text) for pat in include_patterns) and not any(re.search(pat, event_text) for pat in exclude_patterns):
        logger.info(f"Event match: {event_text[:100]}...")
        handle_filtered_message(None, None, event_text, event_channel, event_ts)
    else:
        logger.info("Event no match")

if __name__ == "__main__":
    SocketModeHandler(app, app_token).start()

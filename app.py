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
        service = match1.group(2)
        desc = match1.group(3).strip()
        full_key = f"{service} {desc}"
        return full_key, service
    elif match2:
        full_key = match2.group(2).strip()
        return full_key, 'Issue'
    return None, None
def get_permalink(channel_id, message_ts):
    try:
        response = app.client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
        return response['permalink']
    except Exception as e:
        logger.error(f"Failed to get permalink: {e}")
        return None
def build_merged_message(sources, alert_text, is_recovery=False):
    channel_links = [f"<#{s['id']}|{s['name']}>" for s in sources]
    link_texts = []
    for s in sources:
        if s['link']:
            link_texts.append(f"<{s['link']}|View message>")
        else:
            link_texts.append(f"<#{s['id']}|{s['name']}>")
    channels_str = ", ".join(channel_links)
    links_str = ", ".join(link_texts)
    final_message = f"{alert_text}\nLinks: {links_str}\nChannels: {channels_str}"
    return final_message
def post_alert_message(alert_text, sources, target_channel_id):
    final_message = build_merged_message(sources, alert_text, False)
    blocks = [
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
    ]
    try:
        response = app.client.chat_postMessage(
            channel=target_channel_id,
            text=final_message,
            blocks=blocks,
            unfurl_links=False
        )
        logger.info("sending message to target channel: {}".format(alert_text))
        return response['ts']
    except Exception as e:
        logger.error("Exception posting alert: {}".format(e), exc_info=True)
        return None
def post_recovery_message(recovery_text, sources, target_channel_id):
    final_message = build_merged_message(sources, recovery_text, True)
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": final_message}
        }
    ]
    try:
        app.client.chat_postMessage(
            channel=target_channel_id,
            text=final_message,
            blocks=blocks,
            unfurl_links=False
        )
        logger.info("sending message to target channel: {}".format(recovery_text))
    except Exception as e:
        logger.error("Exception posting recovery: {}".format(e), exc_info=True)
def edit_alert_message(entry, target_channel_id):
    final_message = build_merged_message(entry['source_channels'], entry['original_alert'], False)
    blocks = [
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
    ]
    try:
        app.client.chat_update(
            channel=target_channel_id,
            ts=entry['posted_ts'],
            text=final_message,
            blocks=blocks
        )
        logger.info("Edited alert message with additional channel")
    except Exception as e:
        logger.error("Exception editing alert: {}".format(e), exc_info=True)
def is_triggered_message_cached(triggered_message, original_message):
    if "Issue" in original_message:
        if triggered_message[1] in recent_messages_cache:
            if triggered_message[0] in recent_messages_cache[triggered_message[1]]:
                logger.info("Cache hit for Issue")
                timestamp = recent_messages_cache[triggered_message[1]][triggered_message[0]]['time']
                if (datetime.now() - timestamp) <= timedelta(minutes=15):
                    return True
                else:
                    del recent_messages_cache[triggered_message[1]][triggered_message[0]]
                    logger.info("Expired Issue cache entry")
                    return False
        logger.info("No cache for Issue")
        return False
    else:
        if triggered_message[1] in recent_messages_cache and triggered_message[0] in recent_messages_cache[triggered_message[1]]:
            logger.info("Cache hit for Triggered")
            timestamp = recent_messages_cache[triggered_message[1]][triggered_message[0]]['time']
            if (datetime.now() - timestamp) <= timedelta(minutes=60):
                return True
            else:
                del recent_messages_cache[triggered_message[1]][triggered_message[0]]
                logger.info("Expired Triggered cache entry")
                return False
        logger.info("No cache for Triggered")
        return False
def is_recovery_cached(triggered_message, original_message):
    service = triggered_message[1]
    full_key = triggered_message[0]
    if service in recovered_alerts and full_key in recovered_alerts[service]:
        logger.info("Recovery cache hit")
        timestamp = recovered_alerts[service][full_key]['time']
        if (datetime.now() - timestamp) <= timedelta(minutes=60):
            return True
        else:
            del recovered_alerts[service][full_key]
            logger.info("Expired recovery cache entry")
            return False
    logger.info("No recovery cache")
    return False
def update_recent_messages_cache(triggered_message, unstable=False, source=None, original_alert=None, posted_ts=None):
    service = triggered_message[1]
    full_key = triggered_message[0]
    if service not in recent_messages_cache:
        recent_messages_cache[service] = {}
    if full_key not in recent_messages_cache[service]:
        recent_messages_cache[service][full_key] = {
            'time': datetime.now(),
            'trigger_count': 0,
            'source_channels': [],
            'posted_ts': None,
            'original_alert': None,
            'recovered': False
        }
    entry = recent_messages_cache[service][full_key]
    if source:
        entry['source_channels'].append(source)
    if original_alert is not None:
        entry['original_alert'] = original_alert
    if posted_ts is not None:
        entry['posted_ts'] = posted_ts
    if unstable:
        entry['trigger_count'] += 1
    entry['time'] = datetime.now()
    logger.info("recent_messages_cache after update: {}".format(recent_messages_cache))
def update_recovered_cache(triggered_message):
    service = triggered_message[1]
    full_key = triggered_message[0]
    if service not in recovered_alerts:
        recovered_alerts[service] = {}
    recovered_alerts[service][full_key] = {'time': datetime.now()}
    logger.info("Updated recovered cache for: {}".format(full_key))
def reset_sequence(triggered_message, original_message):
    try:
        service = triggered_message[1]
        full_key = triggered_message[0]
        logger.info("Popping message: {}".format(triggered_message))
        if service in recent_messages_cache:
            recent_messages_cache[service].pop(full_key, None)
            if not recent_messages_cache[service]:  # Optional: clean empty service
                del recent_messages_cache[service]
        logger.info("recent_messages_cache after reset: {}".format(recent_messages_cache))
    except Exception as err:
        logger.error("{}".format(err))
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
    unified_pattern = r'(Triggered|Recovered):\s*([^\s]+)\s+(.+)'
  
    #for any trigger:
    if "Triggered" in triggered_message or ("started" in original_message and any(trigger in original_message for trigger in triggers)):
        extracted = extract_triggered_message(original_message, unified_pattern)
        if not extracted[0]:  # No match
            logger.warning("No pattern match for triggered message")
            return
        triggered_message = extracted
        permalink = get_permalink(channel_id, message_ts)
        source = {
            'name': channel_name,
            'id': channel_id,
            'link': permalink
        } if permalink else {
            'name': channel_name,
            'id': channel_id,
            'link': None
        }
        unstable = "started" in original_message and any(trigger in original_message for trigger in triggers)
        if not is_triggered_message_cached(triggered_message, original_message):
            logger.info("New alert - posting")
            posted_ts = post_alert_message(original_message, [source], target_channel_id)
            if posted_ts:
                update_recent_messages_cache(triggered_message, unstable=unstable, source=source, original_alert=original_message, posted_ts=posted_ts)
        else:
            logger.info("Updating existing alert")
            update_recent_messages_cache(triggered_message, unstable=unstable, source=source, original_alert=None, posted_ts=None)
            entry = recent_messages_cache[triggered_message[1]][triggered_message[0]]
            if entry['posted_ts']:
                edit_alert_message(entry, target_channel_id)
    elif "Recovered" in triggered_message or ("resolved" in original_message and any(trigger in original_message for trigger in triggers)):
        extracted = extract_triggered_message(original_message, unified_pattern)
        if not extracted[0]:  # No match
            logger.warning("No pattern match for recovery message")
            return
        triggered_message = extracted
        if is_recovery_cached(triggered_message, original_message):
            logger.info("Skipping duplicate recovery")
            return
        is_resolved = "resolved" in original_message and any(trigger in original_message for trigger in triggers)
        skip = False
        entry = None
        try:
            service = triggered_message[1]
            full_key = triggered_message[0]
            if service in recent_messages_cache and full_key in recent_messages_cache[service]:
                entry = recent_messages_cache[service][full_key]
                if is_resolved and entry['trigger_count'] < 3 :
                    skip = True
                    logger.info("Skipping due to trigger count < 3: {}".format(entry['trigger_count']))
        except Exception:
            pass
        if not skip:
            logger.info("Processing recovery: {}".format(original_message))
            permalink = get_permalink(channel_id, message_ts)
            source = {
                'name': channel_name,
                'id': channel_id,
                'link': permalink
            } if permalink else {
                'name': channel_name,
                'id': channel_id,
                'link': None
            }
            if entry:
                if not entry['recovered']:
                    post_recovery_message(original_message, entry['source_channels'], target_channel_id)
                    entry['recovered'] = True
                    update_recovered_cache(triggered_message)
            else:
                post_recovery_message(original_message, [source], target_channel_id)
                update_recovered_cache(triggered_message)
            reset_sequence(triggered_message, original_message)
        else:
            logger.info("Skipped recovery message: {}".format(original_message))
    logger.info("{}".format("Finished session"))
try:
    app = App(token=bot_token)
    recent_messages_cache = {}
    recovered_alerts = {}
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
    event_channel = body['event']['channel']
    event_ts = body['event']['ts']
    if event_channel in channel_ids and 'attachments' in event_data:
        # Assuming there could be multiple attachments, process each one
        for attachment in event_data['attachments']:
            title = attachment.get('fallback', '')
            #logger.info("title: {}".format(title))
            event_message = title
            if not any(re.search(pattern, event_message) for pattern in exclude_patterns) and any(re.search(pattern, event_message) for pattern in include_patterns):
                logger.info(f"event message: {event_message}")
                handle_filtered_message(None, None, event_message, event_channel, event_ts)
            else:
                logger.info("event_log: {}".format(body))
    else:
        logger.info("No 'events' found")
if __name__ == "__main__":
    SocketModeHandler(app, app_token).start()

```python
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
def extract_description_and_status(original_message):
    match = re.search(r'(?i)(Triggered|Recovered|started|resolved|Issue):\s*(.+)', original_message)
    if match:
        status = match.group(1).lower()
        description = match.group(2).strip()
        return description, status
    match2 = re.search(r'Name:\s*(.+)', original_message)
    if match2:
        return match2.group(1).strip(), 'issue'
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
        response = app.client.chat_postMessage(
            channel=target_channel_id,
            text=final_message,
            blocks=blocks,
            unfurl_links=False
        )
        logger.info("sending message to target channel: {}".format(recovery_text))
        return response['ts']
    except Exception as e:
        logger.error("Exception posting recovery: {}".format(e), exc_info=True)
        return None
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
def edit_recovery_message(entry, target_channel_id):
    final_message = build_merged_message(entry['recovery_sources'], entry['recovery_text'], True)
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": final_message}
        }
    ]
    try:
        app.client.chat_update(
            channel=target_channel_id,
            ts=entry['recovered_posted_ts'],
            text=final_message,
            blocks=blocks
        )
        logger.info("Edited recovery message with additional channel")
    except Exception as e:
        logger.error("Exception editing recovery: {}".format(e), exc_info=True)
def is_triggered_message_cached(key):
    if key not in recent_messages_cache:
        return False
    entry = recent_messages_cache[key]
    if entry.get('recovered', False):
        del recent_messages_cache[key]
        return False
    timestamp = entry['time']
    delta = timedelta(minutes=15) if entry['status'] == 'issue' else timedelta(minutes=60)
    if (datetime.now() - timestamp) <= delta:
        logger.info("Triggered within {}mins".format(15 if entry['status'] == 'issue' else 60))
        return True
    else:
        del recent_messages_cache[key]
        logger.info("recent_messages_cache after delete: {}".format(recent_messages_cache))
        return False
def update_recent_messages_cache(key, unstable=False, source=None, original_alert=None, posted_ts=None, status=None):
    if key not in recent_messages_cache:
        recent_messages_cache[key] = {
            'time': datetime.now(),
            'trigger_count': 0,
            'source_channels': [],
            'posted_ts': None,
            'original_alert': None,
            'recovered': False,
            'recovery_sources': [],
            'recovery_text': None,
            'recovered_posted_ts': None,
            'recovery_start_time': None,
            'status': None
        }
    entry = recent_messages_cache[key]
    if status is not None and entry['status'] is None:
        entry['status'] = status
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
def reset_sequence(key, original_message):
    try:
        logger.info("Popping message: {}".format(key))
        pop_value = recent_messages_cache.pop(key, 'Nothing to pop')
        logger.info("recent_messages_cache after reset: {}".format(recent_messages_cache))
        logger.info("Popped value: {}".format(pop_value))
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
    description, status = extract_description_and_status(original_message)
    if description is None:
        logger.info("No description extracted, skipping")
        return
    channel_name = get_channel_name(channel_id)
    triggers = ["Disaster", "High"]
    is_alert = status == 'triggered' or (status == 'started' and any(trigger in original_message for trigger in triggers))
    is_recovery = status == 'recovered' or (status == 'resolved' and any(trigger in original_message for trigger in triggers))
    if is_alert:
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
        unstable = status == 'started' and any(trigger in original_message for trigger in triggers)
        if not is_triggered_message_cached(description):
            posted_ts = post_alert_message(original_message, [source], target_channel_id)
            if posted_ts:
                update_recent_messages_cache(description, unstable=unstable, source=source, original_alert=original_message, posted_ts=posted_ts, status=status)
        else:
            update_recent_messages_cache(description, unstable=unstable, source=source, original_alert=None, posted_ts=None, status=None)
            entry = recent_messages_cache[description]
            if entry['posted_ts']:
                edit_alert_message(entry, target_channel_id)
    elif is_recovery:
        timeout_minutes = 15 if recent_messages_cache.get(description, {}).get('status') == 'issue' else 60
        timeout = timedelta(minutes=timeout_minutes)
        entry = None
        skip = False
        now = datetime.now()
        try:
            if description in recent_messages_cache:
                entry = recent_messages_cache[description]
                if entry.get('recovered_posted_ts') and (now - entry['recovery_start_time']) > timedelta(minutes=15):
                    del recent_messages_cache[description]
                    entry = None
                    logger.info("Expired recovery window, treating as new")
                elif (now - entry['time']) > timeout:
                    del recent_messages_cache[description]
                    entry = None
                else:
                    is_resolved = status == 'resolved' and any(trigger in original_message for trigger in triggers)
                    if is_resolved and entry['trigger_count'] < 3 :
                        skip = True
                        logger.info("Skipping due to trigger count < 3: {}".format(entry['trigger_count']))
        except Exception:
            pass
        if skip:
            logger.info("Skipped recovery message: {}".format(original_message))
            return
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
        if not entry:
            # Standalone recovery (rare)
            post_recovery_message(original_message, [source], target_channel_id)
            return
        # Append to recovery sources
        entry['recovery_sources'].append(source)
        if entry['recovered_posted_ts'] is None:
            # First recovery
            posted_ts = post_recovery_message(original_message, entry['recovery_sources'], target_channel_id)
            if posted_ts:
                entry['recovered_posted_ts'] = posted_ts
                entry['recovery_text'] = original_message
                entry['recovery_start_time'] = now
                entry['recovered'] = True
                logger.info("Resetting message: {}".format(original_message))
        else:
            # Subsequent recovery
            recovery_start_time = entry['recovery_start_time']
            if (now - recovery_start_time) <= timedelta(minutes=15):
                edit_recovery_message(entry, target_channel_id)
                logger.info("Merged subsequent recovery: {}".format(original_message))
            else:
                logger.info("Late recovery, skipping: {}".format(original_message))
        if entry['recovered'] and entry['recovery_start_time'] and (now - entry['recovery_start_time']) > timedelta(minutes=15):
            del recent_messages_cache[description]
            logger.info("Cleaned up cache entry after recovery window")
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
```

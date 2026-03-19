import json
import codecs

with codecs.open('real_messages.json', 'r', 'utf-8') as f:
    messages = json.load(f)

print(f"Total messages: {len(messages)}")
for msg in messages:
    print(f"Channel: {msg['channel']}, ID: {msg.get('id')}")
    print(f"Time: {msg.get('msg_dt')}")
    print(f"Text:\n{msg.get('text')}")
    print("-" * 40)

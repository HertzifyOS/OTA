#!/usr/bin/env python3
import json
import sys
import os
import requests
import html

# ---------- Args ----------
if len(sys.argv) < 2:
    print("Usage: telegram_notify.py <device_json>")
    sys.exit(1)

json_path = sys.argv[1]

# ---------- Env ----------
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN")
CHAT_ID = os.environ.get("TG_CHANNEL_ID")
MAINTAINER = os.environ.get(
    "MAINTAINER",
    os.environ.get("GITHUB_ACTOR", "Unknown")
)

if not BOT_TOKEN or not CHAT_ID:
    print("Missing TG_BOT_TOKEN or TG_CHANNEL_ID")
    sys.exit(1)

# ---------- Load JSON ----------
with open(json_path, "r") as f:
    content = f.read().strip()
    if not content:
        print(f"Skipping empty file: {json_path}")
        sys.exit(0)
    data = json.loads(content)
entry = data["response"][0]

filename = entry["filename"]
version = entry["version"]
romtype = entry["romtype"]
size_gb = entry["size"] / (1024 ** 3)
download_url = entry["url"]

device = os.path.basename(json_path).replace(".json", "")

# ---------- Escape HTML ----------
device_h = html.escape(device)
version_h = html.escape(version)
romtype_h = html.escape(romtype)
filename_h = html.escape(filename)
maintainer_h = html.escape(MAINTAINER)

maintainer_link = f"https://github.com/{MAINTAINER}"

hashtag = f"#{device_h}"

# ---------- Message (HTML) ----------
text = f"""
<b>🚀 HertzifyOS Update Released</b>

📱 <b>Device:</b> <code>{device_h}</code>
📦 <b>Filename:</b> <code>{filename_h}</code>
🧩 <b>Version:</b> <code>{version_h}</code>
🏷 <b>Type:</b> <code>{romtype_h}</code>
💾 <b>Size:</b> <code>{size_gb:.2f} GB</code>
👤 <b>Maintainer:</b> <a href="{maintainer_link}">{maintainer_h}</a>

⬇️ <b><a href="{download_url}">Download</a></b>

{hashtag}

— <b>HertzifyOS</b> —
"""

payload = {
    "chat_id": CHAT_ID,
    "text": text,
    "parse_mode": "HTML",
    "disable_web_page_preview": True
}

resp = requests.post(
    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
    json=payload,
    timeout=20
)

if not resp.ok:
    print("Telegram sendMessage failed:")
    print(resp.text)
    resp.raise_for_status()

message_id = resp.json()["result"]["message_id"]

# ---------- Auto pin ----------
requests.post(
    f"https://api.telegram.org/bot{BOT_TOKEN}/pinChatMessage",
    json={
        "chat_id": CHAT_ID,
        "message_id": message_id,
        "disable_notification": True
    },
    timeout=10
)

print("Telegram message sent & pinned successfully")

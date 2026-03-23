import os
import time
import json
import asyncio
import subprocess
from collections import deque
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ChatType

# ================= CONFIG =================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DEST_CHANNEL = int(os.getenv("DEST_CHANNEL", 0))

OWNER_ID = 5344078567
ALLOWED_USERS = [5351848105]
ALLOWED_GROUPS = [-1003810374456]

app = Client("EncoderBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

users = {}
active_process = {}
task_queue = deque()
current_user = None
current_task = None
queue_lock = asyncio.Lock()
in_queue = set()

# ================= UTILS =================

def is_authorized(message: Message) -> bool:
    if not message.from_user:
        return False
    user_id = message.from_user.id
    chat_id = message.chat.id

    if message.text and message.text.lower().startswith("/start"):
        return True
    if user_id == OWNER_ID:
        return True
    if message.chat.type == ChatType.PRIVATE:
        return user_id in ALLOWED_USERS
    return chat_id in ALLOWED_GROUPS

def progress_bar(percent):
    total_blocks = 12
    filled = int((percent / 100) * total_blocks)
    return "█" * filled + "░" * (total_blocks - filled)

def get_duration(file):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", file],
            capture_output=True, text=True, check=True
        )
        data = json.loads(result.stdout)
        duration_str = data.get("format", {}).get("duration")

        if duration_str and duration_str != 'N/A':
            return float(duration_str)
        return 0.0

    except Exception as e:
        print(f"Duration error: {e}")
        return 0.0

# ================= DOWNLOAD =================

async def download_with_progress(client, file_id, save_as, status_msg, label):
    def progress_callback(current, total):
        if total <= 0:
            return
        percent = int((current / total) * 100)
        bar = progress_bar(percent)
        try:
            asyncio.create_task(
                status_msg.edit(f"📥 Downloading {label}...\n\n{bar} {percent}%")
            )
        except:
            pass

    try:
        return await client.download_media(file_id, file_name=save_as, progress=progress_callback)
    except:
        return None

# ================= COMMANDS =================

@app.on_message(filters.command("start"))
async def start(client, message: Message):
    await message.reply("👋 Send video + subtitle to start encoding")

@app.on_message(filters.command("hsub"))
async def hsub_handler(client, message: Message):
    if not is_authorized(message):
        return

    replied = message.reply_to_message
    if not replied or not (replied.video or replied.document):
        await message.reply("❌ Reply to a video")
        return

    media = replied.video or replied.document
    users[message.from_user.id] = {
        "video": {
            "file_id": media.file_id,
            "file_name": getattr(media, 'file_name', "video.mp4")
        }
    }

    await message.reply("📄 Now send subtitle")

# ================= FILE HANDLER =================

@app.on_message(filters.video | filters.document)
async def file_handler(client, message: Message):
    if not is_authorized(message):
        return

    user_id = message.from_user.id

    if user_id not in users:
        return

    if message.document and message.document.file_name.endswith((".srt", ".ass", ".ssa", ".vtt")):

        users[user_id]["subtitle"] = {
            "file_id": message.document.file_id,
            "file_name": message.document.file_name
        }

        task_queue.append({
            "user_id": user_id,
            "message": message,
            "video_info": users[user_id]["video"],
            "subtitle_info": users[user_id]["subtitle"]
        })

        await message.reply(f"✅ Added to queue: {len(task_queue)}")
        del users[user_id]

# ================= ENCODING =================

async def encode_video(user_id, video_path, sub_path, output_path, duration, msg):
    cmd = [
        "ffmpeg", "-i", video_path,
        "-vf", f"subtitles='{sub_path}'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-c:a", "copy",
        "-progress", "pipe:1", "-nostats",
        output_path
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL
    )

    active_process[user_id] = process
    last_update_time = time.time()

    if duration <= 0:
        await msg.edit("🔥 Encoding...")
        await process.wait()
        return os.path.exists(output_path)

    while True:
        line = await process.stdout.readline()
        if not line:
            break

        line = line.decode(errors="ignore").strip()

        if line.startswith("out_time_ms="):
            value = line.split("=")[1].strip()

            if value == "N/A":
                continue

            try:
                current_time = int(value) / 1000000
            except ValueError:
                continue

            percent = min(int((current_time / duration) * 100), 100)

            if time.time() - last_update_time >= 5:
                bar = progress_bar(percent)
                await msg.edit(f"🔥 Encoding...\n\n[{bar}] {percent}%")
                last_update_time = time.time()

    await process.wait()
    return os.path.exists(output_path)

# ================= UPLOAD =================

async def upload_file(client, message, file_path):
    await client.send_video(
        chat_id=DEST_CHANNEL,
        video=file_path,
        caption="✅ Encoded"
    )
    if os.path.exists(file_path):
        os.remove(file_path)

# ================= WORKER =================

async def process_encoding(client, message, user_id, video_info, subtitle_info):
    status = await message.reply("📥 Starting...")

    v_path = await download_with_progress(client, video_info["file_id"], video_info["file_name"], status, "Video")
    s_path = await download_with_progress(client, subtitle_info["file_id"], subtitle_info["file_name"], status, "Subtitle")

    if not v_path or not s_path:
        await status.edit("❌ Download failed")
        return

    output = f"Hardsub_{video_info['file_name']}"
    duration = get_duration(v_path)

    if await encode_video(user_id, v_path, s_path, output, duration, status):
        await upload_file(client, message, output)
        await status.edit("✅ Done")
    else:
        await status.edit("❌ Failed")

# ================= QUEUE =================

async def queue_worker():
    global current_user

    while True:
        if not task_queue or current_user:
            await asyncio.sleep(2)
            continue

        task = task_queue.popleft()
        current_user = task['user_id']

        await process_encoding(
            app,
            task['message'],
            current_user,
            task['video_info'],
            task['subtitle_info']
        )

        current_user = None

# ================= MAIN =================

async def main():
    await app.start()
    print("Bot Started")
    asyncio.create_task(queue_worker())
    await idle()

if __name__ == "__main__":
    asyncio.run(main())

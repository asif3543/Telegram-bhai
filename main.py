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
current_task = None # इसे वापस जोड़ा गया
queue_lock = asyncio.Lock()

# ================= UTILS =================

def is_authorized(message: Message) -> bool:
    if not message.from_user: return False
    user_id = message.from_user.id
    chat_id = message.chat.id
    if message.text and message.text.lower().startswith("/start"): return True
    if user_id == OWNER_ID: return True
    if message.chat.type == ChatType.PRIVATE: return user_id in ALLOWED_USERS
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
    last_update_time = time.time()
    def progress_callback(current, total):
        nonlocal last_update_time
        if total <= 0: return
        percent = int((current / total) * 100)
        if time.time() - last_update_time < 2 and percent != 100: return # हर 2 सेकंड में अपडेट करें
        bar = progress_bar(percent)
        try:
            asyncio.create_task(status_msg.edit(f"📥 Downloading {label}...\n\n{bar} {percent}%"))
            last_update_time = time.time()
        except: pass

    try:
        return await client.download_media(file_id, file_name=save_as, progress=progress_callback)
    except Exception as e:
        print(f"Download failed: {e}")
        return None

# ================= COMMANDS =================

@app.on_message(filters.command("start") & filters.private)
async def start(client, message: Message):
    await message.reply("👋 Hello! I can hardsub subtitles into videos.\n\nReply to a video with `/hsub` or just send me a video, then send the subtitle file.")

@app.on_message(filters.command("hsub"))
async def hsub_handler(client, message: Message):
    if not is_authorized(message): return
    replied = message.reply_to_message
    if not replied or not (replied.video or replied.document):
        await message.reply("❌ Please reply to a video with this command.")
        return

    media = replied.video or replied.document
    users[message.from_user.id] = {"video": {"file_id": media.file_id, "file_name": getattr(media, 'file_name', "video.mp4")}}
    await message.reply("✅ Video received. Now, please send me the subtitle file (.srt, .ass, etc.).")

# ================= FILE HANDLER (FIXED LOGIC) =================

@app.on_message(filters.video | filters.document)
async def file_handler(client, message: Message):
    if not is_authorized(message): return
    user_id = message.from_user.id
    media = message.video or message.document
    if not media: return

    # --- यहाँ लॉजिक ठीक किया गया है ---
    # अगर यह एक सबटाइटल फाइल है
    if isinstance(media, Message) and media.document and media.document.file_name.endswith((".srt", ".ass", ".ssa", ".vtt")):
        if user_id not in users or "video" not in users[user_id]:
            await message.reply("❌ I have the subtitle, but please send the video first.")
            return

        users[user_id]["subtitle"] = {"file_id": media.file_id, "file_name": media.file_name}
        task_info = {'user_id': user_id, 'message': message, 'video_info': users[user_id]["video"], 'subtitle_info': users[user_id]["subtitle"]}
        task_queue.append(task_info)
        await message.reply(f"✅ Task added to queue. Your position is: {len(task_queue)}")
        del users[user_id]
    
    # अगर यह एक वीडियो फाइल है
    elif media.mime_type and "video" in media.mime_type:
        users[user_id] = {"video": {"file_id": media.file_id, "file_name": getattr(media, 'file_name', "video.mp4")}}
        await message.reply("✅ Video received. Now, please send me the subtitle file (.srt, .ass, etc.).")

# ================= ENCODING =================

async def encode_video(user_id, video_path, sub_path, output_path, duration, msg):
    cmd = ["ffmpeg", "-i", video_path, "-vf", f"subtitles='{sub_path}'", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "copy", "-progress", "pipe:1", "-nostats", output_path]
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    active_process[user_id] = process
    last_update_time = time.time()

    if duration <= 0:
        await msg.edit("🔥 Encoding... (Progress bar is unavailable)")
        await process.wait()
        stderr = await process.stderr.read()
        if process.returncode != 0:
            print(f"FFMPEG Error: {stderr.decode()}")
            return False
        return os.path.exists(output_path)

    while True:
        line = await process.stdout.readline()
        if not line: break
        line = line.decode(errors="ignore").strip()
        if line.startswith("out_time_ms="):
            try:
                current_time = int(line.split("=")[1]) / 1000000
                percent = min(int((current_time / duration) * 100), 100)
                if time.time() - last_update_time >= 5:
                    bar = progress_bar(percent)
                    await msg.edit(f"🔥 Encoding...\n\n[{bar}] {percent}%")
                    last_update_time = time.time()
            except (ValueError, IndexError):
                continue
    
    await process.wait()
    if process.returncode != 0:
        stderr = await process.stderr.read()
        print(f"FFMPEG Error: {stderr.decode()}")
        return False
    return os.path.exists(output_path)

# ================= UPLOAD =================

async def upload_file(client, message, file_path):
    upload_msg = await message.reply("📤 Uploading to channel...")
    try:
        await client.send_video(chat_id=DEST_CHANNEL, video=file_path, caption=f"✅ Encoded: `{os.path.basename(file_path)}`")
        await upload_msg.edit("✅ Done! Video sent to the channel.")
    except Exception as e:
        await upload_msg.edit(f"❌ Upload Failed: {e}")
    finally:
        if os.path.exists(file_path): os.remove(file_path)

# ================= WORKER =================

async def process_encoding(client, message, user_id, video_info, subtitle_info):
    status = await message.reply("⏳ Preparing to process...")
    v_path, s_path, output = None, None, None
    try:
        v_path = await download_with_progress(client, video_info["file_id"], video_info["file_name"], status, "Video")
        s_path = await download_with_progress(client, subtitle_info["file_id"], subtitle_info["file_name"], status, "Subtitle")
        if not v_path or not s_path:
            await status.edit("❌ Download failed. Task cancelled.")
            return

        output = f"Hardsub_{os.path.basename(v_path)}"
        duration = get_duration(v_path)

        if await encode_video(user_id, v_path, s_path, output, duration, status):
            await upload_file(client, message, output)
        else:
            await status.edit("❌ Encoding failed. Please check the logs.")
    
    except Exception as e:
        await status.edit(f"❌ An unexpected error occurred: {e}")
    finally:
        # Cleanup
        if user_id in active_process: del active_process[user_id]
        if v_path and os.path.exists(v_path): os.remove(v_path)
        if s_path and os.path.exists(s_path): os.remove(s_path)
        if output and os.path.exists(output): os.remove(output)

# ================= QUEUE (FIXED) =================

async def queue_worker():
    global current_user, current_task
    while True:
        if not task_queue or current_user:
            await asyncio.sleep(2)
            continue
        async with queue_lock:
            task = task_queue.popleft()
            current_user = task['user_id']
        
        # --- यहाँ लॉजिक ठीक किया गया है ---
        current_task = asyncio.create_task(process_encoding(app, task['message'], current_user, task['video_info'], task['subtitle_info']))
        try:
            await current_task
        except asyncio.CancelledError:
            await app.send_message(current_user, "Task was cancelled.")
        
        current_user = None
        current_task = None

# ================= MAIN =================

async def main():
    await app.start()
    print("Bot Started Successfully!")
    asyncio.create_task(queue_worker())
    await idle()

if __name__ == "__main__":
    asyncio.run(main())

import os
import re
import time
import json
import asyncio
import subprocess
from collections import deque
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ChatType, ParseMode

# ================= CONFIGURATION =================

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
    """FIXED: Robust duration fetching to handle 'N/A' errors"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", file],
            capture_output=True, text=True, check=True
        )
        data = json.loads(result.stdout)
        duration = data.get("format", {}).get("duration")
        
        # Agar format mein duration nahi hai, toh streams check karo
        if not duration or duration == 'N/A':
            for stream in data.get("streams", []):
                duration = stream.get("duration")
                if duration and duration != 'N/A': break
        
        return float(duration) if duration and duration != 'N/A' else 0.0
    except Exception:
        return 0.0

# ================= DOWNLOADER =================

async def download_with_progress(client, file_id, save_as, status_msg, label):
    last_update = 0
    async def progress_callback(current, total):    
        nonlocal last_update
        if time.time() - last_update < 5: return
        percent = min(100, int((current / total) * 100))    
        bar = progress_bar(percent)
        try: 
            await status_msg.edit(f"📥 <b>Downloading {label}...</b>\n\n{bar} {percent}%")
            last_update = time.time()
        except: pass

    try:    
        return await client.download_media(file_id, file_name=save_as, progress=progress_callback)    
    except Exception: return None

# ================= HANDLERS =================

@app.on_message(filters.command("start"))
async def start(client, message: Message):
    photo_url = "https://graph.org/file/f8fe7d78413cd236dea26-9fbf269f0f054594b0.jpg"
    caption = "<b>ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ꜰᴜʀɪɴᴀ ᴇɴᴄᴏᴅɪɴɢ ʙᴏᴛ</b>\n\n<b>Use me to hardsub subtitles into video.</b>"
    await client.send_photo(chat_id=message.chat.id, photo=photo_url, caption=caption)

@app.on_message(filters.command("hsub"))
async def hsub_handler(client, message: Message):
    if not is_authorized(message): return
    replied = message.reply_to_message
    if not replied or not (replied.video or replied.document):
        await message.reply("❌ Please reply to a video with /hsub")
        return
    media = replied.video or replied.document
    users[message.from_user.id] = {"video": {"file_id": media.file_id, "file_name": getattr(media, 'file_name', "video.mp4")}}
    await message.reply("📄 Now send subtitle (.srt / .ass / .vtt)")

@app.on_message(filters.command("cancel"))
async def cancel_handler(client, message: Message):
    user_id = message.from_user.id    
    if user_id == current_user:
        if current_task: current_task.cancel()
        if user_id in active_process: active_process[user_id].kill()
        await message.reply("❌ Your encoding has been cancelled.")
    elif user_id in in_queue:
        # Queue se remove karne ka logic
        global task_queue
        task_queue = deque([t for t in task_queue if t['user_id'] != user_id])
        in_queue.remove(user_id)
        await message.reply("❌ Task removed from queue.")
    else: await message.reply("❌ Nothing running.")

@app.on_message(filters.video | filters.document)
async def file_handler(client, message: Message):
    if not is_authorized(message): return
    user_id = message.from_user.id    
    
    # Subtitle file check
    if message.document and message.document.file_name.lower().endswith((".srt", ".ass", ".ssa", ".vtt")):
        if user_id not in users or "video" not in users[user_id]:
            await message.reply("❌ Please reply to a video with /hsub first.")
            return
        users[user_id]["subtitle"] = {"file_id": message.document.file_id, "file_name": message.document.file_name}
        task_queue.append({'user_id': user_id, 'message': message, 'video_info': users[user_id]["video"], 'subtitle_info': users[user_id]["subtitle"]})
        in_queue.add(user_id)
        await message.reply(f"✅ Task added to queue. Position: {len(task_queue)}")
        del users[user_id]
    else:
        # Video file store
        media = message.video or message.document
        users[user_id] = {"video": {"file_id": media.file_id, "file_name": getattr(media, 'file_name', "video.mp4")}}
        await message.reply("📄 Now send subtitle for this video.")

# ================= ENCODER & THUMBNAIL =================

async def generate_thumbnail(video_path, user_id):
    try:
        duration = get_duration(video_path)
        timestamp = duration / 2 if duration > 1 else 0.5
        thumb_path = f"thumb_{user_id}.jpg"
        cmd = ["ffmpeg", "-i", video_path, "-ss", str(timestamp), "-vframes", "1", "-q:v", "2", "-y", thumb_path]
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await process.wait()
        return thumb_path if os.path.exists(thumb_path) else None
    except: return None

async def encode_video(user_id, video_path, sub_path, output_path, duration, msg):
    # FIXED: Path escaping for Linux (Railway)
    sub_path_es = sub_path.replace("'", "'\\''")
    cmd = [
        "ffmpeg", "-i", video_path, "-vf", f"subtitles='{sub_path_es}'", 
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", 
        "-c:a", "copy", "-progress", "pipe:1", "-nostats", output_path
    ]
    
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    active_process[user_id] = process
    last_update = time.time()

    while True:
        line = await process.stdout.readline()
        if not line: break
        line = line.decode(errors="ignore").strip()
        if "out_time_ms=" in line:
            try:
                # FIXED: N/A check before conversion
                val = line.split("=")[1]
                if val.isdigit() and duration > 0:
                    current_time = int(val) / 1000000
                    percent = min(int((current_time / duration) * 100), 100)
                    if time.time() - last_update >= 10:
                        bar = progress_bar(percent)
                        await msg.edit(f"<b>🔥 Encoding...</b>\n\n[{bar}] {percent}%")
                        last_update = time.time()
            except: continue
    
    await process.wait()
    return os.path.exists(output_path)

# ================= CORE PROCESSOR =================

async def process_encoding(client, message, user_id, video_info, subtitle_info):
    status = await message.reply("⚙️ Initializing...")
    v_path = s_path = output = thumb = None
    
    try:
        v_path = await download_with_progress(client, video_info["file_id"], video_info["file_name"], status, "Video")
        s_path = await download_with_progress(client, subtitle_info["file_id"], subtitle_info["file_name"], status, "Subtitle")
        
        if not v_path or not s_path:
            await status.edit("❌ Download failed.")
            return

        output = f"Hardsub_{video_info['file_name']}"
        duration = get_duration(v_path)
        
        await status.edit("🔥 <b>Encoding started...</b>")
        if await encode_video(user_id, v_path, s_path, output, duration, status):
            thumb = await generate_thumbnail(output, user_id)
            await status.edit("📤 <b>Uploading to Channel...</b>")
            await client.send_video(
                chat_id=DEST_CHANNEL,
                video=output,
                thumb=thumb,
                caption=f"<b>✅ Encoded:</b> <code>{output}</code>\n<b>By: @Furinaencodingbot</b>",
                supports_streaming=True
            )
            await status.edit("✅ <b>Success! Video sent to channel.</b>")
        else:
            await status.edit("❌ <b>Encoding failed.</b>")

    except Exception as e:
        await status.edit(f"❌ <b>Error:</b> {str(e)}")
    finally:
        # Clean up
        for f in [v_path, s_path, output, thumb]:
            if f and os.path.exists(f): os.remove(f)

async def queue_worker():
    global current_user, current_task
    while True:
        if not task_queue or current_user:
            await asyncio.sleep(5)
            continue
        async with queue_lock:
            task = task_queue.popleft()
            current_user = task['user_id']
            in_queue.remove(current_user)
        
        current_task = asyncio.create_task(process_encoding(app, task['message'], current_user, task['video_info'], task['subtitle_info']))
        await current_task
        current_user = None

async def main():
    await app.start()
    print("Bot is Online!")
    asyncio.create_task(queue_worker())
    await idle()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())

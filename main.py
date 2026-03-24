import os
import re
import time
import json
import asyncio
import subprocess
from collections import deque
from pyrogram import Client, filters, idle
from pyrogram.types import Message
from pyrogram.enums import ChatType

# ================= CONFIGURATION =================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DEST_CHANNEL = int(os.getenv("DEST_CHANNEL", 0)) 

OWNER_ID = 5344078567                    
ALLOWED_USERS = [5351848105]             
ALLOWED_GROUPS = [-1003810374456]        

app = Client("EncoderBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Global Variables
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
    u_id = message.from_user.id    
    if message.text and message.text.lower().startswith("/start"): return True    
    if u_id == OWNER_ID or u_id in ALLOWED_USERS or message.chat.id in ALLOWED_GROUPS:
        return True
    return False

def progress_bar(percent):
    filled = int((percent / 100) * 12)
    return "█" * filled + "░" * (12 - filled)

def get_duration(file):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", file],
            capture_output=True, text=True, check=True
        )
        data = json.loads(result.stdout)
        duration = data.get("format", {}).get("duration")
        if not duration or duration == 'N/A':
            for stream in data.get("streams", []):
                duration = stream.get("duration")
                if duration and duration != 'N/A': break
        return float(duration) if duration and duration != 'N/A' else 0.0
    except: return 0.0

# ================= HANDLERS =================

@app.on_message(filters.command("start"))
async def start(client, message: Message):
    photo_url = "https://graph.org/file/f8fe7d78413cd236dea26-9fbf269f0f054594b0.jpg"
    await message.reply_photo(photo=photo_url, caption="<b>🔥 Furina Encoder is Online!</b>\n\nReply to a video with /hsub to start.")

@app.on_message(filters.command("hsub"))
async def hsub_handler(client, message: Message):
    if not is_authorized(message): return
    replied = message.reply_to_message
    if not replied or not (replied.video or replied.document):
        return await message.reply("❌ Reply to a video file.")
    media = replied.video or replied.document
    users[message.from_user.id] = {"video": {"file_id": media.file_id, "file_name": getattr(media, 'file_name', "video.mp4")}}
    await message.reply("📄 Now send the Subtitle file (.srt / .ass).")

@app.on_message(filters.command("cancel"))
async def cancel_handler(client, message: Message):
    user_id = message.from_user.id
    if user_id == current_user:
        if current_task: current_task.cancel()
        if user_id in active_process: active_process[user_id].kill()
        await message.reply("❌ Current encoding cancelled.")
    elif user_id in in_queue:
        global task_queue
        task_queue = deque([t for t in task_queue if t['user_id'] != user_id])
        in_queue.remove(user_id)
        await message.reply("❌ Removed from queue.")
    else:
        await message.reply("❌ Nothing to cancel.")

@app.on_message(filters.command("delete"))
async def delete_handler(client, message: Message):
    if not is_authorized(message): return
    global users, active_process, task_queue, current_user, current_task, in_queue
    if current_task: current_task.cancel()
    for uid, process in active_process.items():
        try: process.kill()
        except: pass
    users.clear()
    active_process.clear()
    task_queue.clear()
    in_queue.clear()
    current_user = None
    current_task = None
    await message.reply("🗑️ <b>All old data, queues, and active tasks have been successfully deleted!</b>")

@app.on_message(filters.video | filters.document)
async def file_handler(client, message: Message):
    if not is_authorized(message): return
    user_id = message.from_user.id    
    if message.document and message.document.file_name.lower().endswith((".srt", ".ass", ".vtt")):
        if user_id not in users or "video" not in users[user_id]:
            return await message.reply("❌ Send /hsub on a video first.")
        users[user_id]["subtitle"] = {"file_id": message.document.file_id, "file_name": message.document.file_name}
        task_queue.append({'user_id': user_id, 'message': message, 'video_info': users[user_id]["video"], 'subtitle_info': users[user_id]["subtitle"]})
        in_queue.add(user_id)
        await message.reply(f"✅ Added to Queue. Position: {len(task_queue)}")
        del users[user_id]
    else:
        media = message.video or message.document
        users[user_id] = {"video": {"file_id": media.file_id, "file_name": getattr(media, 'file_name', "video.mp4")}}
        await message.reply("📄 Now send the Subtitle file.")

# ================= CORE LOGIC (FASTEST SETTINGS) =================

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
    # Railway/Linux compatibility ke liye escaping
    sub_path_es = sub_path.replace("'", "'\\''").replace(":", "\\:")
    
    cmd = [
        "ffmpeg", "-i", video_path, 
        "-vf", f"subtitles='{sub_path_es}'", 
        "-c:v", "libx264", 
        "-preset", "ultrafast",  # Fastest encoding speed
        "-crf", "22",            # Good balance of size and quality
        "-threads", "0",         # Auto-optimize for Railway CPU
        "-c:a", "copy",          # Direct audio copy (Zero processing time)
        "-map", "0:v",           # Video stream copy
        "-map", "0:a",           # Audio stream copy
        "-progress", "pipe:1", 
        "-nostats", "-y", 
        output_path
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
                val = line.split("=")[1]
                if val.isdigit() and duration > 0:
                    current_time = int(val) / 1000000
                    percent = min(int((current_time / duration) * 100), 100)
                    if time.time() - last_update >= 10:
                        bar = progress_bar(percent)
                        await msg.edit(f"<b>🔥 Encoding:</b> {percent}%\n{bar}")
                        last_update = time.time()
            except: continue
    
    await process.wait()
    return os.path.exists(output_path) and os.path.getsize(output_path) > 0

async def process_encoding(client, message, user_id, video_info, subtitle_info):
    status = await message.reply("⚙️ Downloading files...")
    v_path = s_path = output = thumb = None
    try:
        v_path = await client.download_media(video_info["file_id"], file_name=video_info["file_name"])
        s_path = await client.download_media(subtitle_info["file_id"], file_name=subtitle_info["file_name"])
        output = f"Encoded_{video_info['file_name']}"
        duration = get_duration(v_path)
        await status.edit("🔥 <b>Encoding started...</b>")
        if await encode_video(user_id, v_path, s_path, output, duration, status):
            thumb = await generate_thumbnail(output, user_id)
            await status.edit("📤 <b>Uploading to Channel...</b>")
            await client.send_video(
                chat_id=DEST_CHANNEL,
                video=output,
                thumb=thumb,
                caption=f"<b>✅ Hardsub Complete</b>\n<code>{output}</code>",
                supports_streaming=True
            )
            await status.edit("✅ <b>Success! Sent to channel.</b>")
        else:
            await status.edit("❌ <b>Error: Process failed.</b> Check subtitle format.")
    except Exception as e:
        await status.edit(f"❌ <b>Error:</b> {str(e)}")
    finally:
        for f in [v_path, s_path, output, thumb]:
            if f and os.path.exists(f): os.remove(f)

async def queue_worker():
    global current_user, current_task
    while True:
        if not task_queue or current_user:
            await asyncio.sleep(5); continue
        async with queue_lock:
            task = task_queue.popleft()
            current_user = task['user_id']
            in_queue.remove(current_user)
        try:
            current_task = asyncio.create_task(process_encoding(app, task['message'], current_user, task['video_info'], task['subtitle_info']))
            await current_task
        except asyncio.CancelledError: pass
        finally: current_user = None

async def main():
    await app.start()
    print("Bot is Online!")
    asyncio.create_task(queue_worker())
    await idle()

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())

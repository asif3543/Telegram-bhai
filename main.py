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

#
# ====================================================================
# सबसे ज़रूरी बदलाव: DEST_CHANNEL को सीधे कोड में डाल दिया गया है
# ====================================================================
#
DEST_CHANNEL = furina_test_channel_9988 

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
    user_id = message.from_user.id
    if message.text and message.text.lower().startswith("/start"): return True    
    if user_id == OWNER_ID or user_id in ALLOWED_USERS or message.chat.id in ALLOWED_GROUPS:
        return True
    return False

def progress_bar(percent):
    filled = int((percent / 100) * 12)
    return "█" * filled + "░" * (12 - filled)

def get_duration(file_path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", file_path],
            capture_output=True, text=True, check=True
        )
        data = json.loads(result.stdout)
        duration = data.get("format", {}).get("duration")
        return float(duration) if duration and duration != 'N/A' else 0.0
    except Exception as e:
        print(f"Error getting duration: {e}")
        return 0.0

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
        return await message.reply("❌ Reply to a video file with this command.")
    media = replied.video or replied.document
    users[message.from_user.id] = {"video": {"file_id": media.file_id, "file_name": getattr(media, 'file_name', "video.mp4")}}
    await message.reply("📄 Video received. Now send the Subtitle file (.srt / .ass).")

@app.on_message(filters.command("cancel"))
async def cancel_handler(client, message: Message):
    # This function is simplified for better queue management
    user_id = message.from_user.id
    if user_id == current_user:
        if current_task: current_task.cancel()
        if user_id in active_process: 
            active_process[user_id].kill()
            del active_process[user_id]
        await message.reply("❌ Current encoding task has been cancelled.")
    else:
        global task_queue
        initial_len = len(task_queue)
        task_queue = deque([t for t in task_queue if t['user_id'] != user_id])
        if len(task_queue) < initial_len:
            if user_id in in_queue: in_queue.remove(user_id)
            await message.reply("❌ Your task has been removed from the queue.")
        else:
            await message.reply("❌ You have no active or queued tasks to cancel.")

@app.on_message((filters.video | filters.document) & ~filters.command("hsub"))
async def file_handler(client, message: Message):
    if not is_authorized(message): return
    user_id = message.from_user.id
    media = message.video or message.document
    
    # Logic to handle subtitle file
    if media and hasattr(media, "file_name") and media.file_name and media.file_name.lower().endswith((".srt", ".ass", ".ssa", ".vtt")):
        if user_id not in users or "video" not in users[user_id]:
            return await message.reply("❌ Please send a video first or use `/hsub` on a video.")
        
        users[user_id]["subtitle"] = {"file_id": media.file_id, "file_name": media.file_name}
        task_info = {'user_id': user_id, 'message': message, 'video_info': users[user_id]["video"], 'subtitle_info': users[user_id]["subtitle"]}
        task_queue.append(task_info)
        in_queue.add(user_id)
        await message.reply(f"✅ Task added to Queue. Position: {len(task_queue)}")
        del users[user_id]
    
    # Logic to handle video file
    elif media and hasattr(media, "mime_type") and media.mime_type and "video" in media.mime_type:
        users[user_id] = {"video": {"file_id": media.file_id, "file_name": getattr(media, 'file_name', "video.mp4")}}
        await message.reply("📄 Video received. Now send the Subtitle file.")

# ================= CORE LOGIC =================

async def encode_video(user_id, video_path, sub_path, output_path, duration, msg):
    # --- FFmpeg Command Fix ---
    # The complex escaping is not needed with asyncio.subprocess and can cause errors.
    # This is a much safer way.
    cmd = [
        "ffmpeg", "-i", video_path, 
        "-vf", f"subtitles={sub_path}", 
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", 
        "-c:a", "copy", "-progress", "pipe:1", "-nostats", "-y", output_path
    ]
    
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
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
                    if time.time() - last_update >= 7: # Update every 7 seconds
                        bar = progress_bar(percent)
                        await msg.edit(f"<b>🔥 Encoding...</b>\n{bar} {percent}%")
                        last_update = time.time()
            except: continue
    
    await process.wait()
    stderr = (await process.stderr.read()).decode()
    if process.returncode != 0:
        print(f"FFMPEG ERROR: {stderr}") # For Railway logs
        return False
    
    return os.path.exists(output_path) and os.path.getsize(output_path) > 1000 # Check if file is not empty

async def process_encoding(client, message, user_id, video_info, subtitle_info):
    status = await message.reply("📥 Downloading files...")
    v_path = s_path = output = thumb = None
    
    try:
        v_path = await client.download_media(video_info["file_id"], file_name=f"{user_id}_video")
        s_path = await client.download_media(subtitle_info["file_id"], file_name=f"{user_id}_subtitle")
        if not v_path or not s_path:
            return await status.edit("❌ Download failed.")

        output = f"Encoded_{video_info.get('file_name', 'output.mp4')}"
        duration = get_duration(v_path)
        
        await status.edit("🔥 Encoding started...")
        if await encode_video(user_id, v_path, s_path, output, duration, status):
            await status.edit("📤 Uploading to Channel...")
            await client.send_video(
                chat_id=DEST_CHANNEL,
                video=output,
                caption=f"✅ Hardsubbed: `{os.path.basename(output)}`",
                supports_streaming=True
            )
            await status.edit("✅ Success! Video sent to the channel.")
        else:
            await status.edit("❌ Encoding failed. This might be due to an unsupported subtitle format or font. Please check the logs.")
            
    except Exception as e:
        await status.edit(f"❌ An Error Occurred: `{str(e)}`")
    finally:
        if user_id in active_process: del active_process[user_id]
        for f in [v_path, s_path, output]:
            if f and os.path.exists(f): os.remove(f)

async def queue_worker():
    global current_user, current_task
    while True:
        if not task_queue or current_user:
            await asyncio.sleep(2); continue
        
        async with queue_lock:
            task = task_queue.popleft()
            current_user = task['user_id']
        
        current_task = asyncio.create_task(process_encoding(app, task['message'], current_user, task['video_info'], task['subtitle_info']))
        try:
            await current_task
        except asyncio.CancelledError:
            await app.send_message(current_user, "Your task was cancelled by your request.")

        current_user = None
        current_task = None

async def main():
    await app.start()
    print("Bot is Online!")
    asyncio.create_task(queue_worker())
    await idle()

if __name__ == "__main__":
    asyncio.run(main()) # Modern way to run asyncio

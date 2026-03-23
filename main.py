import os
import re
import time
import json
import asyncio
import subprocess
import traceback
from collections import deque
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ChatType

# ================= CONFIGURATION (Railway Variables) =================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DEST_CHANNEL = int(os.getenv("DEST_CHANNEL", 0))

# 🔐 Authority System Configuration
OWNER_ID = 5344078567                    
ALLOWED_USERS = [5351848105]             
ALLOWED_GROUPS = [-1003810374456]        

app = Client("EncoderBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Global Variables for Queue Management
users = {}
active_process = {}  
task_queue = deque()
current_user = None
current_task = None
queue_lock = asyncio.Lock()
in_queue = set()

# ================= UTILS & PROGRESS BARS =================

def progress_bar(percent):
    total_blocks = 12
    filled = int((percent / 100) * total_blocks)
    return "█" * filled + "░" * (total_blocks - filled)

# ================= FIXED & ROBUST get_duration FUNCTION =================
def get_duration(file):
    """Safely gets the duration of a video file, handling 'N/A' and errors."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", 
             "-show_format", "-show_streams", file],
            capture_output=True, text=True, check=True, timeout=30
        )
        data = json.loads(result.stdout)
        
        # Try from format
        duration_str = data.get("format", {}).get("duration")
        
        # If not, try from video stream
        if not duration_str or duration_str == 'N/A':
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    duration_str = stream.get("duration")
                    break
        
        if duration_str and duration_str != 'N/A':
            return float(duration_str)
        else:
            print(f"Warning: Duration not found or was 'N/A' for file {file}. Returning 0.0.")
            return 0.0
    except subprocess.TimeoutExpired:
        print(f"ffprobe timeout for {file}")
        return 0.0
    except Exception as e:
        print(f"Error getting duration for {file}: {e}. Returning 0.0.")
        return 0.0

# ================= DOWNLOADER =================

async def download_with_progress(client, file_id, save_as, status_msg, label):
    start_time = time.time()
    last_percent = -1
    last_bytes = 0
    last_time = start_time

    def progress_callback(current, total):    
        nonlocal last_percent, last_bytes, last_time    
        if total <= 0: return    
        percent = min(100, int((current / total) * 100))    
        if (percent - last_percent >= 2) or (percent == 100 and last_percent < 100):    
            current_time = time.time()    
            bytes_diff = current - last_bytes    
            time_diff = current_time - last_time    
            speed_str = f"{(bytes_diff / time_diff) / (1024 * 1024):.2f} MB/s" if time_diff > 0.1 else "0.00 MB/s"    
            bar = progress_bar(percent)    
            try: asyncio.create_task(status_msg.edit(f"📥 Downloading {label}...\n\n{bar} {percent}%\n\n⚡ Speed: {speed_str}"))
            except: pass  
            last_percent = percent
            last_bytes = current
            last_time = current_time

    try:    
        file_path = await client.download_media(file_id, file_name=save_as, progress=progress_callback)    
        return file_path    
    except Exception: return None

# ================= COMMAND HANDLERS =================

@app.on_message(filters.command("start"))
async def start(client, message: Message):
    photo_url = "https://graph.org/file/f8fe7d78413cd236dea26-9fbf269f0f054594b0.jpg"
    caption = "<b>ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ꜰᴜʀɪɴᴀ ᴇɴᴄᴏᴅɪɴɢ ʙᴏᴛ</b>\n\n<b>ᴜꜱᴇ ᴍᴇ ᴛᴏ ᴀᴅᴅ ꜱᴜʙᴛɪᴛʟᴇ ɪɴᴛᴏ ʏᴏᴜʀ ᴠɪᴅᴇᴏ.</b>"
    button = InlineKeyboardMarkup([[InlineKeyboardButton("Help", callback_data=f"starthelp_{message.from_user.id}"), InlineKeyboardButton("About", callback_data=f"startabout_{message.from_user.id}")]])
    await client.send_photo(chat_id=message.chat.id, photo=photo_url, caption=caption, reply_markup=button)

@app.on_message(filters.command("hsub"))
async def hsub_handler(client, message: Message):
    if not is_authorized(message): return
    replied = message.reply_to_message
    if not replied or not (replied.video or replied.document):
        await message.reply("❌ Please reply to a video with /hsub")
        return
    media = replied.video or replied.document
    file_id = media.file_id
    file_name = getattr(media, 'file_name', "video.mp4")
    users[message.from_user.id] = {"video": {"file_id": file_id, "file_name": file_name}}
    await message.reply("📄 Now send subtitle (.srt / .ass / .ssa / .vtt)")

@app.on_message(filters.command("cancel"))
async def cancel_handler(client, message: Message):
    user_id = message.from_user.id    
    if user_id == current_user:
        if current_task: current_task.cancel()
        if user_id in active_process: active_process[user_id].kill()
        await message.reply("❌ Your encoding has been cancelled.")
    elif user_id in in_queue:
        for i, task in enumerate(task_queue):
            if task['user_id'] == user_id:
                del task_queue[i]
                in_queue.remove(user_id)
                break
        await message.reply("❌ Task removed from queue.")
    else: await message.reply("❌ Nothing running.")

# ================= FILE HANDLER & QUEUE =================

@app.on_message(filters.video | filters.document)
async def file_handler(client, message: Message):
    if not is_authorized(message): return
    user_id = message.from_user.id    

    if user_id not in users: return
    
    if message.document and message.document.file_name.endswith((".srt", ".ass", ".ssa", ".vtt")):
        if "video" not in users[user_id]:
            await message.reply("❌ First send or reply to a video.")
            return
            
        users[user_id]["subtitle"] = {"file_id": message.document.file_id, "file_name": message.document.file_name}
        
        task_info = {'user_id': user_id, 'message': message, 'video_info': users[user_id]["video"], 'subtitle_info': users[user_id]["subtitle"]}
        task_queue.append(task_info)
        in_queue.add(user_id)
        await message.reply(f"✅ Task added to queue. Position: {len(task_queue)}")
        del users[user_id]
    else:
        media = message.video or message.document
        users[user_id] = {"video": {"file_id": media.file_id, "file_name": getattr(media, 'file_name', "video.mp4")}}
        await message.reply("📄 Now send subtitle (.srt / .ass / .ssa / .vtt)")

def is_authorized(message: Message) -> bool:
    if not message.from_user: return False
    user_id = message.from_user.id    
    chat_id = message.chat.id    
    if message.text and message.text.lower().startswith("/start"): return True    
    if user_id == OWNER_ID: return True    
    if message.chat.type == ChatType.PRIVATE: return user_id in ALLOWED_USERS    
    return chat_id in ALLOWED_GROUPS

# ================= ENCODING & THUMBNAIL =================

async def generate_thumbnail(video_path, user_id):
    try:
        duration = get_duration(video_path)
        timestamp = duration / 2 if duration > 1 else 1
        thumb_path = f"thumb_{user_id}.jpg"
        cmd = ["ffmpeg", "-i", video_path, "-ss", str(timestamp), "-vframes", "1", "-q:v", "2", "-y", thumb_path]
        process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await process.wait()
        return thumb_path if os.path.exists(thumb_path) else None
    except Exception as e: 
        print(f"Error generating thumbnail: {e}")
        return None

async def encode_video(user_id, video_path, sub_path, output_path, duration, msg):
    cmd = ["ffmpeg", "-i", video_path, "-vf", f"subtitles='{sub_path}'", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "copy", "-progress", "pipe:1", "-nostats", output_path]
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    active_process[user_id] = process
    last_update_time = time.time()

    if duration <= 0:
        await msg.edit("🔥 Encoding...\n(Cannot determine video length, progress bar unavailable)")
        await process.wait()
        return os.path.exists(output_path)

    while True:
        line = await process.stdout.readline()
        if not line: break
        line = line.decode(errors="ignore").strip()
        if line.startswith("out_time_ms="):
            try:
                current_time = int(line.split("=")[1]) / 1000000
                percent = min(int((current_time / duration) * 100), 100) if duration > 0 else 0
                if time.time() - last_update_time >= 7:
                    bar = progress_bar(percent)
                    await msg.edit(f"<b>ᴇɴᴄᴏᴅɪɴɢ...</b>\n\n[{bar}] {percent}%")
                    last_update_time = time.time()
            except:
                continue
    await process.wait()
    return os.path.exists(output_path)

# ================= UPLOAD TO CHANNEL =================

async def upload_file(client, message, file_path, thumb_path=None):
    upload_msg = await message.reply("<b>ᴜᴘʟᴏᴀᴅɪɴɢ ᴛᴏ ᴄʜᴀɴɴᴇʟ...</b>")
    caption = f"<b>{os.path.basename(file_path)}</b>\n\n<b>ᴇɴᴄᴏᴅᴇᴅ ʙʏ: @Furinaencodingbot</b>"
    
    try:
        await client.send_video(
            chat_id=DEST_CHANNEL,
            video=file_path,
            thumb=thumb_path,
            caption=caption,
            supports_streaming=True
        )
        await upload_msg.edit("✅ <b>Video uploaded to channel!</b>")
    except Exception as e:
        await upload_msg.edit(f"❌ <b>Upload Error:</b> {str(e)}")
    finally:
        if os.path.exists(file_path): os.remove(file_path)

# ================= WORKER =================

async def process_encoding(client, message, user_id, video_info, subtitle_info):
    status = await message.reply("📥 Starting Task...")
    v_path = None
    s_path = None
    output = None
    thumb = None
    try:
        v_path = await download_with_progress(client, video_info["file_id"], video_info["file_name"], status, "Video")
        s_path = await download_with_progress(client, subtitle_info["file_id"], subtitle_info["file_name"], status, "Subtitle")
        
        if not v_path or not s_path:
            await status.edit("❌ Download failed. Please try again.")
            return

        output = f"Hardsub_{video_info['file_name']}"
        duration = get_duration(v_path)
        
        await status.edit("🔥 Encoding...")
        if await encode_video(user_id, v_path, s_path, output, duration, status):
            thumb = await generate_thumbnail(output, user_id)
            await upload_file(client, message, output, thumb)
        else:
            await status.edit("❌ Encoding failed.")

    except Exception as e:
        await status.edit(f"❌ An unexpected error occurred: {str(e)}")
        print("=== FULL ERROR ===")
        traceback.print_exc()
    finally:
        # Cleanup all created files
        if v_path and os.path.exists(v_path): os.remove(v_path)
        if s_path and os.path.exists(s_path): os.remove(s_path)
        if output and os.path.exists(output): os.remove(output)
        if thumb and os.path.exists(thumb): os.remove(thumb)

async def queue_worker():
    global current_user, current_task
    while True:
        if not task_queue or current_user:
            await asyncio.sleep(2)
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
    print("Bot Started!")
    asyncio.create_task(queue_worker())
    await idle()

if __name__ == "__main__":
    asyncio.run(main())

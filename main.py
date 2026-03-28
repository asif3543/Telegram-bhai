import os
import time
import json
import asyncio
import threading
import tempfile
import re
from collections import deque
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import MessageNotModified, MessageIdInvalid
from http.server import HTTPServer, BaseHTTPRequestHandler

# ================= CONFIGURATION =================

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DEST_CHANNEL = "@Sub_and_hardsub"   # yaha channel ka username dena. Id nahi dena.
PORT = 10000      # ye change mat karna 

OWNER_ID = 5351848105       
ALLOWED_USERS = [5344078567]             
ALLOWED_GROUPS = [-1003899919015] 

app = Client("EncoderBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Global Variables
users_data = {}
task_queue = deque()
in_queue = set()
processing_lock = asyncio.Lock()
main_loop = None
edit = "Maintanence by: @Sub_and_hardsub"     # ye change mat karna warna bot start nahi hoga. yaha value hoga "Maintanence by: @Sub_and_hardsub"


# Tracks currently encoding process per user (for cancellation)
current_encoding = {}  # user_id -> asyncio.subprocess.Process

# ================= UTILS =================

def is_authorized(message: Message) -> bool:
    """Check if user is allowed (owner, allowed list, or group)."""
    if not message.from_user: return False
    u_id = message.from_user.id    
    if message.text and message.text.lower().startswith("/start"): return True    
    if u_id == OWNER_ID or u_id in ALLOWED_USERS or message.chat.id in ALLOWED_GROUPS:
        return True
    return False

def is_owner(message: Message) -> bool:
    """Check if user is the bot owner."""
    return message.from_user and message.from_user.id == OWNER_ID

async def get_duration(file):
    try:
        cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", file]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        data = json.loads(stdout.decode())
        return float(data.get("format", {}).get("duration", 0))
    except:
        return 0

def format_progress_bar(percent, width=10):
    filled = int(percent * width / 100)
    bar = "█" * filled + "░" * (width - filled)
    return bar

async def safe_edit(message: Message, text: str):
    try:
        await message.edit(text)
    except (MessageNotModified, MessageIdInvalid):
        pass
    except Exception:
        pass

async def download_with_verification(client, file_id, status_msg, phase="Downloading"):
    """Download with verification and retries (no progress bar)."""
    temp_dir = tempfile.gettempdir()
    base_name = f"temp_{int(time.time())}_{file_id}"
    
    for attempt in range(5):
        temp_file = os.path.join(temp_dir, f"{base_name}_{attempt}")
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
                
            path = await client.download_media(file_id, file_name=temp_file)
            if path and os.path.exists(path) and os.path.getsize(path) > 0:
                # Verify with ffprobe
                cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
                proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    return path
                else:
                    raise Exception("File corrupt")
        except Exception as e:
            if attempt < 4:
                await asyncio.sleep(5 * (attempt + 1))
                continue
            raise Exception(f"Download failed after {attempt+1} attempts: {str(e)}")
    raise Exception("Download failed after 5 attempts")

async def encode_with_progress(video_path, subtitle_path, output_path, total_duration, status_msg, user_id):
    """Run FFmpeg and update progress from -progress output."""
    # Escape subtitle path for FFmpeg filter
    escaped_sub = subtitle_path.replace("\\", "\\\\").replace("'", "'\\\\''")
    
    # [1] FFMPEG SETTINGS UPDATED FOR SPEED AND STABLE SIZE
    cmd = [
        "ffmpeg", "-i", video_path,
        "-vf", f"subtitles=filename='{escaped_sub}'",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "24",
        "-threads", "0",
        "-max_muxing_queue_size", "1024",
        "-c:a", "copy",
        "-progress", "pipe:1",
        "-y", output_path
    ]
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    # Store process for potential cancellation
    current_encoding[user_id] = process
    
    last_update = 0
    progress_data = {}
    error_lines = []

    async def read_stdout():
        nonlocal last_update
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            line_str = line.decode(errors="ignore").strip()
            if "=" in line_str:
                key, val = line_str.split("=", 1)
                progress_data[key] = val
            if key == "out_time_ms":
                try:
                    ms = int(progress_data.get("out_time_ms", 0))
                    current_seconds = ms / 1_000_000.0
                    percent = (current_seconds / total_duration) * 100 if total_duration > 0 else 0
                    now = time.time()
                    
                    # [4] PROGRESS UPDATE CHANGED FROM 7 TO 5 SECONDS
                    if now - last_update > 5 or percent >= 100:
                        bar = format_progress_bar(percent)
                        await safe_edit(status_msg, f"🔥 Encoding...\n`{bar}` {percent:.1f}%")
                        last_update = now
                except Exception:
                    pass

    async def read_stderr():
        nonlocal error_lines
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            line_str = line.decode(errors="ignore")
            error_lines.append(line_str)

    await asyncio.gather(read_stdout(), read_stderr())

    returncode = await process.wait()
    # Remove from tracking
    current_encoding.pop(user_id, None)
    
    if returncode != 0:
        error_text = "".join(error_lines[-20:])
        raise Exception(f"FFmpeg failed with code {returncode}\n{error_text}")
    if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
        raise Exception("Output file missing or too small")
    return True

# ================= HANDLERS =================

@app.on_message(filters.command("start"))
async def start(client, message: Message):
    await message.reply(f"<b>🔥 Hardsub bot is Online again!</b>\n\nUse /hsub to add subtitle into video\nUse /cancel to stop your current task\nUse /delete to clear all tasks (owner only)\n\n{edit}")

@app.on_message(filters.command("delete"))
async def delete_all(client, message: Message):
    """Only owner can clear all queues and data."""
    if not is_owner(message):
        await message.reply("❌ Only the bot owner can use this command.")
        return
    global task_queue, in_queue, users_data
    task_queue.clear()
    in_queue.clear()
    users_data.clear()
    await message.reply("🗑️ All data cleared.")

@app.on_message(filters.command("cancel"))
async def cancel_task(client, message: Message):
    """Cancel the user's own queued or running task."""
    if not is_authorized(message):
        return
    user_id = message.from_user.id
    
    # Check if user has a task in queue
    removed = False
    for i, task in enumerate(task_queue):
        if task["user_id"] == user_id:
            del task_queue[i]
            removed = True
            break
    
    # If task is currently encoding, terminate the process
    if user_id in current_encoding:
        proc = current_encoding[user_id]
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except:
            proc.kill()
        current_encoding.pop(user_id, None)
        await message.reply("🛑 Your encoding task has been cancelled.")
        return
    
    if removed:
        in_queue.discard(user_id)
        await message.reply("✅ Your task has been removed from the queue.")
    else:
        await message.reply("❌ No active task found for you.")

@app.on_message(filters.command("hsub"))
async def hsub_cmd(client, message: Message):
    if not is_authorized(message): return
    replied = message.reply_to_message
    if not replied or not (replied.video or replied.document):
        return await message.reply("❌ Reply to a video file with /hsub")
    
    media = replied.video or replied.document
    users_data[message.from_user.id] = {
        "video": {"file_id": media.file_id, "file_name": media.file_name or "video.mp4"},
        "chat_id": message.chat.id,
        "state": "WAIT_SUB"
    }
    await message.reply("📄 Now send the Subtitle file (.srt / .ass)")

@app.on_message(filters.document | filters.video | filters.text)
async def handle_all_inputs(client, message: Message):
    if not is_authorized(message): return
    user_id = message.from_user.id
    
    if user_id not in users_data: return

    state = users_data[user_id].get("state")

    if state == "WAIT_SUB" and message.document:
        if message.document.file_name.lower().endswith((".srt", ".ass")):
            users_data[user_id]["subtitle"] = {"file_id": message.document.file_id, "file_name": message.document.file_name}
            users_data[user_id]["state"] = "WAIT_RENAME_CHOICE"
            
            btn = InlineKeyboardMarkup([[
                InlineKeyboardButton("Rename", callback_data="rn_yes"),
                InlineKeyboardButton("Skip", callback_data="rn_skip")
            ]])
            await message.reply("Do you want to rename the output file?", reply_markup=btn)
        return

    if state == "WAIT_RENAME_TEXT" and message.text:
        new_name = message.text.strip()
        base = os.path.splitext(new_name)[0]
        new_name = base + ".mp4"
        users_data[user_id]["video"]["file_name"] = new_name
        await add_to_queue(user_id, message)
        return

@app.on_callback_query(filters.regex("^rn_"))
async def callback_rename(client, query: CallbackQuery):
    user_id = query.from_user.id
    if user_id not in users_data:
        return await query.answer("Not Yours!", show_alert=True)

    if query.data == "rn_yes":
        users_data[user_id]["state"] = "WAIT_RENAME_TEXT"
        await query.message.edit("📝 Send new name for the video (without extension)\n\nEx: [S01 - Ep 02] Oshi no Ko - HD")
    else:
        original = users_data[user_id]["video"]["file_name"]
        base = os.path.splitext(original)[0]
        users_data[user_id]["video"]["file_name"] = base + ".mp4"
        await query.message.edit("🚀 Processing with original name...")
        await add_to_queue(user_id, query.message)

async def add_to_queue(user_id, message):
    data = users_data.pop(user_id)
    task_queue.append({
        "user_id": user_id,
        "video": data["video"],
        "subtitle": data["subtitle"],
        "chat_id": data["chat_id"]
    })
    in_queue.add(user_id)
    await message.reply(f"✅ Added to Queue. Position: {len(task_queue)}")

# ================= CORE ENCODER =================

async def worker():
    while True:
        if not task_queue:
            await asyncio.sleep(5)
            continue
        
        task = task_queue.popleft()
        uid = task["user_id"]
        v_info = task["video"]
        s_info = task["subtitle"]
        original_chat = task["chat_id"]
        
        status = await app.send_message(original_chat, "⏳ Starting Process...")
        channel_log = None
        v_path = s_path = out_path = None
        
        try:
            if DEST_CHANNEL:
                channel_log = await app.send_message(DEST_CHANNEL, f"<b>🔄 Starting:</b> {v_info['file_name']}")

            await safe_edit(status, "📥 Downloading video...")
            v_path = await download_with_verification(app, v_info["file_id"], status, "Downloading video")

            # [3] SERVER SAFETY CHECK (500 MB)
            if os.path.getsize(v_path) > 500 * 1024 * 1024:
                await safe_edit(status, "❌ Video too large for server.")
                continue  # Using 'continue' instead of 'return' to keep the queue running.

            await safe_edit(status, "📥 Downloading subtitle...")
            s_path = await download_with_verification(app, s_info["file_id"], status, "Downloading subtitle")

            dur = await get_duration(v_path)
            out_path = v_info["file_name"]
            await safe_edit(status, "🔥 Encoding...")
            success = await encode_with_progress(v_path, s_path, out_path, dur, status, uid)

            if success:
                await safe_edit(status, "📤 Uploading...")
                upload_target = DEST_CHANNEL if DEST_CHANNEL else original_chat
                
                # [2] SEND AS DOCUMENT FORMAT INSTEAD OF VIDEO
                await app.send_document(
                    chat_id=upload_target,
                    document=out_path,
                    caption=f"{out_path}"
                )
                
                await safe_edit(status, f"✅ Successfully Completed!\n\nFile sent to {DEST_CHANNEL}")
                if channel_log:
                    await channel_log.delete()
            else:
                await safe_edit(status, "❌ Encoding Failed.")
                
        except Exception as e:
            await app.send_message(original_chat, f"❌ Error: {str(e)}")
        finally:
            in_queue.discard(uid)
            for f in [v_path, s_path, out_path]:
                if f and os.path.exists(f):
                    try:
                        os.remove(f)
                    except:
                        pass

# ================= RENDER KEEP ALIVE =================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is Running")

def run_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()

# ================= MAIN =================

async def main():
    if edit != "Maintanence by: @Sub_and_hardsub":
        print("credit hataya isiliye nahi chala. Sahi karo wo pehele. Waha value hoga 'Maintanence by: @Silent_Shinjou'")
        return
    global main_loop
    main_loop = asyncio.get_event_loop()
    await app.start()
    print("Bot is started!")
    asyncio.create_task(worker())
    await idle()

if __name__ == "__main__":
    threading.Thread(target=run_health_server, daemon=True).start()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())

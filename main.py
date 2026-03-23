import os
import asyncio
import subprocess
from pyrogram import Client, filters
from pyrogram.types import Message

# Ye variables aap Render/Railway ke 'Environment Variables' mein daalenge
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DEST_CHANNEL = int(os.environ.get("DEST_CHANNEL", 0)) # Aapka Private Channel ID

bot = Client("HardsubBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Temporary storage manage karne ke liye dictionary
user_status = {}

@bot.on_message(filters.command("start") & filters.private)
async def start(client, message):
    await message.reply_text("👋 Welcome! Hardsub ke liye pehle Video bhejo, fir us video ko **Reply** karke apni `.ass` file bhejna.")

@bot.on_message(filters.video & filters.private)
async def video_receiver(client, message):
    user_status[message.chat.id] = message.id
    await message.reply_text("✅ Video mil gayi! Ab is video ko **Reply** karke `.ass` subtitle file send karo.")

@bot.on_message(filters.document & filters.private)
async def sub_receiver(client, message):
    # Check if user replied to the correct video
    if not message.reply_to_message or message.reply_to_message.id != user_status.get(message.chat.id):
        return await message.reply_text("❌ Error: Pehle wali video ko hi Reply karke file bhejo!")

    if message.document.file_name.endswith(".ass"):
        status_msg = await message.reply_text("📥 Downloading files... Please wait.")
        
        # Paths setup
        v_path = await message.reply_to_message.download()
        s_path = await message.download()
        out_path = f"hardsub_{message.chat.id}.mp4"

        await status_msg.edit("⚙️ Hardsubbing Start... (FFmpeg Processing) 🔥")

        # FFmpeg Command for High Quality & .ass support
        # '-preset veryfast' isliye taaki Render/Railway par timeout na ho
        cmd = [
            'ffmpeg', '-i', v_path,
            '-vf', f"ass='{s_path}'",
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
            '-c:a', 'copy', out_path, '-y'
        ]

        try:
            process = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            
            if process.returncode == 0:
                await status_msg.edit("📤 Uploading to Private Channel...")
                await client.send_video(
                    chat_id=DEST_CHANNEL,
                    video=out_path,
                    caption=f"✅ Hardsub Done!\nOriginal: {message.reply_to_message.video.file_name}"
                )
                await status_msg.edit("🚀 Kaam khatam! Video channel par bhej di gayi hai.")
            else:
                error_log = process.stderr.decode()
                await status_msg.edit(f"❌ FFmpeg Error: {error_log[:100]}")

        except Exception as e:
            await status_msg.edit(f"⚠️ Error: {str(e)}")
        
        finally:
            # STORAGE PROTECTION: Har process ke baad files delete karna
            for f in [v_path, s_path, out_path]:
                if f and os.path.exists(f):
                    os.remove(f)
    else:
        await message.reply_text("❌ Sirf `.ass` format ki file bhejo.")

bot.run()
      

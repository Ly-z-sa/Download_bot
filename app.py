import os
import re
import asyncio
import logging
import threading
from typing import Optional, Tuple
from datetime import datetime
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    constants
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
import yt_dlp

# --- NEW IMPORTS FOR RENDER ---
from http.server import SimpleHTTPRequestHandler, HTTPServer


# Load environment variables (for local development only)
try:
    load_dotenv()
except:
    pass

TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    print("ERROR: BOT_TOKEN environment variable not found!")
    exit(1)

DOWNLOAD_DIR = "downloads"
TEMP_DIR = "temp"
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB Telegram limit

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Create directories
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)


# --- Helpers ---
def clean_filename(filename: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', '', filename)[:100]


def format_size(bytes: int) -> str:
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes < 1024.0:
            return f"{bytes:.1f} {unit}"
        bytes /= 1024.0
    return f"{bytes:.1f} TB"


def is_valid_url(url: str) -> Tuple[bool, str]:
    youtube_patterns = [
        r'(https?://)?(www\.)?(youtube\.com|youtu\.be|m\.youtube\.com)/',
        r'(https?://)?(www\.)?(youtube\.com/shorts/)',
    ]
    tiktok_patterns = [
        r'(https?://)?(www\.)?(tiktok\.com|vm\.tiktok\.com)/',
    ]
    twitter_patterns = [
        r'(https?://)?(www\.)?(twitter\.com|x\.com)/',
    ]

    for pattern in youtube_patterns:
        if re.search(pattern, url, re.IGNORECASE):
            return True, "youtube"
    for pattern in tiktok_patterns:
        if re.search(pattern, url, re.IGNORECASE):
            return True, "tiktok"
    for pattern in twitter_patterns:
        if re.search(pattern, url, re.IGNORECASE):
            return True, "twitter"
    return False, None


async def download_media(url: str, format_type: str = "video", quality: str = "best") -> Tuple[Optional[str], Optional[str], Optional[dict]]:
    try:
        clean_template = os.path.join(TEMP_DIR, '%(title).100s.%(ext)s')
        ydl_opts = {
            'outtmpl': clean_template,
            'quiet': True,
            'no_warnings': True,
            'restrictfilenames': True,
            'ignoreerrors': True,
            'no_check_certificate': True,
            'concurrent_fragment_downloads': 4,
            'retries': 1,
            'fragment_retries': 1,
            'skip_unavailable_fragments': True,
        }

        if 'youtube.com' in url or 'youtu.be' in url:
            ydl_opts.update({'extractor_args': {'youtube': {'skip': ['dash', 'hls']}}})
        elif 'tiktok.com' in url or 'twitter.com' in url or 'x.com' in url:
            ydl_opts.update({
                'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
            })

        if format_type == "audio":
            ydl_opts['format'] = 'bestaudio'
            ydl_opts['postprocessors'] = [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '128',
            }]
        else:
            ydl_opts['format'] = 'best[height<=720]/best' if quality == "best" else f'best[height<={quality}]/best'

        def sync_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return info

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, sync_download)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            expected_path = ydl.prepare_filename(info)

        filepath = expected_path if format_type != "audio" else os.path.splitext(expected_path)[0] + '.mp3'
        if not os.path.exists(filepath):
            files = [os.path.join(TEMP_DIR, f) for f in os.listdir(TEMP_DIR)]
            filepath = max(files, key=os.path.getctime) if files else None
            if not filepath:
                return None, None, None

        filename = os.path.basename(filepath)
        metadata = {
            'title': info.get('title', 'Unknown'),
            'duration': info.get('duration', 0),
            'uploader': info.get('uploader', 'Unknown'),
            'view_count': info.get('view_count', 0),
            'like_count': info.get('like_count', 0),
            'upload_date': info.get('upload_date', ''),
        }
        return filepath, filename, metadata

    except Exception as e:
        logger.error(f"Download error: {str(e)}")
        return None, None, None


# --- URL storage for callbacks ---
url_storage = {}


def create_quality_keyboard(url: str, platform: str) -> InlineKeyboardMarkup:
    url_hash = str(hash(url))[-8:]
    url_storage[url_hash] = url

    if platform == "youtube":
        keyboard = [
            [InlineKeyboardButton("📹 Best Video", callback_data=f"dl|video|best|{url_hash}"),
             InlineKeyboardButton("🎵 Audio Only", callback_data=f"dl|audio|best|{url_hash}")],
            [InlineKeyboardButton("720p", callback_data=f"dl|video|720|{url_hash}"),
             InlineKeyboardButton("480p", callback_data=f"dl|video|480|{url_hash}"),
             InlineKeyboardButton("360p", callback_data=f"dl|video|360|{url_hash}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("📹 Download Video", callback_data=f"dl|video|best|{url_hash}"),
             InlineKeyboardButton("🎵 Audio Only", callback_data=f"dl|audio|best|{url_hash}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]
    return InlineKeyboardMarkup(keyboard)


# --- Commands ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎬 Welcome! Send me a YouTube, TikTok, or Twitter/X link and I'll fetch it."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Send me a valid YouTube, TikTok, or Twitter/X link. I’ll give you quality options."
    )


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("ℹ️ Simple video downloader bot. Made for Render demo 🚀")


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    is_valid, platform = is_valid_url(url)
    if not is_valid:
        await update.message.reply_text("❌ Unsupported link! Only YouTube/TikTok/Twitter.")
        return
    await update.message.reply_text(
        f"📹 {platform.title()} video detected! Choose option:",
        reply_markup=create_quality_keyboard(url, platform)
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "cancel":
        await query.message.edit_text("❌ Cancelled.")
        return

    _, format_type, quality, url_hash = data.split("|")
    url = url_storage.get(url_hash)
    if not url:
        await query.message.edit_text("❌ Session expired. Send the link again.")
        return

    await query.message.edit_text("⬇️ Downloading... Please wait.")
    filepath, filename, metadata = await download_media(url, format_type, quality)

    if not filepath:
        await query.message.edit_text("❌ Download failed.")
        return

    file_size = os.path.getsize(filepath)
    if file_size > MAX_FILE_SIZE:
        os.remove(filepath)
        await query.message.edit_text("❌ File too large for Telegram (50MB limit).")
        return

    caption = f"✅ {metadata['title']}\n👤 {metadata['uploader']}\n📊 {format_size(file_size)}"

    with open(filepath, "rb") as f:
        if format_type == "audio":
            await context.bot.send_audio(chat_id=query.message.chat_id, audio=f, caption=caption)
        else:
            await context.bot.send_video(chat_id=query.message.chat_id, video=f, caption=caption)

    os.remove(filepath)
    try:
        await query.message.delete()
    except:
        pass


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ Unknown command. Use /help.")


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error {context.error}")


# --- Dummy web server for Render ---
def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
    print(f"🌐 Dummy server running on port {port}")
    server.serve_forever()


# --- Main ---
def main():
    threading.Thread(target=run_web_server, daemon=True).start()

    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    application.add_error_handler(error_handler)

    print("🤖 Bot is starting (polling mode)...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

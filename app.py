import os
import re
import asyncio
import logging
import shutil
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

# --- NEW: Minimal web server for Render ---
from fastapi import FastAPI
import uvicorn
import threading

app = FastAPI()

@app.get("/")
def root():
    return {"status": "Bot is running"}

def run_webserver():
    port = int(os.environ.get("PORT", 10000))  # Render sets $PORT
    uvicorn.run(app, host="0.0.0.0", port=port)

# Make sure cookies.txt is in a writable place
SECRET_COOKIES = "/etc/secrets/cookies.txt"
LOCAL_COOKIES = "cookies.txt"

if os.path.exists(SECRET_COOKIES):
    try:
        shutil.copy(SECRET_COOKIES, LOCAL_COOKIES)
        print("✅ Copied cookies.txt from /etc/secrets to local path")
    except Exception as e:
        print("⚠️ Failed to copy cookies.txt:", e)

# --- Load environment variables ---
try:
    load_dotenv()
except:
    pass

TOKEN = os.getenv('BOT_TOKEN')
if not TOKEN:
    print("ERROR: BOT_TOKEN environment variable not found!")
    exit(1)

print(f"Bot starting with token: {TOKEN[:10]}...")
DOWNLOAD_DIR = "downloads"
TEMP_DIR = "temp"
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB Telegram limit

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# Helper: Clean filename
def clean_filename(filename: str) -> str:
    """Remove invalid characters from filename"""
    return re.sub(r'[<>:"/\\|?*]', '', filename)[:100]

# Helper: Format file size
def format_size(bytes: int) -> str:
    """Convert bytes to human readable format"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes < 1024.0:
            return f"{bytes:.1f} {unit}"
        bytes /= 1024.0
    return f"{bytes:.1f} TB"

# Helper: Validate URL
def is_valid_url(url: str) -> Tuple[bool, str]:
    """Check if URL is from supported platform"""
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

# Helper: Download with yt-dlp
async def download_media(url: str, format_type: str = "video", quality: str = "best") -> Tuple[Optional[str], Optional[str], Optional[dict]]:
    """Download media using yt-dlp (fixed for YouTube audio/video)"""
    try:
        clean_template = os.path.join(TEMP_DIR, '%(title).100s.%(ext)s')
        
        ydl_opts = {
            'outtmpl': clean_template,
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'restrictfilenames': True,
            'ignoreerrors': True,
            'no_check_certificate': True,
            'concurrent_fragment_downloads': 4,
            'retries': 1,
            'fragment_retries': 1,
            'skip_unavailable_fragments': True,
        }

        # Platform-specific options
       if 'youtube.com' in url or 'youtu.be' in url:
            cookie_path = os.getenv("YOUTUBE_COOKIE_PATH", "cookies.txt")  # Use env var or default
            if format_type == "audio":
                ydl_opts.update({
                    'format': 'bestaudio/best',  # fallback to best if not exactly available
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'mp3',
                        'preferredquality': '128',
                    }],
                    'noplaylist': True,
                    'cookiefile': cookie_path,
                })
            else:
                ydl_opts.update({
                    'format': 'bestvideo+bestaudio/best',  # try best video+audio, fallback to best
                    'merge_output_format': 'mp4',
                    'noplaylist': True,
                    'cookiefile': cookie_path,
                })

        elif 'tiktok.com' in url:
            ydl_opts.update({
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
            })
        elif 'twitter.com' in url or 'x.com' in url:
            ydl_opts.update({
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
            })

        # For non-YouTube video formats (TikTok/X) keep your original logic
        if format_type == "video" and 'youtube' not in url:
            if quality == "best":
                ydl_opts['format'] = 'best[height<=720]/best'
            else:
                ydl_opts['format'] = f'best[height<={quality}]/best'

        # Download in executor to avoid blocking
        def sync_download():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return info

        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, sync_download)

        # Get filename
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            expected_path = ydl.prepare_filename(info)

        if format_type == "audio":
            filepath = os.path.splitext(expected_path)[0] + '.mp3'
        else:
            filepath = expected_path

        if not os.path.exists(filepath):
            temp_files = [os.path.join(TEMP_DIR, f) for f in os.listdir(TEMP_DIR) if os.path.isfile(os.path.join(TEMP_DIR, f))]
            if temp_files:
                filepath = max(temp_files, key=os.path.getctime)
            else:
                logger.error(f"No files found in {TEMP_DIR}")
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
        error_msg = str(e).lower()

        if 'twitter' in error_msg or 'x.com' in error_msg:
            return "TWITTER_ERROR", None, None

        return None, None, None

# URL storage for callback data
url_storage = {}

# Helper: Create quality keyboard
def create_quality_keyboard(url: str, platform: str) -> InlineKeyboardMarkup:
    """Create inline keyboard for quality selection"""
    # Create short hash for URL to avoid callback data length limit
    url_hash = str(hash(url))[-8:]  # Use last 8 chars of hash
    url_storage[url_hash] = url
    
    keyboard = []
    
    if platform == "youtube":
        keyboard = [
            [
                InlineKeyboardButton("📹 Best Video", callback_data=f"dl|video|best|{url_hash}"),
                InlineKeyboardButton("🎵 Audio Only", callback_data=f"dl|audio|best|{url_hash}")
            ],
            [
                InlineKeyboardButton("720p", callback_data=f"dl|video|720|{url_hash}"),
                InlineKeyboardButton("480p", callback_data=f"dl|video|480|{url_hash}"),
                InlineKeyboardButton("360p", callback_data=f"dl|video|360|{url_hash}")
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]
    elif platform == "twitter":
        keyboard = [
            [
                InlineKeyboardButton("📹 Download Video", callback_data=f"dl|video|best|{url_hash}"),
                InlineKeyboardButton("🎵 Audio Only", callback_data=f"dl|audio|best|{url_hash}")
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]
    else:  # TikTok
        keyboard = [
            [
                InlineKeyboardButton("📹 Download Video", callback_data=f"dl|video|best|{url_hash}"),
                InlineKeyboardButton("🎵 Audio Only", callback_data=f"dl|audio|best|{url_hash}")
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ]
    
    return InlineKeyboardMarkup(keyboard)

# Command: /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message"""
    welcome_text = (
        "🎬 *Welcome to Video Downloader Bot!*\n\n"
        "I can download videos from:\n"
        "• YouTube (including Shorts)\n"
        "• TikTok\n"
        "• Twitter/X\n\n"
        "*How to use:*\n"
        "1️⃣ Send me a video link\n"
        "2️⃣ Choose quality/format\n"
        "3️⃣ Get your file!\n\n"
        "📝 *Commands:*\n"
        "/start - Show this message\n"
        "/help - Get help\n"
        "/about - About this bot\n\n"
        "Just send me a link to get started! 🚀"
    )
    
    await update.message.reply_text(
        welcome_text,
        parse_mode=constants.ParseMode.MARKDOWN
    )

# Command: /help
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message"""
    help_text = (
        "❓ *Help & FAQ*\n\n"
        "*Supported Links:*\n"
        "• YouTube videos & shorts\n"
        "• TikTok videos\n"
        "• Twitter/X videos\n\n"
        "*File Size Limit:*\n"
        "Maximum 50MB (Telegram limit)\n\n"
        "*Troubleshooting:*\n"
        "• Make sure the link is public\n"
        "• Try different quality if download fails\n"
        "• Some videos may be region-locked\n\n"
        "*Tips:*\n"
        "• Lower quality = smaller file size\n"
        "• Audio-only is fastest to download\n"
        "• Be patient with long videos\n\n"
        "Need more help? Contact @Ly\_z\_sa"
    )
    
    await update.message.reply_text(
        help_text,
        parse_mode=constants.ParseMode.MARKDOWN
    )

# Command: /about
async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send about message"""
    about_text = (
        "ℹ️ *About This Bot*\n\n"
        "Version: 2.3\n"
        "Updated: September 2025\n\n"
        "This bot helps you download videos from various platforms "
        "directly to Telegram.\n\n"
        "*Features:*\n"
        "✅ Multiple quality options\n"
        "✅ Audio extraction\n"
        "✅ Fast processing\n"
        "✅ Clean interface\n\n"
        "*Privacy:*\n"
        "• No data is stored\n"
        "• Files are deleted after sending\n"
        "• No logs are kept\n\n"
        "Made with ❤️ by @Ly\_z\_sa"
    )
    
    await update.message.reply_text(
        about_text,
        parse_mode=constants.ParseMode.MARKDOWN
    )

# Handler: Process links
async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process video links"""
    message = update.message
    url = message.text.strip()
    
    # Validate URL
    is_valid, platform = is_valid_url(url)
    
    if not is_valid:
        await message.reply_text(
            "❌ *Invalid or unsupported link!*\n\n"
            "Please send a valid YouTube, TikTok or Twitter/X link.",
            parse_mode=constants.ParseMode.MARKDOWN
        )
        return
    
    # Send download options immediately (no slow info fetching)
    await message.reply_text(
        f"📹 *{platform.title()} video detected!*\n\n"
        "Choose download option:",
        parse_mode=constants.ParseMode.MARKDOWN,
        reply_markup=create_quality_keyboard(url, platform)
    )

# Handler: Callback queries
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard callbacks"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    # Handle cancel
    if data == "cancel":
        await query.message.edit_text("❌ Download cancelled.")
        return
    
    # Parse callback data
    parts = data.split("|")
    if len(parts) != 4 or parts[0] != "dl":
        return
    
    _, format_type, quality, url_hash = parts
    
    # Get URL from storage
    url = url_storage.get(url_hash)
    if not url:
        await query.message.edit_text(
            "❌ *Session expired!*\n"
            "Please send the link again.",
            parse_mode=constants.ParseMode.MARKDOWN
        )
        return
    
    # Update message
    await query.message.edit_text(
        f"⬇️ *Downloading...*\n"
        f"Format: {format_type.title()}\n"
        f"Quality: {quality.upper() if quality != 'best' else 'Best available'}\n\n"
        f"Please wait, this may take a moment...",
        parse_mode=constants.ParseMode.MARKDOWN
    )
    
    # Start download immediately
    filepath, filename, metadata = await download_media(url, format_type, quality)
    
    # Handle Twitter/X specific errors
    if filepath == "TWITTER_ERROR":
        await query.message.edit_text(
            "❌ *Twitter/X Download Failed!*\n\n"
            "Twitter/X has restricted access for downloaders.\n\n"
            "*Alternatives:*\n"
            "• Try a different Twitter video\n"
            "• Use YouTube or TikTok instead\n"
            "• Some Twitter videos may work, others won't\n\n"
            "This is due to Twitter's anti-bot measures.",
            parse_mode=constants.ParseMode.MARKDOWN
        )
        return
    
    if not filepath or not os.path.exists(filepath):
        await query.message.edit_text(
            "❌ *Download failed!*\n\n"
            "Possible reasons:\n"
            "• Video is private or deleted\n"
            "• Network error\n"
            "• Video is region-locked\n\n"
            "Please try again or use different quality.",
            parse_mode=constants.ParseMode.MARKDOWN
        )
        return
    
    # Check file size
    file_size = os.path.getsize(filepath)
    if file_size > MAX_FILE_SIZE:
        os.remove(filepath)
        await query.message.edit_text(
            f"❌ *File too large!*\n\n"
            f"File size: {format_size(file_size)}\n"
            f"Telegram limit: {format_size(MAX_FILE_SIZE)}\n\n"
            "Try downloading with lower quality.",
            parse_mode=constants.ParseMode.MARKDOWN
        )
        return
    
    # Prepare caption
    caption = (
        f"✅ *{metadata['title']}*\n"
        f"👤 {metadata['uploader']}\n"
        f"📊 {format_size(file_size)}"
    )
    
    # Send file immediately (no status update delay)
    upload_success = False
    try:
        
        with open(filepath, 'rb') as file_obj:
            if format_type == "audio":
                await context.bot.send_audio(
                    chat_id=query.message.chat_id,
                    audio=file_obj,
                    caption=caption,
                    parse_mode=constants.ParseMode.MARKDOWN,
                    title=metadata['title'][:100],
                    performer=metadata['uploader'][:100],
                    filename=filename
                )
            else:
                await context.bot.send_video(
                    chat_id=query.message.chat_id,
                    video=file_obj,
                    caption=caption,
                    parse_mode=constants.ParseMode.MARKDOWN,
                    supports_streaming=True,
                    filename=filename
                )
        
        upload_success = True
        # Delete the status message after successful upload
        try:
            await query.message.delete()
        except:
            pass
        
    except Exception as e:
        logger.error(f"Upload error: {str(e)}")
        # Only show error if upload actually failed
        try:
            await query.message.edit_text(
                "❌ *Upload failed!*\n"
                "Please try again later.",
                parse_mode=constants.ParseMode.MARKDOWN
            )
        except:
            pass
    
    finally:
        # Clean up
        if os.path.exists(filepath):
            os.remove(filepath)

# Handler: Unknown commands
async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle unknown commands"""
    await update.message.reply_text(
        "❓ Unknown command. Use /help for available commands."
    )

# Error handler
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Log errors"""
    try:
        logger.error(f"Update {update} caused error {context.error}")
        
        if update and update.message:
            try:
                await update.message.reply_text(
                    "⚠️ An error occurred. Please try again later."
                )
            except:
                pass  # Ignore if we can't send error message
    except Exception as e:
        logger.error(f"Error in error handler: {str(e)}")
        pass


# Main function
def main():
    """Start the bot and webserver for Render"""
    try:
        # Start web server in a separate thread (so Render sees a port)
        threading.Thread(target=run_webserver, daemon=True).start()

        # Start Telegram bot
        application = Application.builder().token(TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("help", help_command))
        application.add_handler(CommandHandler("about", about_command))
        application.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_link
        ))
        application.add_handler(CallbackQueryHandler(handle_callback))
        application.add_handler(MessageHandler(filters.COMMAND, unknown_command))
        application.add_error_handler(error_handler)

        print("🤖 Bot is starting...")
        application.run_polling(drop_pending_updates=True)
    except Exception as e:
        print(f"Bot crashed: {str(e)}. Restarting in 5 seconds...")
        import time
        time.sleep(5)
        main()

if __name__ == "__main__":
    main()





import os
import logging
import json
import threading
import time
import glob
from datetime import date
from flask import Flask, request, abort
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
import yt_dlp

# ----------------- CONFIGURATION & ENV VARS -----------------
TOKEN = os.getenv("TOKEN")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "nexouya")
BOT_USERNAME = os.getenv("BOT_USERNAME", "@nexouya_bot")
PORT = int(os.environ.get("PORT", 10000))
RENDER_URL = os.getenv("RENDER_URL")

if not TOKEN or not RENDER_URL:
    raise RuntimeError("TOKEN and RENDER_URL environment variables are mandatory!")

# ----------------- LOGGING SETUP -----------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ----------------- FLASK & BOT INIT -----------------
app = Flask(__name__)
# threaded=False is CRITICAL for Flask + telebot webhooks
bot = telebot.TeleBot(TOKEN, threaded=False) 

DOWNLOAD_DIR = 'downloads'
if not os.path.exists(DOWNLOAD_DIR):
    os.makedirs(DOWNLOAD_DIR)

# ----------------- THREAD-SAFE DATABASE MANAGER -----------------
class DatabaseManager:
    def __init__(self, file_path):
        self.file_path = file_path
        self.lock = threading.Lock()

    def execute(self, func):
        with self.lock:
            if not os.path.exists(self.file_path):
                db = {"file_caption": "🎬 {title}\n\n💾 حجم: {size}\n⚡️ Nexouya Down\n🤖 {bot}"}
            else:
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    try: db = json.load(f)
                    except: db = {}
            
            if "file_caption" not in db: db["file_caption"] = "🎬 {title}\n\n💾 حجم: {size}\n⚡️ Nexouya Down\n🤖 {bot}"
            
            result = func(db)
            
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(db, f, indent=4, ensure_ascii=False)
            return result

db_manager = DatabaseManager('database.json')

def init_user(chat_id, username):
    def operation(db):
        chat_id_str = str(chat_id)
        today = date.today().isoformat()
        if chat_id_str not in db:
            db[chat_id_str] = {"username": username if username else "NoUsername", "coins": 5, "downloads": 0, "banned": False, "daily_stats": {}}
        if "daily_stats" not in db[chat_id_str]: db[chat_id_str]["daily_stats"] = {}
        if today not in db[chat_id_str]["daily_stats"]: db[chat_id_str]["daily_stats"][today] = {"count": 0, "size_bytes": 0}
        return db[chat_id_str]
    return db_manager.execute(operation)

def deduct_coin(chat_id, is_admin):
    if is_admin: return True
    def operation(db):
        if db[str(chat_id)]['coins'] > 0:
            db[str(chat_id)]['coins'] -= 1
            return True
        return False
    return db_manager.execute(operation)

def refund_coin(chat_id, is_admin):
    if is_admin: return
    def operation(db): db[str(chat_id)]['coins'] += 1
    db_manager.execute(operation)

def update_daily_stat(chat_id, file_size_bytes):
    def operation(db):
        chat_id_str = str(chat_id)
        today = date.today().isoformat()
        if chat_id_str in db:
            if today not in db[chat_id_str]["daily_stats"]: db[chat_id_str]["daily_stats"][today] = {"count": 0, "size_bytes": 0}
            db[chat_id_str]["daily_stats"][today]["count"] += 1
            db[chat_id_str]["daily_stats"][today]["size_bytes"] += file_size_bytes
            db[chat_id_str]["downloads"] += 1
    db_manager.execute(operation)

# ----------------- THREAD-SAFE DOWNLOAD MANAGER -----------------
class DownloadManager:
    def __init__(self):
        self.active_downloads = set()
        self.lock = threading.Lock()

    def add(self, chat_id):
        with self.lock:
            if chat_id in self.active_downloads: return False
            self.active_downloads.add(chat_id)
            return True

    def remove(self, chat_id):
        with self.lock:
            self.active_downloads.discard(chat_id)

download_manager = DownloadManager()
user_sessions = {}

# ----------------- KEYBOARDS -----------------
def main_markup(is_admin=False):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("👤 حساب کاربری", "ℹ️ راهنمای ربات")
    if is_admin: markup.add("👨‍💻 پنل مدیریت")
    return markup

def download_mode_markup():
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("⚡️ ورود به حالت دانلود", callback_data="enter_dl_mode"))
    markup.add(InlineKeyboardButton("❌ لغو عملیات", callback_data="dl_cancel"))
    return markup

def quality_markup():
    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(
        InlineKeyboardButton("🎬 4K", callback_data="res_2160"), InlineKeyboardButton("🎬 2K", callback_data="res_1440"), InlineKeyboardButton("🎬 1080p", callback_data="res_1080"),
        InlineKeyboardButton("📱 720p", callback_data="res_720"), InlineKeyboardButton("📱 480p", callback_data="res_480"), InlineKeyboardButton("📱 360p", callback_data="res_360"),
        InlineKeyboardButton("📉 240p", callback_data="res_240"), InlineKeyboardButton("📉 144p", callback_data="res_144"), InlineKeyboardButton("🎵 MP3", callback_data="res_audio"),
        InlineKeyboardButton("❌ لغو عملیات", callback_data="dl_cancel")
    )
    return markup

def panel_markup():
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("✏️ تغییر کپشن", callback_data="admin_set_caption"), InlineKeyboardButton("💰 اهدای سکه", callback_data="admin_add_coin"),
        InlineKeyboardButton("🎁 اهدای همگانی", callback_data="admin_bulk_coin"), InlineKeyboardButton("📊 آمار امروز", callback_data="admin_daily_stats"),
        InlineKeyboardButton("🏆 رتبه‌بندی", callback_data="admin_leaderboard"), InlineKeyboardButton("🔍 اطلاعات کاربر", callback_data="admin_user_info"),
        InlineKeyboardButton("🚫 بن/آنبن", callback_data="admin_ban"), InlineKeyboardButton("📢 همگانی", callback_data="admin_broadcast")
    )
    return markup

# ----------------- PROGRESS BAR & WORKER -----------------
last_edit_time = {}

def progress_hook(d, chat_id, msg_id):
    if d['status'] == 'downloading':
        current_time = time.time()
        # 3 second throttle to prevent FloodWait
        if chat_id not in last_edit_time or (current_time - last_edit_time.get(chat_id, 0)) > 3:
            try:
                percent = d.get('_percent_str', 'N/A').strip()
                speed = d.get('_speed_str', 'N/A').strip()
                text = f"⏳ **دانلود...**\n[{percent}]\n🚀 `{speed}`"
                bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, parse_mode="Markdown")
                last_edit_time[chat_id] = current_time
            except Exception:
                pass # Silent handling for FloodWait
                 
    elif d['status'] == 'finished':
        try:
            bot.edit_message_text("✅ دانلود پایان یافت!\n⚙️ مرج صدا/تصویر...", chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
        # Memory leak fix
        last_edit_time.pop(chat_id, None)

def download_worker(chat_id, msg_id, url, resolution):
    user = init_user(chat_id, None)
    is_admin = user.get('username', '').lower() == ADMIN_USERNAME.lower()

    ydl_opts = {
        'outtmpl': f'{DOWNLOAD_DIR}/{chat_id}_%(id)s.%(ext)s',
        'noplaylist': True,
        'quiet': True,
        'nocheckcertificate': True,
        'progress_hooks': [lambda d: progress_hook(d, chat_id, msg_id)],
        'merge_output_format': 'mp4',
        # ⚡ Performance Optimization
        'concurrent_fragment_downloads': 4,
        'retries': 1,
    }

    if resolution == 'res_audio':
        ydl_opts['format'] = 'ba/b' # Fast audio mode
        ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]
    else:
        height = resolution.replace('res_', '')
        # ⚡ Fast mode format selection
        ydl_opts['format'] = f'bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b'

    filename = None
    try:
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl_info:
            info = ydl_info.extract_info(url, download=False)
            video_title = info.get('title', 'Unknown')

        bot.edit_message_text(f"🎬 **عنوان:** `{video_title}`\n\n⏳ شروع دانلود...", chat_id=chat_id, message_id=msg_id, parse_mode="Markdown")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        # 🧠 Stability: Handle missing files using glob search instead of static filename
        files = glob.glob(f"{DOWNLOAD_DIR}/{chat_id}_*.*")
        if not files:
            raise Exception("فایل دانلود شده روی سرور یافت نشد!")
        
        filename = max(files, key=os.path.getctime) # Get newest file
        
        if resolution != 'res_audio' and not filename.endswith('.mp4'):
            new_name = os.path.splitext(filename)[0] + '.mp4'
            os.rename(filename, new_name)
            filename = new_name

        file_size = os.path.getsize(filename)
        if file_size > 104857600: # 100MB
            refund_coin(chat_id, is_admin)
            bot.edit_message_text("❌ **حجم فایل بیش از ۱۰۰ مگابایت است.**\n🪙 سکه بازگردانده شد.", chat_id=chat_id, message_id=msg_id, parse_mode="Markdown")
            return

        bot.edit_message_text("📤 آپلود به تلگرام...", chat_id=chat_id, message_id=msg_id)

        safe_title = video_title.replace('*', '').replace('_', '').replace('`', '')
        size_str = f"{file_size / (1024*1024):.2f} MB"
        
        def get_caption(db): return db.get("file_caption", "🎬 {title}\n💾 {size}\n🤖 {bot}")
        caption_template = db_manager.execute(get_caption)
        final_caption = caption_template.replace("{title}", safe_title).replace("{size}", size_str).replace("{bot}", BOT_USERNAME)

        with open(filename, 'rb') as file:
            if resolution == 'res_audio':
                bot.send_audio(chat_id, file, caption=final_caption, timeout=120)
            else:
                bot.send_video(chat_id, file, caption=final_caption, timeout=120, supports_streaming=True)

        update_daily_stat(chat_id, file_size)
        try: bot.delete_message(chat_id, msg_id)
        except: pass

    except yt_dlp.utils.DownloadError as e:
        logger.error(f"yt-dlp DownloadError: {e}")
        refund_coin(chat_id, is_admin)
        try: bot.edit_message_text("❌ **خطا در دانلود!**\n🪙 سکه بازگردانده شد.", chat_id=chat_id, message_id=msg_id, parse_mode="Markdown")
        except: pass
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        refund_coin(chat_id, is_admin)
        try: bot.edit_message_text("⚠️ **خطای سیستمی!**\n🪙 سکه بازگردانده شد.", chat_id=chat_id, message_id=msg_id, parse_mode="Markdown")
        except: pass
    finally:
        # 🧯 Critical: Safe file cleanup after upload
        if filename and os.path.exists(filename):
            try: os.remove(filename)
            except: pass
        
        # Cleanup partial files if process crashed before merge
        for f in glob.glob(f"{DOWNLOAD_DIR}/{chat_id}_*"):
            try: os.remove(f)
            except: pass
            
        # 🧯 Critical: Thread-safe download lock release
        download_manager.remove(chat_id)


# ----------------- BOT HANDLERS -----------------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    username = message.from_user.username
    is_admin = (username and username.lower() == ADMIN_USERNAME.lower())
    user = init_user(message.chat.id, username)
    text = f"💎 **Nexouya Down** 💎\n\n🪙 موجودی: `{user['coins']}` سکه\nلینک رو بفرست:"
    bot.send_message(message.chat.id, text, parse_mode="Markdown", reply_markup=main_markup(is_admin))

@bot.message_handler(func=lambda message: message.text and message.text.startswith('http'))
def handle_link(message):
    chat_id = str(message.chat.id)
    user = init_user(chat_id, message.from_user.username)
    is_admin = (message.from_user.username and message.from_user.username.lower() == ADMIN_USERNAME.lower())

    if user.get('banned', False): return bot.reply_to(message, "🚫 مسدود شده‌اید.")
    if not is_admin and user['coins'] <= 0: return bot.reply_to(message, "❌ سکه تمام شده!")
    
    user_sessions[chat_id] = message.text
    bot.reply_to(message, "🔗 لینک دریافت شد!\n\n⚡️ وارد حالت دانلود شوید:", reply_markup=download_mode_markup(), parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    chat_id = str(call.message.chat.id)
    username = call.from_user.username
    is_admin = (username and username.lower() == ADMIN_USERNAME.lower())
    data = call.data

    if data.startswith('admin_') and not is_admin:
        return bot.answer_callback_query(call.id, "⛔️ ادمین نیستید!", show_alert=True)

    if data == "enter_dl_mode":
        if chat_id not in user_sessions: return bot.answer_callback_query(call.id, "❌ لینک منقضی شده!", show_alert=True)
        warning_text = "⚡️ **حالت دانلود**\n\n⚠️ اگر کیفتی بالاتر از ویدیو باشد، بالاترین کیفیت موجود دانلود می‌شود.\n\n👇 کیفیت را انتخاب کنید:"
        bot.edit_message_text(warning_text, chat_id=chat_id, message_id=call.message.message_id, parse_mode="Markdown", reply_markup=quality_markup())

    elif data.startswith('res_'):
        if chat_id not in user_sessions:
            return bot.answer_callback_query(call.id, "❌ لینک منقضی شده!", show_alert=True)
            
        if not download_manager.add(chat_id):
            return bot.answer_callback_query(call.id, "⏳ دانلود دیگری در حال انجام است!", show_alert=True)

        url = user_sessions.pop(chat_id)
        
        if not deduct_coin(chat_id, is_admin):
            download_manager.remove(chat_id)
            return bot.answer_callback_query(call.id, "سکه کافی ندارید!", show_alert=True)

        msg = bot.edit_message_text("⏳ **آماده‌سازی...**", chat_id=chat_id, message_id=call.message.message_id, parse_mode="Markdown")
        
        # 🚀 Performance: Run in daemon thread to not block Flask
        thread = threading.Thread(target=download_worker, args=(int(chat_id), msg.message_id, url, data), daemon=True)
        thread.start()
        
    elif data == "dl_cancel":
        user_sessions.pop(chat_id, None)
        bot.edit_message_text("❌ عملیات لغو شد.", chat_id=chat_id, message_id=call.message.message_id)

# ----------------- FLASK WEBHOOK ROUTES -----------------
@app.route(f'/{TOKEN}', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ''
    else:
        abort(403)

@app.route('/', methods=['GET'])
def health_check():
    return 'OK', 200

# ----------------- MAIN EXECUTION -----------------
if __name__ == '__main__':
    logger.info(f"Starting bot on port {PORT}...")
    logger.info(f"Setting webhook to: {RENDER_URL}/{TOKEN}")
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=f"{RENDER_URL}/{TOKEN}")
    # Gunicorn will bind this, but for local testing:
    app.run(host='0.0.0.0', port=PORT)
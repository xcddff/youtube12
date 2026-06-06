import os
import logging
import json
import threading
import time
import glob
import requests
from datetime import date
from flask import Flask, request, abort
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
import yt_dlp

# ----------------- CONFIGURATION & ENV VARS -----------------
TOKEN = os.getenv("TOKEN", "").strip()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "nexouya").strip().lstrip("@")
BOT_USERNAME = os.getenv("BOT_USERNAME", "@nexouya_bot").strip()
PORT = int(os.environ.get("PORT", 10000))
RENDER_URL = os.getenv("RENDER_URL", "").strip().rstrip("/")

if BOT_USERNAME and not BOT_USERNAME.startswith("@"):
    BOT_USERNAME = "@" + BOT_USERNAME.lstrip("@")

if not TOKEN or not RENDER_URL:
    raise RuntimeError("TOKEN and RENDER_URL environment variables are mandatory!")

# ----------------- LOGGING SETUP -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ----------------- FLASK & BOT INIT -----------------
app = Flask(__name__)
bot = telebot.TeleBot(TOKEN, threaded=True, num_threads=8)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ----------------- WEBHOOK STARTUP -----------------
_webhook_started = False
_webhook_lock = threading.Lock()

def setup_webhook():
    global _webhook_started
    with _webhook_lock:
        if _webhook_started:
            return
        _webhook_started = True

    webhook_url = f"{RENDER_URL}/{TOKEN}"
    set_url = f"https://api.telegram.org/bot{TOKEN}/setWebhook"
    delete_url = f"https://api.telegram.org/bot{TOKEN}/deleteWebhook"

    # Give Gunicorn a moment to bind the port first
    time.sleep(5)

    logger.info("Preparing webhook: %s", webhook_url)

    try:
        # Clear old webhook config first
        try:
            r = requests.get(delete_url, timeout=15)
            logger.info("deleteWebhook response: %s", r.text)
        except Exception as e:
            logger.warning("deleteWebhook failed (ignored): %s", e)

        payload = {
            "url": webhook_url,
            "allowed_updates": json.dumps(["message", "callback_query"]),
            "drop_pending_updates": "false",
        }

        for attempt in range(1, 4):
            try:
                resp = requests.post(set_url, data=payload, timeout=20)
                body = resp.text
                try:
                    data = resp.json()
                except Exception:
                    data = {"ok": False, "raw": body}

                if resp.ok and data.get("ok"):
                    logger.info("Webhook set successfully: %s", data)
                    return

                logger.error(
                    "Webhook set failed (attempt %s/3) status=%s body=%s",
                    attempt, resp.status_code, body
                )
            except Exception as e:
                logger.exception("Webhook setup exception (attempt %s/3): %s", attempt, e)

            time.sleep(3)

        logger.critical("Failed to set webhook after retries.")
    except Exception as e:
        logger.exception("Fatal webhook setup error: %s", e)

# Start once when module is imported by Gunicorn worker
threading.Thread(target=setup_webhook, daemon=True).start()

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
                try:
                    with open(self.file_path, "r", encoding="utf-8") as f:
                        db = json.load(f)
                except Exception:
                    db = {}

            if "file_caption" not in db:
                db["file_caption"] = "🎬 {title}\n\n💾 حجم: {size}\n⚡️ Nexouya Down\n🤖 {bot}"

            result = func(db)

            tmp_path = self.file_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(db, f, indent=4, ensure_ascii=False)
            os.replace(tmp_path, self.file_path)

            return result

db_manager = DatabaseManager("database.json")

def init_user(chat_id, username):
    def operation(db):
        chat_id_str = str(chat_id)
        today = date.today().isoformat()

        if chat_id_str not in db:
            db[chat_id_str] = {
                "username": username if username else "NoUsername",
                "coins": 5,
                "downloads": 0,
                "banned": False,
                "daily_stats": {}
            }
        else:
            if username and db[chat_id_str].get("username") in ("", "NoUsername", None):
                db[chat_id_str]["username"] = username

        if "daily_stats" not in db[chat_id_str]:
            db[chat_id_str]["daily_stats"] = {}
        if today not in db[chat_id_str]["daily_stats"]:
            db[chat_id_str]["daily_stats"][today] = {"count": 0, "size_bytes": 0}

        return db[chat_id_str]

    return db_manager.execute(operation)

def deduct_coin(chat_id, is_admin):
    if is_admin:
        return True

    def operation(db):
        chat_id_str = str(chat_id)
        if chat_id_str not in db:
            db[chat_id_str] = {
                "username": "NoUsername",
                "coins": 5,
                "downloads": 0,
                "banned": False,
                "daily_stats": {}
            }
        if db[chat_id_str]["coins"] > 0:
            db[chat_id_str]["coins"] -= 1
            return True
        return False

    return db_manager.execute(operation)

def refund_coin(chat_id, is_admin):
    if is_admin:
        return

    def operation(db):
        chat_id_str = str(chat_id)
        if chat_id_str not in db:
            db[chat_id_str] = {
                "username": "NoUsername",
                "coins": 0,
                "downloads": 0,
                "banned": False,
                "daily_stats": {}
            }
        db[chat_id_str]["coins"] += 1

    db_manager.execute(operation)

def update_daily_stat(chat_id, file_size_bytes):
    def operation(db):
        chat_id_str = str(chat_id)
        today = date.today().isoformat()
        if chat_id_str in db:
            if "daily_stats" not in db[chat_id_str]:
                db[chat_id_str]["daily_stats"] = {}
            if today not in db[chat_id_str]["daily_stats"]:
                db[chat_id_str]["daily_stats"][today] = {"count": 0, "size_bytes": 0}
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
            if chat_id in self.active_downloads:
                return False
            self.active_downloads.add(chat_id)
            return True

    def remove(self, chat_id):
        with self.lock:
            self.active_downloads.discard(chat_id)

download_manager = DownloadManager()

# ----------------- STATE LOCKS -----------------
user_sessions = {}
sessions_lock = threading.Lock()
last_edit_time = {}
progress_lock = threading.Lock()

def set_session(chat_id, url):
    with sessions_lock:
        user_sessions[chat_id] = url

def pop_session(chat_id):
    with sessions_lock:
        return user_sessions.pop(chat_id, None)

def has_session(chat_id):
    with sessions_lock:
        return chat_id in user_sessions

# ----------------- HELPERS -----------------
def answer_callback(call, text=None, alert=False):
    try:
        bot.answer_callback_query(call.id, text=text, show_alert=alert)
    except Exception:
        pass

def safe_edit_message(chat_id, msg_id, text, parse_mode=None, reply_markup=None):
    try:
        return bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )
    except Exception:
        return None

def safe_delete_message(chat_id, msg_id):
    try:
        bot.delete_message(chat_id, msg_id)
    except Exception:
        pass

# ----------------- KEYBOARDS -----------------
def main_markup(is_admin=False):
    markup = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add("👤 حساب کاربری", "ℹ️ راهنمای ربات")
    if is_admin:
        markup.add("👨‍💻 پنل مدیریت")
    return markup

def download_mode_markup():
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("⚡️ ورود به حالت دانلود", callback_data="enter_dl_mode"))
    markup.add(InlineKeyboardButton("❌ لغو عملیات", callback_data="dl_cancel"))
    return markup

def quality_markup():
    markup = InlineKeyboardMarkup(row_width=3)
    markup.add(
        InlineKeyboardButton("🎬 4K", callback_data="res_2160"),
        InlineKeyboardButton("🎬 2K", callback_data="res_1440"),
        InlineKeyboardButton("🎬 1080p", callback_data="res_1080")
    )
    markup.add(
        InlineKeyboardButton("📱 720p", callback_data="res_720"),
        InlineKeyboardButton("📱 480p", callback_data="res_480"),
        InlineKeyboardButton("📱 360p", callback_data="res_360")
    )
    markup.add(
        InlineKeyboardButton("📉 240p", callback_data="res_240"),
        InlineKeyboardButton("📉 144p", callback_data="res_144"),
        InlineKeyboardButton("🎵 MP3", callback_data="res_audio")
    )
    markup.add(InlineKeyboardButton("❌ لغو عملیات", callback_data="dl_cancel"))
    return markup

def panel_markup():
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("✏️ تغییر کپشن", callback_data="admin_set_caption"),
        InlineKeyboardButton("💰 اهدای سکه به یک کاربر", callback_data="admin_add_coin"),
        InlineKeyboardButton("🎁 اهدای سکه همگانی", callback_data="admin_bulk_coin"),
        InlineKeyboardButton("📊 آمار دانلود امروز", callback_data="admin_daily_stats"),
        InlineKeyboardButton("🏆 رتبه‌بندی روزانه", callback_data="admin_leaderboard"),
        InlineKeyboardButton("🔍 بررسی اطلاعات کاربر", callback_data="admin_user_info"),
        InlineKeyboardButton("🚫 بن / آنبن کاربر", callback_data="admin_ban"),
        InlineKeyboardButton("📢 ارسال پیام همگانی", callback_data="admin_broadcast")
    )
    return markup

# ----------------- PROGRESS BAR LOGIC -----------------
def progress_hook(d, chat_id, msg_id):
    if d.get("status") == "downloading":
        current_time = time.time()
        with progress_lock:
            last = last_edit_time.get(chat_id, 0)
            if (current_time - last) <= 3:
                return
            last_edit_time[chat_id] = current_time

        try:
            percent = d.get("_percent_str", "N/A").strip()
            speed = d.get("_speed_str", "N/A").strip()
            downloaded = d.get("_downloaded_str", "N/A").strip()

            try:
                p_float = float(percent.replace("%", "")) / 10
                blocks = int(p_float)
                bar = "▓" * blocks + "░" * (10 - blocks)
            except Exception:
                bar = "░" * 10

            text = (
                f"⏳ **در حال دانلود مدیا...**\n\n"
                f"[{bar}] {percent}\n\n"
                f"📥 حجم دریافت شده: `{downloaded}`\n"
                f"🚀 سرعت دانلود: `{speed}`"
            )
            safe_edit_message(chat_id, msg_id, text, parse_mode="Markdown")
        except Exception:
            pass

    elif d.get("status") == "finished":
        try:
            safe_edit_message(chat_id, msg_id, "✅ دانلود پایان یافت!\n⚙️ در حال مرج کردن صدا و تصویر...")
        except Exception:
            pass
        with progress_lock:
            last_edit_time.pop(chat_id, None)

# ----------------- DOWNLOAD WORKER -----------------
def download_worker(chat_id, msg_id, url, resolution):
    user = init_user(chat_id, None)
    is_admin = user.get("username", "").lower() == ADMIN_USERNAME.lower()

    ydl_opts = {
        "outtmpl": f"{DOWNLOAD_DIR}/{chat_id}_%(id)s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "nocheckcertificate": True,
        "progress_hooks": [lambda d: progress_hook(d, chat_id, msg_id)],
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 4,
        "retries": 1,
    }

    if resolution == "res_audio":
        ydl_opts["format"] = "ba/b"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        height = resolution.replace("res_", "")
        ydl_opts["format"] = f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b"

    filename = None
    try:
        with yt_dlp.YoutubeDL({"quiet": True}) as ydl_info:
            info = ydl_info.extract_info(url, download=False)
            video_title = info.get("title", "Unknown")

        safe_edit_message(
            chat_id,
            msg_id,
            f"🎬 **عنوان:** `{video_title}`\n\n⏳ شروع دانلود...",
            parse_mode="Markdown"
        )

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)

        files = [
            f for f in glob.glob(f"{DOWNLOAD_DIR}/{chat_id}_*")
            if not f.endswith(".part") and not f.endswith(".ytdl")
        ]
        if not files:
            raise Exception("فایل دانلود شده روی سرور یافت نشد!")

        filename = max(files, key=os.path.getctime)

        if resolution != "res_audio" and not filename.endswith(".mp4"):
            new_name = os.path.splitext(filename)[0] + ".mp4"
            os.rename(filename, new_name)
            filename = new_name

        file_size = os.path.getsize(filename)
        if file_size > 104857600:
            refund_coin(chat_id, is_admin)
            safe_edit_message(
                chat_id,
                msg_id,
                "❌ **حجم فایل بیش از ۱۰۰ مگابایت است.**\n🪙 سکه بازگردانده شد.",
                parse_mode="Markdown"
            )
            return

        safe_edit_message(chat_id, msg_id, "📤 آپلود به تلگرام...")

        safe_title = video_title.replace("*", "").replace("_", "").replace("`", "")
        size_str = f"{file_size / (1024 * 1024):.2f} MB"

        def get_caption(db):
            return db.get("file_caption", "🎬 {title}\n💾 {size}\n🤖 {bot}")

        caption_template = db_manager.execute(get_caption)
        final_caption = (
            caption_template
            .replace("{title}", safe_title)
            .replace("{size}", size_str)
            .replace("{bot}", BOT_USERNAME)
        )

        with open(filename, "rb") as file:
            if resolution == "res_audio":
                bot.send_audio(chat_id, file, caption=final_caption, timeout=120)
            else:
                bot.send_video(
                    chat_id,
                    file,
                    caption=final_caption,
                    timeout=120,
                    supports_streaming=True
                )

        update_daily_stat(chat_id, file_size)
        safe_delete_message(chat_id, msg_id)

    except yt_dlp.utils.DownloadError as e:
        logger.error("yt-dlp DownloadError: %s", e)
        refund_coin(chat_id, is_admin)
        safe_edit_message(
            chat_id,
            msg_id,
            "❌ **خطا در دانلود!**\n🪙 سکه بازگردانده شد.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.exception("Unexpected error: %s", e)
        refund_coin(chat_id, is_admin)
        safe_edit_message(
            chat_id,
            msg_id,
            "⚠️ **خطای سیستمی!**\n🪙 سکه بازگردانده شد.",
            parse_mode="Markdown"
        )
    finally:
        if filename and os.path.exists(filename):
            try:
                os.remove(filename)
            except Exception:
                pass

        for f in glob.glob(f"{DOWNLOAD_DIR}/{chat_id}_*"):
            try:
                os.remove(f)
            except Exception:
                pass

        download_manager.remove(chat_id)

# ----------------- BOT HANDLERS -----------------
@bot.message_handler(commands=["start"])
def send_welcome(message):
    username = message.from_user.username
    is_admin = bool(username and username.lower() == ADMIN_USERNAME.lower())
    user = init_user(message.chat.id, username)

    text = (
        "💎 **به ربات هوشمند Nexouya Down خوش آمدید!** 💎\n\n"
        "🚀 یک دانلودر قدرتمند و سریع برای دریافت ویدیوها و موزیک‌ها!\n"
        "کافیه لینک رو بفرستی و وارد حالت دانلود شو. 😉\n\n"
        f"🪙 **موجودی شما:** `{user['coins']}` سکه\n\n"
        "👇 از دکمه‌های زیر استفاده کن یا مستقیم لینکت رو بفرست:"
    )
    bot.send_message(
        message.chat.id,
        text,
        parse_mode="Markdown",
        reply_markup=main_markup(is_admin)
    )

@bot.message_handler(commands=["panel"])
@bot.message_handler(func=lambda message: message.text == "👨‍💻 پنل مدیریت")
def admin_panel(message):
    username = message.from_user.username
    if username and username.lower() == ADMIN_USERNAME.lower():
        bot.send_message(
            message.chat.id,
            "👨‍💻 **پنل مدیریت Nexouya Down**\n\nدسترسی کامل به سیستم ربات:",
            reply_markup=panel_markup(),
            parse_mode="Markdown"
        )
    else:
        bot.reply_to(message, "⛔️ دسترسی ممنوع است!")

@bot.message_handler(func=lambda message: message.text == "👤 حساب کاربری")
def user_profile(message):
    user = init_user(message.chat.id, message.from_user.username)
    status = "🟢 فعال" if not user.get("banned", False) else "🔴 مسدود"
    text = (
        "👤 **پروفایل کاربری شما**\n\n"
        f"🆔 شناسه: `{message.chat.id}`\n"
        f"🪙 سکه‌ها: `{user['coins']}`\n"
        f"📥 مجموع دانلودها: `{user.get('downloads', 0)}`\n"
        f"🔰 وضعیت حساب: {status}"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text == "ℹ️ راهنمای ربات")
def bot_help(message):
    text = (
        "ℹ️ **راهنمای استفاده از Nexouya Down**\n\n"
        "1️⃣ لینک ویدیو یا موزیک مورد نظرت رو بفرست.\n"
        "2️⃣ روی دکمه **⚡️ ورود به حالت دانلود** کلیک کن.\n"
        "3️⃣ کیفیت دلخواهت رو انتخاب کن.\n"
        "4️⃣ فایل مستقیم تو چت خودت ارسال میشه.\n\n"
        "⚠️ **نکته مهم:** حداکثر حجم فایل قابل ارسال ۱۰۰ مگابایت است.\n"
        "💡 *در صورت بروز خطا، سکه خودکار برمی‌گرده.*"
    )
    bot.send_message(message.chat.id, text, parse_mode="Markdown")

@bot.message_handler(func=lambda message: message.text and message.text.startswith("http"))
def handle_link(message):
    chat_id = str(message.chat.id)
    user = init_user(chat_id, message.from_user.username)
    is_admin = bool(message.from_user.username and message.from_user.username.lower() == ADMIN_USERNAME.lower())

    if user.get("banned", False):
        return bot.reply_to(message, "🚫 حساب شما توسط مدیریت مسدود شده است.")
    if not is_admin and user["coins"] <= 0:
        return bot.reply_to(message, "❌ **سکه شما تمام شده!**\nبرای افزایش سکه با مدیریت در ارتباط باشید.", parse_mode="Markdown")

    set_session(chat_id, message.text)
    bot.reply_to(
        message,
        "🔗 **لینک شما دریافت شد!**\n\nبرای انتخاب کیفیت و شروع دانلود، دکمه زیر را لمس کنید:",
        reply_markup=download_mode_markup(),
        parse_mode="Markdown"
    )

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    chat_id = str(call.message.chat.id)
    username = call.from_user.username
    is_admin = bool(username and username.lower() == ADMIN_USERNAME.lower())
    data = call.data

    if data.startswith("admin_") and not is_admin:
        return answer_callback(call, "⛔️ شما ادمین نیستید!", alert=True)

    if data == "admin_set_caption":
        answer_callback(call)
        db = db_manager.execute(lambda db: db.get("file_caption", "تنظیم نشده"))
        msg = bot.send_message(
            chat_id,
            "✏️ **تنظیم کپشن فایل‌های دانلود شده**\n\n"
            "متن جدید را بفرستید. می‌توانید از متغیرهای زیر استفاده کنید:\n\n"
            "🔹 `{title}` : جایگزین اسم ویدیو می‌شود\n"
            "🔹 `{size}` : جایگزین حجم فایل می‌شود\n"
            "🔹 `{bot}` : جایگزین آیدی ربات می‌شود\n\n"
            f"📝 **کپشن فعلی:**\n`{db}`",
            parse_mode="Markdown"
        )
        bot.register_next_step_handler(msg, process_set_caption)

    elif data == "admin_daily_stats":
        answer_callback(call)
        db = db_manager.execute(lambda db: db)
        today = date.today().isoformat()
        total_count = 0
        total_size = 0

        for uid, udata in db.items():
            if uid == "file_caption":
                continue
            today_data = udata.get("daily_stats", {}).get(today, {})
            total_count += today_data.get("count", 0)
            total_size += today_data.get("size_bytes", 0)

        size_mb = total_size / (1024 * 1024)
        safe_edit_message(
            chat_id,
            call.message.message_id,
            f"📊 **آمار دانلود امروز ({today})**\n\n📥 تعداد کل: `{total_count}`\n💾 حجم کل: `{size_mb:.2f} MB`",
            parse_mode="Markdown",
            reply_markup=panel_markup()
        )

    elif data == "admin_leaderboard":
        answer_callback(call)
        db = db_manager.execute(lambda db: db)
        today = date.today().isoformat()
        leaderboard = []

        for uid, udata in db.items():
            if uid == "file_caption":
                continue
            today_data = udata.get("daily_stats", {}).get(today, {})
            size = today_data.get("size_bytes", 0)
            count = today_data.get("count", 0)
            if count > 0:
                leaderboard.append({
                    "uid": uid,
                    "username": udata.get("username", "NoName"),
                    "size": size,
                    "count": count
                })

        leaderboard.sort(key=lambda x: x["size"], reverse=True)
        top_30 = leaderboard[:30]

        text = f"🏆 **رتبه‌بندی روزانه ({today})**\n\n"
        if not top_30:
            text += "هنوز دانلودی ثبت نشده."
        else:
            for i, user in enumerate(top_30, 1):
                size_mb = user["size"] / (1024 * 1024)
                text += f"{i}. @{user['username']} - 📥{user['count']} فایل | 💾{size_mb:.1f}MB\n"

        safe_edit_message(
            chat_id,
            call.message.message_id,
            text,
            parse_mode="Markdown",
            reply_markup=panel_markup()
        )

    elif data == "admin_add_coin":
        answer_callback(call)
        msg = bot.send_message(chat_id, "✏️ **آیدی عددی (Chat ID)** کاربر را بفرستید:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_target_user_coin)

    elif data == "admin_bulk_coin":
        answer_callback(call)
        msg = bot.send_message(chat_id, "🎁 **تعداد سکه‌ای که می‌خواهید به تمام کاربران هدیه دهید را وارد کنید:**", parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_bulk_coin)

    elif data == "admin_user_info":
        answer_callback(call)
        msg = bot.send_message(chat_id, "🔍 **آیدی عددی** کاربر را بفرستید:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_user_info)

    elif data == "admin_ban":
        answer_callback(call)
        msg = bot.send_message(chat_id, "🚫 **آیدی عددی** کاربر را بفرستید:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_ban_user)

    elif data == "admin_broadcast":
        answer_callback(call)
        msg = bot.send_message(chat_id, "📢 متن پیام همگانی را بفرستید:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, process_broadcast)

    elif data == "enter_dl_mode":
        if not has_session(chat_id):
            return answer_callback(call, "❌ لینک منقضی شده! دوباره بفرست.", alert=True)

        answer_callback(call, "⚡️ وارد حالت دانلود شدید!")
        warning_text = (
            "⚡️ **حالت دانلود فعال شد!**\n\n"
            "⚠️ اگر کیفتی که انتخاب می‌کنید بالاتر از کیفیت اصلی ویدیو باشد، "
            "ربات خودکار بالاترین کیفیت موجود آن ویدیو را دانلود می‌کند.\n\n"
            "👇 کیفیت دلخواه خود را انتخاب کنید:"
        )
        safe_edit_message(
            chat_id,
            call.message.message_id,
            warning_text,
            parse_mode="Markdown",
            reply_markup=quality_markup()
        )

    elif data == "dl_cancel":
        answer_callback(call, "❌ عملیات لغو شد.")
        pop_session(chat_id)
        safe_edit_message(chat_id, call.message.message_id, "❌ عملیات لغو شد.")

    elif data.startswith("res_"):
        if not has_session(chat_id):
            return answer_callback(call, "❌ لینک منقضی شده! دوباره بفرست.", alert=True)

        if not download_manager.add(chat_id):
            return answer_callback(call, "⏳ یک دانلود در حال انجام است! صبر کنید.", alert=True)

        url = pop_session(chat_id)

        if not deduct_coin(chat_id, is_admin):
            download_manager.remove(chat_id)
            return answer_callback(call, "سکه کافی ندارید!", alert=True)

        answer_callback(call, "🚀 شروع فرآیند...")

        msg = safe_edit_message(
            chat_id,
            call.message.message_id,
            "⏳ **آماده‌سازی سرور برای دانلود...**",
            parse_mode="Markdown"
        )

        thread = threading.Thread(
            target=download_worker,
            args=(int(chat_id), msg.message_id if msg else call.message.message_id, url, data),
            daemon=True
        )
        thread.start()

# ----------------- ADMIN NEXT STEPS -----------------
def process_set_caption(message):
    new_caption = message.text
    def operation(db):
        db["file_caption"] = new_caption
    db_manager.execute(operation)
    bot.reply_to(
        message,
        "✅ **کپشن فایل‌ها با موفقیت تغییر کرد!**\n\nنمونه کپشن جدید:\n"
        + new_caption.replace("{title}", "اسم ویدیو تست").replace("{size}", "12.5 MB").replace("{bot}", BOT_USERNAME),
        parse_mode="Markdown"
    )

def process_target_user_coin(message):
    target_id = message.text.strip()
    msg = bot.reply_to(message, f"👤 کاربر `{target_id}` انتخاب شد.\nتعداد سکه واریزی را وارد کنید:", parse_mode="Markdown")
    bot.register_next_step_handler(msg, lambda m: finish_give_coin(m, target_id))

def finish_give_coin(message, target_id):
    try:
        amount = int(message.text.strip())
        db = db_manager.execute(lambda db: db)
        if target_id in db:
            def operation(db2):
                db2[target_id]["coins"] += amount
            db_manager.execute(operation)
            bot.reply_to(message, f"✅ `{amount}` سکه به کاربر `{target_id}` اهدا شد.", parse_mode="Markdown")
            try:
                bot.send_message(target_id, f"🎉 **هدیه مدیریتی!**\n🎈 شما `{amount}` سکه دریافت کردید.", parse_mode="Markdown")
            except Exception:
                pass
        else:
            bot.reply_to(message, "❌ این کاربر در دیتابیس وجود ندارد.")
    except ValueError:
        bot.reply_to(message, "❌ لطفاً فقط عدد صحیح وارد کنید.")

def process_bulk_coin(message):
    try:
        amount = int(message.text.strip())
        db = db_manager.execute(lambda db: db)
        count = 0
        for uid in list(db.keys()):
            if uid == "file_caption":
                continue
            if uid in db:
                def operation(db2, _uid=uid):
                    db2[_uid]["coins"] += amount
                db_manager.execute(operation)
                count += 1
                try:
                    bot.send_message(uid, f"🎉 **هدیه همگانی!**\n🎈 شما `{amount}` سکه دریافت کردید.", parse_mode="Markdown")
                except Exception:
                    pass
                time.sleep(0.05)
        bot.reply_to(message, f"✅ **اهدای همگانی موفق!**\n`{amount}` سکه به `{count}` کاربر اهدا شد.", parse_mode="Markdown")
    except ValueError:
        bot.reply_to(message, "❌ لطفاً فقط عدد صحیح وارد کنید.")

def process_user_info(message):
    target_id = message.text.strip()
    db = db_manager.execute(lambda db: db)
    if target_id in db:
        u = db[target_id]
        today = date.today().isoformat()
        today_data = u.get("daily_stats", {}).get(today, {})
        text = (
            f"🔍 **اطلاعات کاربر `{target_id}`**\n\n"
            f"👤 یوزرنیم: @{u.get('username', 'ندارد')}\n"
            f"🪙 سکه‌ها: {u.get('coins', 0)}\n"
            f"📥 دانلود کل: {u.get('downloads', 0)}\n"
            f"📥 دانلود امروز: {today_data.get('count', 0)}\n"
            f"💾 حجم امروز: {today_data.get('size_bytes', 0) / (1024*1024):.2f} MB\n"
            f"🚫 بن: {'بله' if u.get('banned') else 'خیر'}"
        )
        bot.reply_to(message, text, parse_mode="Markdown")
    else:
        bot.reply_to(message, "❌ کاربر یافت نشد.")

def process_ban_user(message):
    target_id = message.text.strip()
    db = db_manager.execute(lambda db: db)
    if target_id in db:
        def operation(db2):
            db2[target_id]["banned"] = not db2[target_id].get("banned", False)
        db_manager.execute(operation)
        updated = db_manager.execute(lambda db2: db2[target_id]["banned"])
        status = "🚫 مسدود (Ban)" if updated else "✅ آزاد (Unban)"
        bot.reply_to(message, f"وضعیت کاربر `{target_id}` تغییر کرد:\n{status}", parse_mode="Markdown")
    else:
        bot.reply_to(message, "❌ کاربر یافت نشد.")

def process_broadcast(message):
    text_to_send = message.text
    db = db_manager.execute(lambda db: db)
    success = 0
    for user_id in list(db.keys()):
        if user_id == "file_caption":
            continue
        try:
            bot.send_message(user_id, f"📢 **پیام مدیریتی:**\n\n{text_to_send}", parse_mode="Markdown")
            success += 1
        except Exception:
            pass
        time.sleep(0.05)
    bot.reply_to(message, f"📊 **پایان ارسال همگانی**\n\n✅ موفق: `{success}`", parse_mode="Markdown")

# ----------------- FLASK WEBHOOK ROUTES -----------------
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    if request.content_type and request.content_type.startswith("application/json"):
        json_string = request.get_data(as_text=True)
        update = telebot.types.Update.de_json(json_string)
        threading.Thread(target=bot.process_new_updates, args=([update],), daemon=True).start()
        return "", 200
    abort(403)

@app.route("/", methods=["GET"])
def health_check():
    return "OK", 200

# ----------------- MAIN EXECUTION -----------------
if __name__ == "__main__":
    logger.info("Running locally with Flask development server...")
    app.run(host="0.0.0.0", port=PORT)

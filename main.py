from flask import Flask
import telebot
import os
import threading

TOKEN = os.getenv("TOKEN")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# وقتی /start بزنن
@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "سلام داداش 😎 ربات کار میکنه")

# ران کردن بات
def run_bot():
    bot.infinity_polling()

threading.Thread(target=run_bot).start()

# برای Render
@app.route('/')
def home():
    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)

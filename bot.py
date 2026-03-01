import os
import logging
import sqlite3
import threading
import pytz
from datetime import datetime

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_FILE = "reminders.db"
TOKEN = os.environ.get("TOKEN")

TIMEZONE_MAP = {
    'New York': 'America/New_York',
    'Chicago': 'America/Chicago',
    'Denver': 'America/Denver',
    'Los Angeles': 'America/Los_Angeles',
    'London': 'Europe/London',
    'Paris': 'Europe/Paris',
    'Berlin': 'Europe/Berlin',
    'Moscow': 'Europe/Moscow',
    'Tokyo': 'Asia/Tokyo',
    'Shanghai': 'Asia/Shanghai',
    'Dubai': 'Asia/Dubai',
    'Singapore': 'Asia/Singapore',
    'Sydney': 'Australia/Sydney',
    'Auckland': 'Pacific/Auckland',
    'UTC': 'UTC'
}

TIMEZONE_DISPLAY = [
    ['New York', 'Chicago', 'Denver', 'Los Angeles'],
    ['London', 'Paris', 'Berlin', 'Moscow'],
    ['Tokyo', 'Shanghai', 'Dubai', 'Singapore'],
    ['Sydney', 'Auckland', 'UTC']
]

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_name TEXT NOT NULL,
            reminder_time TEXT NOT NULL,
            reminder_time_utc TEXT,
            due_date TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            timezone TEXT DEFAULT 'UTC'
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

def get_user_timezone(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT timezone FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 'UTC'

def set_user_timezone(user_id, timezone):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO users (user_id, timezone) VALUES (?, ?)",
        (user_id, timezone)
    )
    conn.commit()
    conn.close()

def get_friendly_name(actual_tz):
    for name, actual in TIMEZONE_MAP.items():
        if actual == actual_tz:
            return name
    return actual_tz

def local_to_utc(time_str, timezone_str):
    try:
        tz = pytz.timezone(timezone_str)
        today = datetime.now().strftime('%Y-%m-%d')
        local_dt = datetime.strptime(f"{today} {time_str}", "%Y-%m-%d %H:%M")
        local_dt = tz.localize(local_dt)
        utc_dt = local_dt.astimezone(pytz.UTC)
        return utc_dt.strftime("%H:%M")
    except:
        return time_str

def utc_to_local(utc_time_str, timezone_str):
    try:
        utc = pytz.UTC
        tz = pytz.timezone(timezone_str)
        today = datetime.now(utc).strftime('%Y-%m-%d')
        utc_dt = datetime.strptime(f"{today} {utc_time_str}", "%Y-%m-%d %H:%M")
        utc_dt = utc.localize(utc_dt)
        local_dt = utc_dt.astimezone(tz)
        return local_dt.strftime("%H:%M")
    except:
        return utc_time_str

def get_local_time(timezone_str):
    tz = pytz.timezone(timezone_str)
    return datetime.now(tz)

def get_tasks(user_id):
    timezone_str = get_user_timezone(user_id)
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, task_name, reminder_time, reminder_time_utc, due_date FROM tasks WHERE user_id = ? AND is_active = 1",
        (user_id,)
    )
    rows = cursor.fetchall()
    conn.close()
    
    tasks = []
    for row in rows:
        local_time = utc_to_local(row[3] or row[2], timezone_str)
        tasks.append({
            'id': row[0],
            'name': row[1],
            'time': local_time,
            'due_date': row[4]
        })
    return tasks

def add_task_db(user_id, name, time_str):
    timezone_str = get_user_timezone(user_id)
    utc_time = local_to_utc(time_str, timezone_str)
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (user_id, task_name, reminder_time, reminder_time_utc) VALUES (?, ?, ?, ?)",
        (user_id, name, time_str, utc_time)
    )
    conn.commit()
    conn.close()

def delete_task_db(task_id, user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
    conn.commit()
    conn.close()

(NAME, TIME) = range(2)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tz = get_user_timezone(user.id)
    friendly = get_friendly_name(tz)
    local_time = get_local_time(tz)
    
    await update.message.reply_text(
        f"👋 Hello {user.first_name}!\n\n"
        f"🕐 Your time: {local_time.strftime('%H:%M')} ({friendly})\n\n"
        f"Commands:\n"
        f"📝 /add - Add task\n"
        f"🌍 /timezone - Change timezone\n"
        f"📋 /list - View tasks\n"
        f"❌ /delete - Remove task"
    )

async def timezone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(tz, callback_data=f"tz_{tz}") for tz in row] for row in TIMEZONE_DISPLAY]
    await update.message.reply_text("🌍 Select your city:", reply_markup=InlineKeyboardMarkup(keyboard))

async def timezone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    friendly_name = query.data.replace("tz_", "")
    actual_tz = TIMEZONE_MAP.get(friendly_name, 'UTC')
    user_id = update.effective_user.id
    
    set_user_timezone(user_id, actual_tz)
    local_time = get_local_time(actual_tz)
    
    await query.edit_message_text(f"✅ Timezone: {friendly_name}\n🕐 Your time: {local_time.strftime('%H:%M %p')}")

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("What should I remind you about?")
    return NAME

async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['name'] = update.message.text
    await update.message.reply_text("What time? (HH:MM, your local time)")
    return TIME

async def add_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_str = update.message.text
    
    try:
        datetime.strptime(time_str, "%H:%M")
    except:
        await update.message.reply_text("❌ Use format HH:MM")
        return TIME
    
    user_id = update.effective_user.id
    name = context.user_data['name']
    
    add_task_db(user_id, name, time_str)
    
    await update.message.reply_text(f"✅ Added: {name} at {time_str}")
    return ConversationHandler.END

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = get_tasks(user_id)
    
    if not tasks:
        await update.message.reply_text("No tasks! Add one with /add")
        return
    
    msg = "📋 Your Tasks:\n\n"
    for task in tasks:
        msg += f"• {task['name']} at {task['time']}\n"
    
    await update.message.reply_text(msg)

async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = get_tasks(user_id)
    
    if not tasks:
        await update.message.reply_text("No tasks to delete!")
        return
    
    keyboard = []
    for task in tasks:
        keyboard.append([InlineKeyboardButton(f"🗑️ {task['name']}", callback_data=f"del_{task['id']}")])
    
    await update.message.reply_text("Delete which?", reply_markup=InlineKeyboardMarkup(keyboard))

async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    task_id = int(query.data.split('_')[1])
    user_id = update.effective_user.id
    
    delete_task_db(task_id, user_id)
    await query.edit_message_text("🗑️ Deleted!")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Cancelled")
    return ConversationHandler.END

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow()
    current_time = now.strftime("%H:%M")
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT t.id, t.user_id, t.task_name, u.timezone FROM tasks t "
        "JOIN users u ON t.user_id = u.user_id "
        "WHERE t.reminder_time_utc = ? AND t.is_active = 1",
        (current_time,)
    )
    tasks = cursor.fetchall()
    conn.close()
    
    for task in tasks:
        try:
            local_time = utc_to_local(current_time, task[3])
            await context.bot.send_message(
                chat_id=task[1],
                text=f"🔔 Reminder: {task[2]}\n(Your time: {local_time})"
            )
        except Exception as e:
            logger.error(f"Failed: {e}")

app = Flask(__name__)

@app.route('/')
def home():
    return "<h1>Mula Bot</h1>"

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))

def main():
    if not TOKEN:
        logger.error("No TOKEN!")
        return
    
    application = Application.builder().token(TOKEN).build()
    
    add_conv = ConversationHandler(
        entry_points=[CommandHandler('add', add_start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_time)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('timezone', timezone_cmd))
    application.add_handler(CallbackQueryHandler(timezone_callback, pattern='^tz_'))
    application.add_handler(CommandHandler('list', list_tasks))
    application.add_handler(CommandHandler('delete', delete_start))
    application.add_handler(add_conv)
    application.add_handler(CallbackQueryHandler(delete_callback, pattern='^del_'))
    
    application.job_queue.run_repeating(check_reminders, interval=60, first=10)
    
    logger.info("Bot started!")
    application.run_polling()

if __name__ == '__main__':
    threading.Thread(target=run_flask).start()
    main()
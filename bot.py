import os
import logging
import sqlite3
import threading
from datetime import datetime, timedelta

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

# Database setup
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_name TEXT NOT NULL,
            reminder_time TEXT NOT NULL,
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

# Helper functions
def get_tasks(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, task_name, reminder_time, due_date FROM tasks WHERE user_id = ? AND is_active = 1",
        (user_id,)
    )
    tasks = cursor.fetchall()
    conn.close()
    return tasks

def add_task_db(user_id, name, time, due_date=None):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (user_id, task_name, reminder_time, due_date) VALUES (?, ?, ?, ?)",
        (user_id, name, time, due_date)
    )
    conn.commit()
    conn.close()

def delete_task_db(task_id, user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
    conn.commit()
    conn.close()

# States
(NAME, TIME) = range(2)

# Command handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"👋 Hello {user.first_name}!\n\n"
        "📝 /add - Add new task\n"
        "📋 /list - View tasks\n"
        "❌ /delete - Remove task\n"
        "❓ /help - Show commands"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Commands:\n"
        "/add - Create reminder\n"
        "/list - See all tasks\n"
        "/delete - Delete task"
    )

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("What should I remind you about?")
    return NAME

async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['name'] = update.message.text
    await update.message.reply_text("What time? (HH:MM, 24-hour format)")
    return TIME

async def add_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    time_str = update.message.text
    
    # Simple validation
    try:
        datetime.strptime(time_str, "%H:%M")
    except:
        await update.message.reply_text("❌ Use format HH:MM (like 09:00 or 14:30)")
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
        msg += f"• {task[1]} at {task[2]}\n"
    
    await update.message.reply_text(msg)

async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = get_tasks(user_id)
    
    if not tasks:
        await update.message.reply_text("No tasks to delete!")
        return
    
    keyboard = []
    for task in tasks:
        keyboard.append([InlineKeyboardButton(
            f"🗑️ {task[1]}", 
            callback_data=f"del_{task[0]}"
        )])
    
    await update.message.reply_text(
        "Delete which task?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

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

# Reminder job
async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    current_time = now.strftime("%H:%M")
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT t.id, t.user_id, t.task_name FROM tasks t "
        "WHERE t.reminder_time = ? AND t.is_active = 1",
        (current_time,)
    )
    tasks = cursor.fetchall()
    conn.close()
    
    for task in tasks:
        try:
            await context.bot.send_message(
                chat_id=task[1],
                text=f"🔔 Reminder: {task[2]}"
            )
        except Exception as e:
            logger.error(f"Failed to send: {e}")

# Flask app
app = Flask(__name__)

@app.route('/')
def home():
    return "<h1>Mula Bot is running!</h1>"

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))

# Main
def main():
    if not TOKEN:
        logger.error("No TOKEN found!")
        return
    
    application = Application.builder().token(TOKEN).build()
    
    # Add conversation handler
    add_conv = ConversationHandler(
        entry_points=[CommandHandler('add', add_start)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_time)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    # Add handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_cmd))
    application.add_handler(CommandHandler('list', list_tasks))
    application.add_handler(CommandHandler('delete', delete_start))
    application.add_handler(add_conv)
    application.add_handler(CallbackQueryHandler(delete_callback, pattern='^del_'))
    
    # Schedule reminders
    application.job_queue.run_repeating(check_reminders, interval=60, first=10)
    
    logger.info("Bot started!")
    application.run_polling()

if __name__ == '__main__':
    threading.Thread(target=run_flask).start()
    main()
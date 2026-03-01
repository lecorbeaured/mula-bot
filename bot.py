import os
import logging
import sqlite3
import threading
import pytz
import re
from datetime import datetime, timedelta

import dateparser
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
            is_recurring INTEGER DEFAULT 0,
            frequency TEXT DEFAULT 'once',
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

def normalize_time(time_str):
    """Fix common time format issues like 245pm -> 2:45pm"""
    # Fix 245pm -> 2:45pm
    time_str = re.sub(r'(\d{1,2})(\d{2})(am|pm)', r'\1:\2\3', time_str, flags=re.IGNORECASE)
    return time_str

def parse_natural_date(text, timezone_str):
    """Improved parser with better date/time extraction"""
    text_lower = text.lower()
    now = datetime.now(pytz.timezone(timezone_str))
    
    target_date = None
    target_time = "09:00"
    is_recurring = False
    
    # Normalize time format first
    text_normalized = normalize_time(text)
    
    # Check for recurring
    if any(word in text_lower for word in ['every', 'daily', 'each', 'weekly', 'monthly']):
        is_recurring = True
    
    # Try dateparser first on full normalized text
    settings = {
        'TIMEZONE': timezone_str,
        'RETURN_AS_TIMEZONE_AWARE': True,
        'PREFER_DATES_FROM': 'future',
        'RELATIVE_BASE': now
    }
    
    parsed = dateparser.parse(text_normalized, settings=settings)
    
    if parsed:
        # Check if it's actually in the future
        if parsed > now:
            return {
                'datetime': parsed,
                'date': parsed.strftime('%Y-%m-%d'),
                'time': parsed.strftime('%H:%M'),
                'is_recurring': is_recurring
            }
        # If parsed date is in past but has year specified, it might be next year
        # Let it through and we'll check later
    
    # Manual parsing for common patterns
    if 'tomorrow' in text_lower:
        target_date = now + timedelta(days=1)
    elif 'today' in text_lower:
        target_date = now
    elif 'next week' in text_lower:
        target_date = now + timedelta(weeks=1)
    elif 'in 2 days' in text_lower or 'in two days' in text_lower:
        target_date = now + timedelta(days=2)
    elif 'in 3 days' in text_lower:
        target_date = now + timedelta(days=3)
    elif 'next monday' in text_lower:
        days_ahead = 0 - now.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        target_date = now + timedelta(days=days_ahead)
    elif 'next tuesday' in text_lower:
        days_ahead = 1 - now.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        target_date = now + timedelta(days=days_ahead)
    elif 'next wednesday' in text_lower:
        days_ahead = 2 - now.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        target_date = now + timedelta(days=days_ahead)
    elif 'next thursday' in text_lower:
        days_ahead = 3 - now.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        target_date = now + timedelta(days=days_ahead)
    elif 'next friday' in text_lower:
        days_ahead = 4 - now.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        target_date = now + timedelta(days=days_ahead)
    
    # If we have a target date from manual parsing, extract time
    if target_date:
        # Extract time with improved regex
        time_patterns = [
            r'(\d{1,2}):(\d{2})\s*(am|pm)',
            r'(\d{1,2})\s*(am|pm)',
            r'at\s+(\d{1,2}):(\d{2})',
            r'at\s+(\d{1,2})\s*(am|pm)'
        ]
        
        for pattern in time_patterns:
            match = re.search(pattern, text_lower)
            if match:
                groups = match.groups()
                hour = int(groups[0])
                minute = int(groups[1]) if len(groups) > 1 and groups[1] and groups[1].isdigit() else 0
                
                ampm = None
                for g in groups:
                    if g in ['am', 'pm']:
                        ampm = g
                        break
                
                if ampm == 'pm' and hour != 12:
                    hour += 12
                elif ampm == 'am' and hour == 12:
                    hour = 0
                
                target_time = f"{hour:02d}:{minute:02d}"
                break
        
        return {
            'datetime': target_date,
            'date': target_date.strftime('%Y-%m-%d'),
            'time': target_time,
            'is_recurring': is_recurring
        }
    
    # If dateparser worked but date is in past, try adding a year
    if parsed:
        # If the parsed date is in the past, assume next year
        if parsed <= now:
            try:
                # Try to parse with explicit year handling
                settings['PREFER_DATES_FROM'] = 'future'
                settings['STRICT_PARSING'] = False
                parsed = dateparser.parse(text_normalized + " next year", settings=settings)
                if not parsed or parsed <= now:
                    # Just add one year manually
                    parsed = parsed.replace(year=parsed.year + 1) if parsed else None
            except:
                pass
        
        if parsed and parsed > now:
            return {
                'datetime': parsed,
                'date': parsed.strftime('%Y-%m-%d'),
                'time': parsed.strftime('%H:%M'),
                'is_recurring': is_recurring
            }
    
    return None

def extract_task_name(text):
    """Remove date/time parts to get clean task name"""
    patterns = [
        r'tomorrow',
        r'today',
        r'next (monday|tuesday|wednesday|thursday|friday|saturday|sunday|week)',
        r'every (day|week|month|monday|tuesday|wednesday|thursday|friday)',
        r'in \d+ days?',
        r'at \d{1,2}:\d{2}\s*(?:am|pm)?',
        r'at \d{1,2}\s*(?:am|pm)',
        r'\d{1,2}:\d{2}\s*(?:am|pm)',
        r'\d{1,2}\d{2}\s*(?:am|pm)',  # 245pm
        r'on \w+ \d{1,2}(?:st|nd|rd|th)?,? \d{4}',
        r'(?:january|february|march|april|may|june|july|august|september|october|november|december) \d{1,2}(?:,?\s*\d{4})?',
        r'(?:daily|weekly|monthly)',
    ]
    
    name = text
    for pattern in patterns:
        name = re.sub(pattern, '', name, flags=re.IGNORECASE)
    
    name = re.sub(r'\s+', ' ', name).strip()
    name = re.sub(r'^(?:remind me to|remind me|to)\s*', '', name, flags=re.IGNORECASE)
    
    return name if name else "Task"

def local_to_utc(time_str, date_str, timezone_str):
    try:
        tz = pytz.timezone(timezone_str)
        local_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
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
    today = datetime.now().strftime('%Y-%m-%d')
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        """SELECT id, task_name, reminder_time, reminder_time_utc, due_date, 
           is_recurring, frequency FROM tasks 
           WHERE user_id = ? AND is_active = 1
           AND (due_date IS NULL OR due_date >= ? OR is_recurring = 1)""",
        (user_id, today)
    )
    rows = cursor.fetchall()
    conn.close()
    
    tasks = []
    for row in rows:
        local_time = utc_to_local(row[3] or row[2], timezone_str)
        due = row[4]
        days_until = None
        
        if due:
            date_obj = datetime.strptime(due, '%Y-%m-%d')
            days_until = (date_obj - datetime.now()).days
        
        tasks.append({
            'id': row[0],
            'name': row[1],
            'time': local_time,
            'due_date': due,
            'is_recurring': row[5],
            'frequency': row[6],
            'days_until': days_until
        })
    return tasks

def add_task_db(user_id, name, time_str, date_str=None, is_recurring=False, frequency='once'):
    timezone_str = get_user_timezone(user_id)
    utc_time = local_to_utc(time_str, date_str or datetime.now().strftime('%Y-%m-%d'), timezone_str)
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO tasks 
           (user_id, task_name, reminder_time, reminder_time_utc, due_date, is_recurring, frequency) 
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, name, time_str, utc_time, date_str, 1 if is_recurring else 0, frequency)
    )
    conn.commit()
    conn.close()

def delete_task_db(task_id, user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
    conn.commit()
    conn.close()

(NATURAL_INPUT, CONFIRM) = range(2)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tz = get_user_timezone(user.id)
    friendly = get_friendly_name(tz)
    local_time = get_local_time(tz)
    
    await update.message.reply_text(
        f"👋 Hello {user.first_name}!\n\n"
        f"🕐 Your time: {local_time.strftime('%H:%M')} ({friendly})\n\n"
        f"📝 /add - Add task\n"
        f"   Examples:\n"
        f"   • 'Call John tomorrow at 3pm'\n"
        f"   • 'Meeting every Friday at 10am'\n"
        f"   • 'Pay rent March 31 at 2:45pm'\n\n"
        f"🌍 /timezone - Change city\n"
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

async def add_smart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 What should I remind you about?\n\n"
        "Examples:\n"
        "• Call John tomorrow at 3pm\n"
        "• Meeting every Friday at 10am\n"
        "• Pay rent March 31 at 2:45pm\n"
        "• Doctor appointment in 2 days at 2pm"
    )
    return NATURAL_INPUT

async def process_natural_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text
    user_id = update.effective_user.id
    timezone_str = get_user_timezone(user_id)
    
    # Normalize input
    user_input_normalized = normalize_time(user_input)
    
    parsed = parse_natural_date(user_input_normalized, timezone_str)
    
    if not parsed:
        await update.message.reply_text(
            "❌ Couldn't understand. Try:\n"
            "• tomorrow at 3pm\n"
            "• March 31 at 2:45pm\n"
            "• next Monday at 10am"
        )
        return NATURAL_INPUT
    
    user_tz = pytz.timezone(timezone_str)
    now = datetime.now(user_tz)
    parsed_dt = parsed['datetime']
    
    # Check if date is in past (with 1 minute buffer)
    if parsed_dt < now - timedelta(minutes=1):
        # If it's a specific date without year, maybe they meant next year
        days_diff = (now - parsed_dt).days
        if days_diff > 365:
            await update.message.reply_text(
                f"❌ That date ({parsed['date']}) is in the past!\n"
                f"Did you mean March 31, {now.year + 1}?"
            )
        else:
            await update.message.reply_text(
                f"❌ That time ({parsed['date']} {parsed['time']}) is in the past!\n"
                f"Try a future date/time."
            )
        return NATURAL_INPUT
    
    task_name = extract_task_name(user_input)
    
    if not task_name or task_name == user_input:
        await update.message.reply_text("What's the task? (e.g., 'Pay rent')")
        context.user_data['parsed'] = parsed
        context.user_data['awaiting_name'] = True
        return NATURAL_INPUT
    
    context.user_data['task_name'] = task_name
    context.user_data['parsed'] = parsed
    
    date_obj = datetime.strptime(parsed['date'], '%Y-%m-%d')
    today = datetime.now().strftime('%Y-%m-%d')
    
    if parsed['date'] == today:
        date_display = "Today"
    elif parsed['date'] == (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%d'):
        date_display = "Tomorrow"
    else:
        days_until = (date_obj - datetime.now()).days
        date_display = f"{parsed['date']} (in {days_until} days)"
    
    recurring_text = "🔁 Recurring" if parsed['is_recurring'] else "☑️ One-time"
    
    await update.message.reply_text(
        f"📝 Task: {task_name}\n"
        f"📅 Date: {date_display}\n"
        f"⏰ Time: {parsed['time']}\n"
        f"🔄 Type: {recurring_text}\n\n"
        f"Correct?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, add it", callback_data='confirm_add')],
            [InlineKeyboardButton("❌ Cancel", callback_data='cancel')]
        ])
    )
    return CONFIRM

async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle both Yes and Cancel buttons"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    
    if query.data == 'cancel':
        await query.edit_message_text("❌ Cancelled")
        context.user_data.clear()
        return ConversationHandler.END
    
    # Handle confirm_add
    data = context.user_data
    
    if not data.get('parsed'):
        await query.edit_message_text("❌ Error: No task data found")
        return ConversationHandler.END
    
    task_name = data.get('task_name', 'Task')
    parsed = data['parsed']
    
    frequency = 'once'
    if parsed['is_recurring']:
        text_lower = task_name.lower()
        if 'week' in text_lower:
            frequency = 'weekly'
        elif 'month' in text_lower:
            frequency = 'monthly'
        else:
            frequency = 'daily'
    
    add_task_db(
        user_id=user_id,
        name=task_name,
        time_str=parsed['time'],
        date_str=parsed['date'],
        is_recurring=parsed['is_recurring'],
        frequency=frequency
    )
    
    when = "starting " + parsed['date'] if parsed['is_recurring'] else "on " + parsed['date']
    
    await query.edit_message_text(
        f"✅ Added!\n\n"
        f"📝 {task_name}\n"
        f"⏰ {parsed['time']} {when}\n"
        f"{'🔁 ' + frequency if parsed['is_recurring'] else '☑️ One-time'}"
    )
    
    context.user_data.clear()
    return ConversationHandler.END

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = get_tasks(user_id)
    
    if not tasks:
        await update.message.reply_text("No tasks! Add one with /add")
        return
    
    msg = "📋 Your Tasks:\n\n"
    for task in tasks:
        emoji = "🔁" if task['is_recurring'] else "☑️"
        
        date_info = ""
        if task['due_date'] and not task['is_recurring']:
            if task.get('days_until') == 0:
                date_info = " TODAY"
            elif task.get('days_until') == 1:
                date_info = " tomorrow"
            elif task.get('days_until') and task['days_until'] > 1:
                date_info = f" ({task['days_until']} days)"
        
        msg += f"{emoji}{date_info} {task['name']} at {task['time']}\n"
    
    await update.message.reply_text(msg)

async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = get_tasks(user_id)
    
    if not tasks:
        await update.message.reply_text("No tasks to delete!")
        return
    
    keyboard = []
    for task in tasks:
        emoji = "🔁" if task['is_recurring'] else "☑️"
        keyboard.append([InlineKeyboardButton(f"{emoji} {task['name']}", callback_data=f"del_{task['id']}")])
    
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
    today = now.strftime("%Y-%m-%d")
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        """SELECT t.id, t.user_id, t.task_name, u.timezone, t.due_date, t.is_recurring 
           FROM tasks t 
           JOIN users u ON t.user_id = u.user_id 
           WHERE t.reminder_time_utc = ? 
           AND t.is_active = 1
           AND (t.is_recurring = 1 OR t.due_date = ?)""",
        (current_time, today)
    )
    tasks = cursor.fetchall()
    conn.close()
    
    for task in tasks:
        try:
            local_time = utc_to_local(current_time, task[3])
            recurring_note = "🔁 " if task[5] else ""
            
            await context.bot.send_message(
                chat_id=task[1],
                text=f"🔔 {recurring_note}Reminder\n\n{task[2]}\nYour time: {local_time}"
            )
        except Exception as e:
            logger.error(f"Failed: {e}")

app = Flask(__name__)

@app.route('/')
def home():
    return "<h1>Mula Bot - Fixed Parser</h1>"

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))

def main():
    if not TOKEN:
        logger.error("No TOKEN!")
        return
    
    application = Application.builder().token(TOKEN).build()
    
    add_conv = ConversationHandler(
        entry_points=[CommandHandler('add', add_smart)],
        states={
            NATURAL_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_natural_input)],
            CONFIRM: [CallbackQueryHandler(confirm_callback)],
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
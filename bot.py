import os
import asyncio
import logging
import sqlite3
import threading
from datetime import datetime, time
from typing import List, Optional

from flask import Flask, render_template_string
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

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database setup
DB_FILE = "reminders.db"

def init_db():
    """Initialize SQLite database"""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_name TEXT NOT NULL,
            reminder_time TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            timezone TEXT DEFAULT 'UTC',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()

class Database:
    @staticmethod
    def add_task(user_id: int, task_name: str, reminder_time: str) -> int:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO tasks (user_id, task_name, reminder_time) VALUES (?, ?, ?)",
            (user_id, task_name, reminder_time)
        )
        task_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return task_id
    
    @staticmethod
    def get_user_tasks(user_id: int) -> List[dict]:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, task_name, reminder_time, is_active FROM tasks WHERE user_id = ? ORDER BY reminder_time",
            (user_id,)
        )
        tasks = [
            {
                "id": row[0],
                "task_name": row[1],
                "reminder_time": row[2],
                "is_active": bool(row[3])
            }
            for row in cursor.fetchall()
        ]
        conn.close()
        return tasks
    
    @staticmethod
    def delete_task(task_id: int, user_id: int) -> bool:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id)
        )
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted
    
    @staticmethod
    def toggle_task(task_id: int, user_id: int) -> bool:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tasks SET is_active = NOT is_active WHERE id = ? AND user_id = ?",
            (task_id, user_id)
        )
        updated = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return updated
    
    @staticmethod
    def get_all_active_tasks() -> List[dict]:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT t.id, t.user_id, t.task_name, t.reminder_time, u.username "
            "FROM tasks t JOIN users u ON t.user_id = u.user_id "
            "WHERE t.is_active = 1"
        )
        tasks = [
            {
                "id": row[0],
                "user_id": row[1],
                "task_name": row[2],
                "reminder_time": row[3],
                "username": row[4]
            }
            for row in cursor.fetchall()
        ]
        conn.close()
        return tasks

# Conversation states
(ADD_TASK_NAME, ADD_TASK_TIME) = range(2)

# Command Handlers
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message and register user"""
    user = update.effective_user
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)",
        (user.id, user.username)
    )
    conn.commit()
    conn.close()
    
    welcome_text = (
        f"👋 Hello {user.first_name}!\n\n"
        "I'm your Daily Task Reminder Bot. Here's what I can do:\n\n"
        "📝 /addtask - Add a new daily task\n"
        "📋 /listtasks - View all your tasks\n"
        "❌ /deletetask - Remove a task\n"
        "⏸️ /toggletask - Enable/disable a task\n"
        "🧪 /test - Test notification settings\n"
        "❓ /help - Show help message\n\n"
        "I'll automatically remind you of your tasks every day!"
    )
    
    await update.message.reply_text(welcome_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help message"""
    help_text = (
        "🔔 *Daily Reminder Bot Help*\n\n"
        "*Commands:*\n"
        "/addtask - Add a new task with time\n"
        "/listtasks - List all your tasks\n"
        "/deletetask - Delete a specific task\n"
        "/toggletask - Toggle task on/off\n"
        "/test - Test your notification settings\n"
        "/help - Show help message\n\n"
        "*Time Format:*\n"
        "Use 24-hour format: `HH:MM`\n"
        "Examples: `09:00`, `14:30`, `20:00`\n\n"
        "*How it works:*\n"
        "I check for tasks every minute and send reminders at the scheduled time!"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def test_notification(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send test notification to verify settings"""
    await update.message.reply_text(
        "🔔 *TEST NOTIFICATION*\n\n"
        "Did you hear/see this?\n\n"
        "If not, check:\n"
        "• Phone volume is up\n"
        "• Telegram notifications are ON\n"
        "• Do Not Disturb is OFF\n\n"
        "💡 Pro Tip: Upgrade to Pro for SMS backup notifications!",
        parse_mode='Markdown'
    )

# Add Task Conversation
async def add_task_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start adding a task"""
    await update.message.reply_text(
        "📝 Let's add a new task!\n\n"
        "What task should I remind you about?\n"
        "(e.g., 'Drink water', 'Team meeting', 'Workout')",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data='cancel')
        ]])
    )
    return ADD_TASK_NAME

async def add_task_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save task name and ask for time"""
    context.user_data['task_name'] = update.message.text
    
    await update.message.reply_text(
        f"✅ Task: *{update.message.text}*\n\n"
        "What time should I remind you daily?\n"
        "Please send time in 24-hour format (e.g., `09:00`, `14:30`)",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data='cancel')
        ]])
    )
    return ADD_TASK_TIME

async def add_task_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Save task time and complete"""
    time_str = update.message.text
    
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        await update.message.reply_text(
            "❌ Invalid time format!\n"
            "Please use 24-hour format like `09:00` or `14:30`",
            parse_mode='Markdown'
        )
        return ADD_TASK_TIME
    
    task_name = context.user_data['task_name']
    user_id = update.effective_user.id
    
    task_id = Database.add_task(user_id, task_name, time_str)
    
    await update.message.reply_text(
        f"✅ *Task Added!*\n\n"
        f"📝 Task: {task_name}\n"
        f"⏰ Time: {time_str}\n\n"
        f"I'll remind you every day at {time_str}!",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📋 View All Tasks", callback_data='list_tasks')
        ]])
    )
    
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel conversation"""
    await update.message.reply_text("❌ Operation cancelled.")
    return ConversationHandler.END

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel from button"""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Operation cancelled.")
    return ConversationHandler.END

# List Tasks
async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all user tasks"""
    user_id = update.effective_user.id
    tasks = Database.get_user_tasks(user_id)
    
    if not tasks:
        await update.message.reply_text(
            "📭 You don't have any tasks yet!\n"
            "Use /addtask to create your first reminder."
        )
        return
    
    message = "📋 *Your Daily Tasks:*\n\n"
    keyboard = []
    
    for task in tasks:
        status = "🟢" if task['is_active'] else "🔴"
        message += f"{status} `{task['reminder_time']}` - {task['task_name']}\n"
        
        action = "⏸️ Pause" if task['is_active'] else "▶️ Resume"
        keyboard.append([
            InlineKeyboardButton(
                f"{action} {task['task_name'][:20]}", 
                callback_data=f"toggle_{task['id']}"
            ),
            InlineKeyboardButton(
                "🗑️ Delete", 
                callback_data=f"delete_{task['id']}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton("➕ Add New Task", callback_data='add_task')])
    
    await update.message.reply_text(
        message,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def list_tasks_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List tasks from callback"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    tasks = Database.get_user_tasks(user_id)
    
    if not tasks:
        await query.edit_message_text(
            "📭 You don't have any tasks yet!\n"
            "Use /addtask to create your first reminder."
        )
        return
    
    message = "📋 *Your Daily Tasks:*\n\n"
    keyboard = []
    
    for task in tasks:
        status = "🟢" if task['is_active'] else "🔴"
        message += f"{status} `{task['reminder_time']}` - {task['task_name']}\n"
        
        action = "⏸️ Pause" if task['is_active'] else "▶️ Resume"
        keyboard.append([
            InlineKeyboardButton(
                f"{action} {task['task_name'][:20]}", 
                callback_data=f"toggle_{task['id']}"
            ),
            InlineKeyboardButton(
                "🗑️ Delete", 
                callback_data=f"delete_{task['id']}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton("➕ Add New Task", callback_data='add_task')])
    
    await query.edit_message_text(
        message,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# Toggle Task
async def toggle_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show toggle menu"""
    user_id = update.effective_user.id
    tasks = Database.get_user_tasks(user_id)
    
    if not tasks:
        await update.message.reply_text("You have no tasks to toggle!")
        return
    
    keyboard = []
    for task in tasks:
        status = "Active" if task['is_active'] else "Paused"
        emoji = "🟢" if task['is_active'] else "🔴"
        keyboard.append([
            InlineKeyboardButton(
                f"{emoji} {task['task_name']} ({status})", 
                callback_data=f"toggle_{task['id']}"
            )
        ])
    
    await update.message.reply_text(
        "Select a task to toggle:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def toggle_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Toggle task status"""
    query = update.callback_query
    await query.answer()
    
    task_id = int(query.data.split('_')[1])
    user_id = update.effective_user.id
    
    if Database.toggle_task(task_id, user_id):
        await query.edit_message_text("✅ Task status updated!")
        await list_tasks_callback(update, context)
    else:
        await query.edit_message_text("❌ Task not found!")

# Delete Task
async def delete_task_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show delete menu"""
    user_id = update.effective_user.id
    tasks = Database.get_user_tasks(user_id)
    
    if not tasks:
        await update.message.reply_text("You have no tasks to delete!")
        return
    
    keyboard = []
    for task in tasks:
        keyboard.append([
            InlineKeyboardButton(
                f"🗑️ {task['task_name']} ({task['reminder_time']})", 
                callback_data=f"delete_{task['id']}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data='cancel_delete')])
    
    await update.message.reply_text(
        "⚠️ Select a task to delete:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def delete_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete task"""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'cancel_delete':
        await query.edit_message_text("❌ Deletion cancelled.")
        return
    
    task_id = int(query.data.split('_')[1])
    user_id = update.effective_user.id
    
    if Database.delete_task(task_id, user_id):
        await query.edit_message_text("🗑️ Task deleted successfully!")
    else:
        await query.edit_message_text("❌ Task not found!")

# Reminder Job
async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Background job to check and send reminders"""
    now = datetime.now()
    current_time = now.strftime("%H:%M")
    
    tasks = Database.get_all_active_tasks()
    
    for task in tasks:
        if task['reminder_time'] == current_time:
            try:
                await context.bot.send_message(
                    chat_id=task['user_id'],
                    text=(
                        f"🔔 *Reminder!*\n\n"
                        f"⏰ It's time for:\n"
                        f"📝 *{task['task_name']}*\n\n"
                        f"Have a great day! 🌟"
                    ),
                    parse_mode='Markdown'
                )
                logger.info(f"Sent reminder to {task['user_id']} for {task['task_name']}")
            except Exception as e:
                logger.error(f"Failed to send reminder: {e}")

def main():
    """Start the bot"""
    init_db()
    
    TOKEN = os.environ.get("TOKEN")
    if not TOKEN:
        logger.error("No TOKEN found! Set TOKEN environment variable.")
        return
    
    application = Application.builder().token(TOKEN).build()
    
    add_task_conv = ConversationHandler(
        entry_points=[CommandHandler('addtask', add_task_start)],
        states={
            ADD_TASK_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_task_name),
                CallbackQueryHandler(cancel_callback, pattern='^cancel$')
            ],
            ADD_TASK_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_task_time),
                CallbackQueryHandler(cancel_callback, pattern='^cancel$')
            ],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', help_command))
    application.add_handler(CommandHandler('test', test_notification))
    application.add_handler(add_task_conv)
    application.add_handler(CommandHandler('listtasks', list_tasks))
    application.add_handler(CommandHandler('deletetask', delete_task_command))
    application.add_handler(CommandHandler('toggletask', toggle_task_command))
    
    application.add_handler(CallbackQueryHandler(list_tasks_callback, pattern='^list_tasks$'))
    application.add_handler(CallbackQueryHandler(toggle_task_callback, pattern='^toggle_'))
    application.add_handler(CallbackQueryHandler(delete_task_callback, pattern='^delete_'))
    application.add_handler(CallbackQueryHandler(add_task_start, pattern='^add_task$'))
    
    job_queue = application.job_queue
    job_queue.run_repeating(check_reminders, interval=60, first=10)
    
    logger.info("🤖 Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

# WEB DASHBOARD
app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Mula Bot Dashboard</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px; background: #f5f5f5; }
        h1 { color: #333; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; background: white; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #ddd; }
        th { background-color: #4CAF50; color: white; }
        .stats { background: white; padding: 20px; border-radius: 8px; margin: 20px 0; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .btn { background: #4CAF50; color: white; padding: 10px 20px; text-decoration: none; border-radius: 4px; display: inline-block; margin-top: 10px; }
        .container { background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
    </style>
</head>
<body>
    <div class="container">
        <h1>🔔 Mula Bot Dashboard</h1>
        
        <div class="stats">
            <h3>Your Stats</h3>
            <p><strong>Total Tasks:</strong> {{ total_tasks }}</p>
            <p><strong>Active Tasks:</strong> {{ active_tasks }}</p>
            <p><strong>Completion Rate:</strong> {{ completion_rate }}%</p>
        </div>
        
        <h2>Your Tasks</h2>
        <table>
            <tr>
                <th>Task</th>
                <th>Time</th>
                <th>Status</th>
                <th>Created</th>
            </tr>
            {% for task in tasks %}
            <tr>
                <td>{{ task.task_name }}</td>
                <td>{{ task.reminder_time }}</td>
                <td>{% if task.is_active %}🟢 Active{% else %}🔴 Paused{% endif %}</td>
                <td>{{ task.created_at }}</td>
            </tr>
            {% endfor %}
        </table>
        
        <a href="/export" class="btn">📥 Export to CSV</a>
    </div>
</body>
</html>
"""

@app.route('/')
def dashboard():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT task_name, reminder_time, is_active, created_at FROM tasks")
    tasks = [
        {
            "task_name": row[0],
            "reminder_time": row[1],
            "is_active": bool(row[2]),
            "created_at": row[3]
        }
        for row in cursor.fetchall()
    ]
    conn.close()
    
    total = len(tasks)
    active = sum(1 for t in tasks if t['is_active'])
    
    return render_template_string(HTML_TEMPLATE, 
                                  tasks=tasks, 
                                  total_tasks=total,
                                  active_tasks=active,
                                  completion_rate=85)

@app.route('/export')
def export_csv():
    import csv
    import io
    from flask import Response
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM tasks")
    data = cursor.fetchall()
    conn.close()
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['ID', 'User', 'Task', 'Time', 'Active', 'Created'])
    writer.writerows(data)
    
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=tasks.csv"}
    )

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()
    
    main()
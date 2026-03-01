import os
import asyncio
import logging
import sqlite3
import threading
import pytz
import re
from datetime import datetime, timedelta, date
from typing import List, Optional
from enum import Enum

import dateparser  # NEW: pip install dateparser
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

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_FILE = "reminders.db"

class ReminderType(Enum):
    RECURRING = "recurring"  # daily, weekly, monthly
    ONE_TIME = "one_time"    # specific date
    SMART = "smart"          # natural language parsed

BADGES = {
    'first_task': {'name': '🌱 Starter', 'desc': 'Created first task'},
    'streak_7': {'name': '🔥 Week Warrior', 'desc': '7-day streak'},
    'streak_30': {'name': '⚡ Monthly Master', 'desc': '30-day streak'},
    'future_planner': {'name': '🔮 Planner', 'desc': 'Scheduled task 30+ days ahead'},
    'date_parser': {'name': '🧠 Smart User', 'desc': 'Used natural language date'},
}

LEVELS = [
    (0, '🥉 Bronze', 1),
    (50, '🥈 Silver', 1.2),
    (150, '🥇 Gold', 1.5),
    (300, '💎 Diamond', 2.0),
    (500, '👑 Legend', 3.0),
]

TIMEZONES = [
    ['America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles'],
    ['Europe/London', 'Europe/Paris', 'Europe/Berlin', 'Europe/Moscow'],
    ['Asia/Tokyo', 'Asia/Shanghai', 'Asia/Dubai', 'Asia/Singapore'],
    ['Australia/Sydney', 'Pacific/Auckland', 'UTC', 'Africa/Lagos']
]

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Enhanced schema with specific dates
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_name TEXT NOT NULL,
            reminder_time TEXT NOT NULL,
            reminder_time_utc TEXT,
            
            -- NEW: Date-specific fields
            reminder_type TEXT DEFAULT 'recurring',
            specific_date DATE,
            specific_date_original TEXT,
            
            -- Recurring fields
            frequency TEXT DEFAULT 'daily',
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_completed TIMESTAMP,
            completion_count INTEGER DEFAULT 0,
            streak INTEGER DEFAULT 0,
            longest_streak INTEGER DEFAULT 0,
            next_reminder_date DATE,
            xp_earned INTEGER DEFAULT 0,
            category TEXT DEFAULT 'general'
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            timezone TEXT DEFAULT 'UTC',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total_xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1,
            title TEXT DEFAULT '🥉 Bronze',
            freeze_tokens INTEGER DEFAULT 0,
            is_pro INTEGER DEFAULT 0,
            pro_expires TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER,
            user_id INTEGER,
            completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            xp_earned INTEGER DEFAULT 10
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS badges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            badge_id TEXT,
            earned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, badge_id)
        )
    ''')
    
    conn.commit()
    conn.close()

def parse_natural_date(date_string: str, timezone_str: str = 'UTC') -> Optional[dict]:
    """
    Parse natural language dates like:
    - "tomorrow at 3pm"
    - "next Monday"
    - "June 12, 2026"
    - "in 3 days"
    - "Friday at 9am"
    """
    settings = {
        'TIMEZONE': timezone_str,
        'RETURN_AS_TIMEZONE_AWARE': True,
        'PREFER_DATES_FROM': 'future',
        'RELATIVE_BASE': datetime.now()
    }
    
    # Try to parse with dateparser
    parsed = dateparser.parse(date_string, settings=settings)
    
    if not parsed:
        return None
    
    # Check if it's date-only or datetime
    has_time = any(x in date_string.lower() for x in ['am', 'pm', ':', 'at', 'morning', 'evening', 'afternoon'])
    
    now = datetime.now(pytz.timezone(timezone_str))
    
    result = {
        'datetime': parsed,
        'date': parsed.strftime('%Y-%m-%d'),
        'time': parsed.strftime('%H:%M'),
        'is_past': parsed < now,
        'is_today': parsed.date() == now.date(),
        'days_until': (parsed.date() - now.date()).days,
        'has_time': has_time
    }
    
    return result

def get_user_timezone(user_id: int) -> str:
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT timezone FROM users WHERE user_id = ?", (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 'UTC'

def get_local_time(timezone_str: str) -> datetime:
    tz = pytz.timezone(timezone_str)
    return datetime.now(tz)

def convert_local_to_utc(dt: datetime, timezone_str: str) -> datetime:
    tz = pytz.timezone(timezone_str)
    local_dt = tz.localize(dt) if dt.tzinfo is None else dt
    return local_dt.astimezone(pytz.UTC)

def convert_utc_to_local(utc_time_str: str, timezone_str: str) -> str:
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

class Database:
    @staticmethod
    def add_task(user_id: int, task_name: str, reminder_time: str, 
                 frequency: str = 'daily', specific_date: str = None,
                 reminder_type: str = 'recurring', category: str = 'general') -> int:
        timezone_str = get_user_timezone(user_id)
        
        # Calculate UTC time
        if specific_date and reminder_time:
            local_dt = datetime.strptime(f"{specific_date} {reminder_time}", "%Y-%m-%d %H:%M")
            utc_dt = convert_local_to_utc(local_dt, timezone_str)
            utc_time = utc_dt.strftime("%H:%M")
            next_date = specific_date
        else:
            utc_time = reminder_time
            next_date = datetime.now().strftime('%Y-%m-%d')
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute(
            """INSERT INTO tasks 
               (user_id, task_name, reminder_time, reminder_time_utc, reminder_type,
                specific_date, specific_date_original, frequency, category, next_reminder_date) 
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, task_name, reminder_time, utc_time, reminder_type,
             specific_date, specific_date, frequency, category, next_date)
        )
        task_id = cursor.lastrowid
        
        # Check badges
        cursor.execute("SELECT COUNT(*) FROM tasks WHERE user_id = ?", (user_id,))
        if cursor.fetchone()[0] == 1:
            Database.award_badge(user_id, 'first_task')
        
        # Check future planner badge
        if specific_date:
            date_obj = datetime.strptime(specific_date, '%Y-%m-%d')
            days_ahead = (date_obj - datetime.now()).days
            if days_ahead > 30:
                Database.award_badge(user_id, 'future_planner')
        
        conn.commit()
        conn.close()
        return task_id
    
    @staticmethod
    def get_user(user_id: int) -> dict:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id, total_xp, level, title, freeze_tokens, is_pro, timezone FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                'user_id': row[0], 'total_xp': row[1] or 0, 'level': row[2] or 1,
                'title': row[3] or '🥉 Bronze', 'freeze_tokens': row[4] or 0,
                'is_pro': bool(row[5]), 'timezone': row[6] or 'UTC'
            }
        return None
    
    @staticmethod
    def set_timezone(user_id: int, timezone: str):
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO users (user_id, timezone) VALUES (?, ?)",
            (user_id, timezone)
        )
        conn.commit()
        conn.close()
        
        # Update existing tasks
        cursor.execute("SELECT id, reminder_time, specific_date FROM tasks WHERE user_id = ?", (user_id,))
        tasks = cursor.fetchall()
        for task_id, local_time, spec_date in tasks:
            if spec_date:
                local_dt = datetime.strptime(f"{spec_date} {local_time}", "%Y-%m-%d %H:%M")
                utc_dt = convert_local_to_utc(local_dt, timezone)
                new_utc = utc_dt.strftime("%H:%M")
            else:
                today = datetime.now().strftime('%Y-%m-%d')
                local_dt = datetime.strptime(f"{today} {local_time}", "%Y-%m-%d %H:%M")
                utc_dt = convert_local_to_utc(local_dt, timezone)
                new_utc = utc_dt.strftime("%H:%M")
            
            cursor.execute("UPDATE tasks SET reminder_time_utc = ? WHERE id = ?", (new_utc, task_id))
        
        conn.commit()
        conn.close()
        Database.award_badge(user_id, 'timezone_set')
    
    @staticmethod
    def update_user_xp(user_id: int, xp: int):
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET total_xp = total_xp + ? WHERE user_id = ?", (xp, user_id))
        cursor.execute("SELECT total_xp FROM users WHERE user_id = ?", (user_id,))
        total_xp = cursor.fetchone()[0]
        
        new_level = 1
        new_title = '🥉 Bronze'
        for threshold, title, mult in LEVELS:
            if total_xp >= threshold:
                new_level = LEVELS.index((threshold, title, mult)) + 1
                new_title = title
        
        cursor.execute("UPDATE users SET level = ?, title = ? WHERE user_id = ?", (new_level, new_title, user_id))
        conn.commit()
        conn.close()
        return {'level': new_level, 'title': new_title}
    
    @staticmethod
    def award_badge(user_id: int, badge_id: str):
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("INSERT OR IGNORE INTO badges (user_id, badge_id) VALUES (?, ?)", (user_id, badge_id))
            if cursor.rowcount > 0:
                Database.update_user_xp(user_id, 50)
            conn.commit()
            conn.close()
            return True
        except:
            return False
    
    @staticmethod
    def get_badges(user_id: int) -> List[dict]:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT badge_id, earned_at FROM badges WHERE user_id = ? ORDER BY earned_at DESC", (user_id,))
        badges = [{'id': row[0], **BADGES.get(row[0], {}), 'earned': row[1]} for row in cursor.fetchall()]
        conn.close()
        return badges
    
    @staticmethod
    def complete_task(task_id: int, user_id: int, is_early: bool = False, is_night: bool = False):
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute(
            "SELECT frequency, last_completed, streak, longest_streak, completion_count, reminder_type, specific_date FROM tasks WHERE id = ?",
            (task_id,)
        )
        result = cursor.fetchone()
        if not result:
            conn.close()
            return None
            
        frequency, last_completed, current_streak, longest_streak, comp_count, reminder_type, specific_date = result
        current_streak = current_streak or 0
        longest_streak = longest_streak or 0
        comp_count = comp_count or 0
        
        now = datetime.now()
        today = now.strftime('%Y-%m-%d')
        
        # For one-time specific date tasks, just mark complete
        if reminder_type == 'one_time' and specific_date:
            new_streak = 1
            next_date = None
            is_archived = True
        else:
            # Recurring logic
            new_streak = current_streak + 1
            if last_completed:
                last_date = datetime.strptime(last_completed.split()[0], '%Y-%m-%d')
                days_diff = (now - last_date).days
                if frequency == 'daily' and days_diff > 1:
                    cursor.execute("SELECT freeze_tokens FROM users WHERE user_id = ?", (user_id,))
                    tokens = cursor.fetchone()[0] or 0
                    if tokens > 0:
                        cursor.execute("UPDATE users SET freeze_tokens = freeze_tokens - 1 WHERE user_id = ?", (user_id,))
                    else:
                        new_streak = 1
            
            new_longest = max(longest_streak, new_streak)
            
            if frequency == 'once':
                next_date = None
                is_archived = True
            elif frequency == 'daily':
                next_date = (now + timedelta(days=1)).strftime('%Y-%m-%d')
                is_archived = False
            elif frequency == 'weekly':
                next_date = (now + timedelta(weeks=1)).strftime('%Y-%m-%d')
                is_archived = False
            else:
                next_month = now.month + 1 if now.month < 12 else 1
                next_year = now.year if now.month < 12 else now.year + 1
                next_date = f"{next_year}-{next_month:02d}-{now.day:02d}"
                is_archived = False
        
        base_xp = 10
        streak_bonus = min(new_streak * 2, 50)
        time_bonus = 5 if (is_early or is_night) else 0
        type_bonus = 20 if reminder_type == 'one_time' else 0
        
        cursor.execute("SELECT is_pro FROM users WHERE user_id = ?", (user_id,))
        is_pro = cursor.fetchone()[0]
        pro_mult = 1.5 if is_pro else 1.0
        
        total_xp = int((base_xp + streak_bonus + time_bonus + type_bonus) * pro_mult)
        
        if is_archived:
            cursor.execute(
                "UPDATE tasks SET is_active = 0, last_completed = ?, completion_count = ?, xp_earned = xp_earned + ? WHERE id = ?",
                (now.strftime('%Y-%m-%d %H:%M:%S'), comp_count + 1, total_xp, task_id)
            )
        else:
            cursor.execute(
                """UPDATE tasks SET last_completed = ?, completion_count = ?, streak = ?, 
                   longest_streak = ?, next_reminder_date = ?, xp_earned = xp_earned + ?
                   WHERE id = ?""",
                (now.strftime('%Y-%m-%d %H:%M:%S'), comp_count + 1, new_streak, 
                 max(longest_streak, new_streak), next_date, total_xp, task_id)
            )
        
        cursor.execute("INSERT INTO completions (task_id, user_id, xp_earned) VALUES (?, ?, ?)", (task_id, user_id, total_xp))
        conn.commit()
        conn.close()
        
        level_info = Database.update_user_xp(user_id, total_xp)
        
        new_badges = []
        if new_streak >= 7 and Database.award_badge(user_id, 'streak_7'):
            new_badges.append(BADGES['streak_7'])
        if is_early and Database.award_badge(user_id, 'early_bird'):
            new_badges.append(BADGES['early_bird'])
        
        return {
            'streak': new_streak, 'xp_earned': total_xp, 'level_up': level_info,
            'new_badges': new_badges, 'is_archived': is_archived
        }
    
    @staticmethod
    def get_tasks(user_id: int, active_only: bool = True, include_future: bool = False) -> List[dict]:
        timezone_str = get_user_timezone(user_id)
        today = datetime.now().strftime('%Y-%m-%d')
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        if include_future:
            cursor.execute(
                """SELECT id, task_name, reminder_time, reminder_time_utc, frequency, is_active, 
                   created_at, last_completed, completion_count, streak, longest_streak, 
                   next_reminder_date, category, reminder_type, specific_date, specific_date_original 
                   FROM tasks WHERE user_id = ? ORDER BY specific_date, reminder_time""", (user_id,)
            )
        elif active_only:
            cursor.execute(
                """SELECT id, task_name, reminder_time, reminder_time_utc, frequency, is_active, 
                   created_at, last_completed, completion_count, streak, longest_streak, 
                   next_reminder_date, category, reminder_type, specific_date, specific_date_original 
                   FROM tasks WHERE user_id = ? AND is_active = 1 
                   AND (next_reminder_date <= ? OR next_reminder_date IS NULL OR specific_date >= ?)
                   ORDER BY specific_date, reminder_time""", (user_id, today, today)
            )
        else:
            cursor.execute(
                """SELECT id, task_name, reminder_time, reminder_time_utc, frequency, is_active, 
                   created_at, last_completed, completion_count, streak, longest_streak, 
                   next_reminder_date, category, reminder_type, specific_date, specific_date_original 
                   FROM tasks WHERE user_id = ? ORDER BY specific_date, reminder_time""", (user_id,)
            )
        
        tasks = []
        for row in cursor.fetchall():
            local_time = convert_utc_to_local(row[3] or row[2], timezone_str)
            specific_date = row[14]
            
            # Calculate days until for display
            days_until = None
            if specific_date:
                date_obj = datetime.strptime(specific_date, '%Y-%m-%d')
                days_until = (date_obj - datetime.now()).days
            
            tasks.append({
                "id": row[0], "task_name": row[1], "reminder_time": local_time,
                "reminder_time_utc": row[3], "frequency": row[4], "is_active": bool(row[5]),
                "created_at": row[6], "last_completed": row[7], "completion_count": row[8] or 0,
                "streak": row[9] or 0, "longest_streak": row[10] or 0,
                "next_reminder_date": row[11], "category": row[12] or 'general',
                "reminder_type": row[13], "specific_date": specific_date,
                "specific_date_original": row[15], "days_until": days_until
            })
        conn.close()
        return tasks
    
    @staticmethod
    def get_reminder_tasks():
        utc_now = datetime.now(pytz.UTC)
        utc_time = utc_now.strftime("%H:%M")
        today = utc_now.strftime('%Y-%m-%d')
        
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Get recurring tasks that match current UTC time
        cursor.execute(
            """SELECT t.id, t.user_id, t.task_name, t.reminder_time, t.frequency,
                   t.streak, t.longest_streak, t.category, u.username, u.is_pro, u.timezone, 
                   t.reminder_time_utc, t.reminder_type, t.specific_date
               FROM tasks t 
               JOIN users u ON t.user_id = u.user_id 
               WHERE t.is_active = 1 
               AND t.reminder_time_utc = ?
               AND (t.next_reminder_date <= ? OR t.next_reminder_date IS NULL)
               AND (t.last_completed IS NULL OR date(t.last_completed) < ?)
               AND (t.reminder_type = 'recurring' OR (t.reminder_type = 'one_time' AND t.specific_date = ?))""",
            (utc_time, today, today, today)
        )
        tasks = [dict(zip([c[0] for c in cursor.description], row)) for row in cursor.fetchall()]
        conn.close()
        return tasks
    
    @staticmethod
    def delete_task(task_id: int, user_id: int) -> bool:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, user_id))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted

(ADD_TASK_NAME, ADD_TASK_DATE, ADD_TASK_CONFIRM) = range(3)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    context.user_data.clear()
    
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user.id, user.username))
    conn.commit()
    conn.close()
    
    user_data = Database.get_user(user_id=user.id) or {'is_pro': False, 'title': '🥉 Bronze', 'timezone': 'UTC'}
    local_time = get_local_time(user_data.get('timezone', 'UTC'))
    
    welcome = f"👋 Hello {user.first_name}! {user_data.get('title', '')}\n\n"
    welcome += f"🕐 Your time: {local_time.strftime('%H:%M %p')} ({user_data.get('timezone', 'UTC')})\n\n"
    
    welcome += (
        "📝 /addtask - Add task\n"
        "   • Daily/weekly/monthly\n"
        "   • Specific dates: 'June 12, 2026 at 3pm'\n"
        "✅ /done - Complete task\n"
        "📋 /tasks - Today's tasks\n"
        "🔮 /upcoming - Future scheduled tasks\n"
        "🏆 /profile - Stats\n"
        "🌍 /timezone - Set timezone\n"
    )
    
    if user_data.get('is_pro'):
        welcome += "\n💎 Pro features active"
    else:
        welcome += "\n💎 Upgrade to Pro: $8/mo"
    
    await update.message.reply_text(welcome, parse_mode='Markdown')

async def add_task_smart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Smart task addition with natural language date parsing"""
    await update.message.reply_text(
        "📝 What should I remind you about?\n\n"
        "Examples:\n"
        "• 'Sign contract on Monday June 12, 2026 at 2pm'\n"
        "• 'Call mom tomorrow at 3pm'\n"
        "• 'Team meeting every Friday at 10am'\n"
        "• 'Pay rent on the 1st at 9am'",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data='cancel')]])
    )
    return ADD_TASK_NAME

async def parse_task_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text
    user_id = update.effective_user.id
    timezone_str = get_user_timezone(user_id)
    
    # Try to parse natural language date
    parsed = parse_natural_date(user_input, timezone_str)
    
    if not parsed:
        # Try to extract manually
        # Look for patterns like "on Monday", "tomorrow", "June 12"
        await update.message.reply_text(
            "❓ I couldn't understand the date.\n\n"
            "Try formats like:\n"
            "• 'June 12, 2026 at 2pm'\n"
            "• 'Tomorrow at 9am'\n"
            "• 'Next Friday at 3pm'\n"
            "• Or just 'Daily at 9am' for recurring"
        )
        return ConversationHandler.END
    
    if parsed['is_past']:
        await update.message.reply_text("❌ That date is in the past! Try a future date.")
        return ConversationHandler.END
    
    # Store parsed data
    context.user_data['parsed'] = parsed
    context.user_data['original_input'] = user_input
    
    # Extract task name (remove date parts)
    task_name = user_input
    date_indicators = ['on ', 'at ', 'tomorrow', 'next ', 'every ', 'today']
    for indicator in date_indicators:
        if indicator in task_name.lower():
            # Simple extraction - everything before the date indicator
            parts = task_name.lower().split(indicator)
            if len(parts) > 1:
                task_name = parts[0].strip()
                break
    
    if not task_name or task_name == user_input:
        # Ask for clarification
        await update.message.reply_text("What's the task name? (e.g., 'Sign contract', 'Call mom')")
        return ADD_TASK_NAME
    
    context.user_data['task_name'] = task_name
    
    # Determine if recurring or one-time
    recurring_words = ['every', 'daily', 'weekly', 'monthly', 'each']
    is_recurring = any(word in user_input.lower() for word in recurring_words)
    
    if is_recurring:
        context.user_data['reminder_type'] = 'recurring'
        freq = 'daily'
        if 'week' in user_input.lower() or 'friday' in user_input.lower() or 'monday' in user_input.lower():
            freq = 'weekly'
        elif 'month' in user_input.lower():
            freq = 'monthly'
        context.user_data['frequency'] = freq
        
        await update.message.reply_text(
            f"📝 Task: {task_name}\n"
            f"⏰ Time: {parsed['time']}\n"
            f"🔄 Frequency: {freq}\n"
            f"📅 Starting: {parsed['date']}\n\n"
            f"Correct?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, add it", callback_data='confirm_recurring')],
                [InlineKeyboardButton("❌ Cancel", callback_data='cancel')]
            ])
        )
    else:
        context.user_data['reminder_type'] = 'one_time'
        
        days_text = ""
        if parsed['days_until'] == 0:
            days_text = "Today"
        elif parsed['days_until'] == 1:
            days_text = "Tomorrow"
        else:
            days_text = f"In {parsed['days_until']} days ({parsed['date']})"
        
        await update.message.reply_text(
            f"📝 Task: {task_name}\n"
            f"📅 Date: {days_text}\n"
            f"⏰ Time: {parsed['time']}\n"
            f"🔮 Type: One-time reminder\n\n"
            f"Correct?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Yes, add it", callback_data='confirm_onetime')],
                [InlineKeyboardButton("❌ Cancel", callback_data='cancel')]
            ])
        )
    
    return ADD_TASK_CONFIRM

async def confirm_task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == 'cancel':
        return await cancel_callback(update, context)
    
    user_id = update.effective_user.id
    data = context.user_data
    
    parsed = data['parsed']
    task_name = data['task_name']
    
    if query.data == 'confirm_recurring':
        task_id = Database.add_task(
            user_id=user_id,
            task_name=task_name,
            reminder_time=parsed['time'],
            frequency=data.get('frequency', 'daily'),
            specific_date=parsed['date'],
            reminder_type='recurring'
        )
        
        await query.edit_message_text(
            f"✅ Recurring task added!\n\n"
            f"📝 {task_name}\n"
            f"⏰ {parsed['time']} every {data.get('frequency', 'day')}\n"
            f"📅 Starting {parsed['date']}"
        )
        
    else:  # confirm_onetime
        task_id = Database.add_task(
            user_id=user_id,
            task_name=task_name,
            reminder_time=parsed['time'],
            frequency='once',
            specific_date=parsed['date'],
            reminder_type='one_time'
        )
        
        Database.award_badge(user_id, 'date_parser')
        
        days_until = parsed['days_until']
        when_text = "today" if days_until == 0 else f"in {days_until} days"
        
        await query.edit_message_text(
            f"✅ One-time reminder set!\n\n"
            f"📝 {task_name}\n"
            f"📅 {when_text} ({parsed['date']})\n"
            f"⏰ {parsed['time']}\n\n"
            f"🔮 I'll remind you then!"
        )
    
    return ConversationHandler.END

async def list_upcoming(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show future scheduled tasks"""
    user_id = update.effective_user.id
    tasks = Database.get_tasks(user_id, active_only=False, include_future=True)
    
    # Filter to future dates only
    today = datetime.now().strftime('%Y-%m-%d')
    future_tasks = [t for t in tasks if t['specific_date'] and t['specific_date'] > today]
    
    if not future_tasks:
        await update.message.reply_text("🔮 No future tasks scheduled!\n\nUse /addtask to plan ahead.")
        return
    
    message = "🔮 *Upcoming Tasks*\n\n"
    
    for task in future_tasks:
        date_obj = datetime.strptime(task['specific_date'], '%Y-%m-%d')
        days_until = (date_obj - datetime.now()).days
        date_display = date_obj.strftime('%b %d, %Y')
        
        when = f"in {days_until} days" if days_until > 1 else "tomorrow"
        
        message += f"📅 *{date_display}* ({when})\n"
        message += f"⏰ {task['reminder_time']} - {task['task_name']}\n\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

# Legacy command for simple recurring tasks
async def add_simple_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Quick add (recurring only):\n\nWhat task?",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data='cancel')]])
    )
    return ADD_TASK_NAME

async def simple_task_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['task_name'] = update.message.text
    await update.message.reply_text("⏰ What time? (HH:MM)")
    return ADD_TASK_DATE

async def simple_task_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        datetime.strptime(update.message.text, "%H:%M")
    except ValueError:
        await update.message.reply_text("❌ Use HH:MM format")
        return ADD_TASK_DATE
    
    user_id = update.effective_user.id
    task_id = Database.add_task(
        user_id=user_id,
        task_name=context.user_data['task_name'],
        reminder_time=update.message.text,
        frequency='daily'
    )
    
    await update.message.reply_text(
        f"✅ Daily task added: {context.user_data['task_name']} at {update.message.text}"
    )
    return ConversationHandler.END

async def complete_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = Database.get_tasks(user_id, active_only=True)
    
    if not tasks:
        await update.message.reply_text("No tasks to complete!")
        return
    
    keyboard = []
    for task in tasks:
        streak = f" 🔥{task['streak']}" if task['streak'] > 0 else ""
        date_info = ""
        
        if task['specific_date'] and task['reminder_type'] == 'one_time':
            days = task.get('days_until')
            if days == 0:
                date_info = " (Today)"
            elif days == 1:
                date_info = " (Tomorrow)"
            else:
                date_info = f" ({task['specific_date']})"
        
        emoji = {'daily': '📅', 'weekly': '📆', 'monthly': '🗓️', 'once': '☑️'}.get(task['frequency'], '📅')
        keyboard.append([InlineKeyboardButton(
            f"{emoji} {task['task_name']}{date_info}{streak}",
            callback_data=f"complete_{task['id']}"
        )])
    
    await update.message.reply_text("Which did you complete?", reply_markup=InlineKeyboardMarkup(keyboard))

async def complete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    task_id = int(query.data.split('_')[1])
    user_id = update.effective_user.id
    
    hour = datetime.now().hour
    is_early = hour < 8
    is_night = hour > 22
    
    result = Database.complete_task(task_id, user_id, is_early, is_night)
    
    if not result:
        await query.edit_message_text("❌ Task not found")
        return
    
    message = f"✅ Complete! +{result['xp_earned']} XP\n"
    
    if result.get('is_archived'):
        message += "📁 Task archived\n"
    else:
        message += f"🔥 Streak: {result['streak']}\n"
    
    if result['new_badges']:
        message += "\n🏅 " + ", ".join([b['name'] for b in result['new_badges']])
    
    await query.edit_message_text(message)

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = Database.get_user(user_id) or {'timezone': 'UTC'}
    tasks = Database.get_tasks(user_id, active_only=True)
    
    if not tasks:
        await update.message.reply_text("No pending tasks!")
        return
    
    local_time = get_local_time(user['timezone'])
    message = f"📋 Tasks (your time: {local_time.strftime('%H:%M')})\n\n"
    
    for task in tasks:
        emoji = {'daily': '📅', 'weekly': '📆', 'monthly': '🗓️', 'once': '☑️'}.get(task['frequency'], '📅')
        streak = f" 🔥{task['streak']}" if task['streak'] > 0 else ""
        
        date_info = ""
        if task['specific_date'] and task['reminder_type'] == 'one_time':
            if task.get('days_until') == 0:
                date_info = " TODAY"
            elif task.get('days_until') == 1:
                date_info = " tomorrow"
        
        message += f"{emoji}{date_info} `{task['reminder_time']}` {task['task_name']}{streak}\n"
    
    await update.message.reply_text(message, parse_mode='Markdown')

async def timezone_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(tz, callback_data=f"tz_{tz}") for tz in row] for row in TIMEZONES]
    await update.message.reply_text("🌍 Select timezone:", reply_markup=InlineKeyboardMarkup(keyboard))

async def timezone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    tz = query.data.replace("tz_", "")
    user_id = update.effective_user.id
    
    Database.set_timezone(user_id, tz)
    local_time = get_local_time(tz)
    
    await query.edit_message_text(f"✅ Timezone: {tz}\n🕐 Your time: {local_time.strftime('%H:%M %p')}")

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = Database.get_user(user_id) or {}
    tasks = Database.get_tasks(user_id, active_only=False)
    
    total = sum(t['completion_count'] for t in tasks)
    best = max((t['longest_streak'] for t in tasks), default=0)
    upcoming = len([t for t in tasks if t.get('days_until') and t['days_until'] > 0])
    
    message = (
        f"🏆 {user.get('title', '🥉 Bronze')}\n"
        f"⭐ Level {user.get('level', 1)}\n"
        f"🔥 Best streak: {best}\n"
        f"✅ Completed: {total}\n"
        f"🔮 Upcoming: {upcoming} scheduled\n"
    )
    
    await update.message.reply_text(message)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled")
    return ConversationHandler.END

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("❌ Cancelled")
    return ConversationHandler.END

async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    tasks = Database.get_reminder_tasks()
    
    for task in tasks:
        try:
            user_tz = task.get('timezone', 'UTC')
            local_time = convert_utc_to_local(task['reminder_time_utc'], user_tz)
            
            date_info = ""
            if task.get('specific_date'):
                date_obj = datetime.strptime(task['specific_date'], '%Y-%m-%d')
                if date_obj.date() == datetime.now().date():
                    date_info = "\n📅 Today!"
                else:
                    date_info = f"\n📅 {date_obj.strftime('%b %d')}"
            
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Complete", callback_data=f"complete_{task['id']}")]])
            
            await context.bot.send_message(
                chat_id=task['user_id'],
                text=f"🔔 Reminder{date_info}\n\n📝 {task['task_name']}\n⏰ Your time: {local_time}",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Failed: {e}")

def main():
    init_db()
    
    TOKEN = os.environ.get("TOKEN")
    if not TOKEN:
        logger.error("No TOKEN!")
        return
    
    application = Application.builder().token(TOKEN).build()
    
    # Smart task addition with natural language
    smart_conv = ConversationHandler(
        entry_points=[CommandHandler('addtask', add_task_smart)],
        states={
            ADD_TASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, parse_task_input)],
            ADD_TASK_CONFIRM: [CallbackQueryHandler(confirm_task_callback, pattern='^confirm_')],
        },
        fallbacks=[CommandHandler('cancel', cancel), CallbackQueryHandler(cancel_callback, pattern='^cancel')],
    )
    
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('help', start))
    application.add_handler(CommandHandler('profile', profile))
    application.add_handler(CommandHandler('tasks', list_tasks))
    application.add_handler(CommandHandler('upcoming', list_upcoming))
    application.add_handler(CommandHandler('done', complete_task))
    application.add_handler(CommandHandler('timezone', timezone_command))
    application.add_handler(CallbackQueryHandler(timezone_callback, pattern='^tz_'))
    application.add_handler(CallbackQueryHandler(complete_callback, pattern='^complete_'))
    application.add_handler(smart_conv)
    
    application.job_queue.run_repeating(reminder_job, interval=60, first=10)
    
    logger.info("🤖 Bot running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

app = Flask(__name__)

@app.route('/')
def dashboard():
    return "<h1>🔔 Mula Bot with Date Parsing</h1><p>Natural language reminders active!</p>"

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))

if __name__ == '__main__':
    threading.Thread(target=run_flask).start()
    main()
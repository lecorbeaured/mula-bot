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

import pathlib
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
DB_FILE = os.path.join(DATA_DIR, "reminders.db")
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
    ['Sydney', 'Auckland', 'UTC', 'Other...']
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

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_stats (
            user_id INTEGER PRIMARY KEY,
            xp INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1,
            streak_current INTEGER DEFAULT 0,
            streak_best INTEGER DEFAULT 0,
            last_completed_date TEXT,
            tasks_completed INTEGER DEFAULT 0
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS task_completions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            xp_earned INTEGER DEFAULT 0
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS badges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            badge_key TEXT NOT NULL,
            earned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, badge_key)
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            pro_status TEXT DEFAULT 'free',
            pro_expires_at TEXT,
            freeze_tokens INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()

    # Migrations: add columns that may be missing from older DBs
    migrations = [
        "ALTER TABLE tasks ADD COLUMN due_date TEXT",
        "ALTER TABLE tasks ADD COLUMN is_recurring INTEGER DEFAULT 0",
        "ALTER TABLE tasks ADD COLUMN frequency TEXT DEFAULT 'once'",
        "ALTER TABLE tasks ADD COLUMN reminder_time_utc TEXT",
    ]
    for sql in migrations:
        try:
            cursor.execute(sql)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists, skip

    # Backfill reminder_time_utc for tasks where it's NULL
    # Also normalize malformed reminder_time values (e.g. "712pm" -> "19:12")
    cursor.execute(
        "SELECT id, user_id, reminder_time FROM tasks WHERE is_active=1"
    )
    tasks_to_fix = cursor.fetchall()
    for task_id, user_id, raw_time in tasks_to_fix:
        import re as _re
        # Normalize "712pm" -> "7:12pm"
        normalized = _re.sub(r'(\d{1,2})(\d{2})(am|pm)', r'\1:\2\3', raw_time, flags=_re.IGNORECASE)
        # Parse to HH:MM 24h
        try:
            from datetime import datetime as _dt
            if ':' in normalized:
                t = _dt.strptime(normalized.strip().upper(), "%I:%M%p")
            else:
                t = _dt.strptime(normalized.strip().upper(), "%I%p")
            hhmm = t.strftime("%H:%M")
        except Exception:
            hhmm = raw_time  # leave as-is if unparseable

        # Update reminder_time to normalized HH:MM and backfill reminder_time_utc
        # Use UTC as default timezone if user has none set
        cursor.execute("SELECT timezone FROM users WHERE user_id=?", (user_id,))
        tz_row = cursor.fetchone()
        tz_str = tz_row[0] if tz_row else 'UTC'

        import pytz as _pytz
        from datetime import datetime as _dt2
        try:
            tz = _pytz.timezone(tz_str)
            today = _dt2.now(tz).strftime('%Y-%m-%d')
            local_dt = _dt2.strptime(f"{today} {hhmm}", "%Y-%m-%d %H:%M")
            local_dt = tz.localize(local_dt)
            utc_time = local_dt.astimezone(_pytz.UTC).strftime("%H:%M")
        except Exception:
            utc_time = hhmm

        cursor.execute(
            "UPDATE tasks SET reminder_time=?, reminder_time_utc=? WHERE id=?",
            (hhmm, utc_time, task_id)
        )
    conn.commit()

    conn.close()

init_db()

# ── MESSAGING ABSTRACTION ─────────────────────────────────────────────────────
# All outbound messages go through these functions.
# To add a new platform (WhatsApp, SMS, Discord etc.), implement
# the same interface here — the rest of the bot stays untouched.

async def msg_send(bot, chat_id, text, reply_markup=None, parse_mode='Markdown'):
    """Send a new message to a user."""
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode
    )

async def msg_reply(update, text, reply_markup=None, parse_mode='Markdown'):
    """Reply to the current update's message."""
    await update.message.reply_text(
        text,
        reply_markup=reply_markup,
        parse_mode=parse_mode
    )

async def msg_edit(query, text, reply_markup=None, parse_mode='Markdown'):
    """Edit an existing inline message (callback query response)."""
    await query.edit_message_text(
        text,
        reply_markup=reply_markup,
        parse_mode=parse_mode
    )

# ── END MESSAGING ABSTRACTION ─────────────────────────────────────────────────

# ── GAMIFICATION ─────────────────────────────────────────────────────────────

XP_PER_TASK = 10
XP_PER_STREAK_BONUS = 5  # extra XP per streak day (stacks)

LEVELS = [
    (1,    0,    "🌱 Seedling"),
    (2,    100,  "🌿 Sprout"),
    (3,    250,  "🌳 Grower"),
    (4,    500,  "⚡ Hustler"),
    (5,    1000, "🔥 Grinder"),
    (6,    2000, "💎 Diamond"),
    (7,    4000, "👑 Legend"),
]

BADGES = {
    "first_task":     ("🎯", "First Step",      "Completed your first task"),
    "early_bird":     ("🌅", "Early Bird",       "Completed a task before 9 AM"),
    "night_owl":      ("🦉", "Night Owl",        "Completed a task after 10 PM"),
    "week_warrior":   ("🗡️", "Week Warrior",     "7-day streak"),
    "month_master":   ("🏆", "Month Master",     "30-day streak"),
    "century":        ("💯", "Century",          "Completed 100 tasks"),
    "speed_runner":   ("⚡", "Speed Runner",     "Completed 5 tasks in one day"),
    "consistent":     ("📅", "Consistent",       "3-day streak"),
}

def get_level(xp):
    level_info = LEVELS[0]
    for entry in LEVELS:
        if xp >= entry[1]:
            level_info = entry
        else:
            break
    return level_info

def xp_to_next_level(xp):
    for i, entry in enumerate(LEVELS):
        if xp < entry[1]:
            return entry[1] - xp, entry[2]
    return 0, LEVELS[-1][2]  # maxed out

def get_or_create_stats(user_id, conn=None):
    close = conn is None
    if close:
        conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM user_stats WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute("INSERT INTO user_stats (user_id) VALUES (?)", (user_id,))
        conn.commit()
        cursor.execute("SELECT * FROM user_stats WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
    if close:
        conn.close()
    cols = ['user_id','xp','level','streak_current','streak_best','last_completed_date','tasks_completed']
    return dict(zip(cols, row))

def award_xp(user_id, xp_amount, conn):
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE user_stats SET xp = xp + ?, tasks_completed = tasks_completed + 1 WHERE user_id = ?",
        (xp_amount, user_id)
    )

def update_streak(user_id, conn):
    """Update streak based on today's date. Returns (current_streak, is_new_day)."""
    cursor = conn.cursor()
    today = datetime.utcnow().strftime('%Y-%m-%d')
    stats = get_or_create_stats(user_id, conn)
    last = stats['last_completed_date']
    streak = stats['streak_current']
    best = stats['streak_best']

    if last == today:
        return streak, False  # already completed today

    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')
    if last == yesterday:
        streak += 1
    else:
        streak = 1  # reset

    best = max(best, streak)
    cursor.execute(
        """UPDATE user_stats 
           SET streak_current = ?, streak_best = ?, last_completed_date = ?
           WHERE user_id = ?""",
        (streak, best, today, user_id)
    )
    return streak, True

def check_and_award_badges(user_id, stats, conn):
    """Check all badge conditions and award any newly earned ones. Returns list of new badge keys."""
    cursor = conn.cursor()
    new_badges = []

    def earned(key):
        cursor.execute("SELECT 1 FROM badges WHERE user_id = ? AND badge_key = ?", (user_id, key))
        return cursor.fetchone() is not None

    def award(key):
        try:
            cursor.execute("INSERT INTO badges (user_id, badge_key) VALUES (?, ?)", (user_id, key))
            new_badges.append(key)
        except sqlite3.IntegrityError:
            pass

    if stats['tasks_completed'] >= 1 and not earned("first_task"):
        award("first_task")
    if stats['tasks_completed'] >= 100 and not earned("century"):
        award("century")
    if stats['streak_current'] >= 3 and not earned("consistent"):
        award("consistent")
    if stats['streak_current'] >= 7 and not earned("week_warrior"):
        award("week_warrior")
    if stats['streak_current'] >= 30 and not earned("month_master"):
        award("month_master")

    # Time-based badges checked separately in check_reminders via local time
    return new_badges

def get_all_badges(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT badge_key, earned_at FROM badges WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def format_badge_notifications(new_badge_keys):
    if not new_badge_keys:
        return ""
    lines = ["\n🏅 *New Badge(s) Unlocked!*"]
    for key in new_badge_keys:
        b = BADGES.get(key)
        if b:
            lines.append(f"  {b[0]} *{b[1]}* — {b[2]}")
    return "\n".join(lines)

def complete_task(user_id, task_id, local_hour=None):
    """Record task completion, update XP/streak/badges. Returns result dict."""
    conn = sqlite3.connect(DB_FILE)
    try:
        get_or_create_stats(user_id, conn)
        streak, is_new_day = update_streak(user_id, conn)

        # XP = base + streak bonus (Pro gets 1.5x)
        base_xp = XP_PER_TASK + (streak - 1) * XP_PER_STREAK_BONUS
        multiplier = PRO_XP_MULTIPLIER if is_pro(user_id) else 1.0
        xp_earned = int(base_xp * multiplier)
        award_xp(user_id, xp_earned, conn)

        # Log completion
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO task_completions (user_id, task_id, xp_earned) VALUES (?, ?, ?)",
            (user_id, task_id, xp_earned)
        )

        # Check daily task count for speed runner badge
        today = datetime.utcnow().strftime('%Y-%m-%d')
        cursor.execute(
            "SELECT COUNT(*) FROM task_completions WHERE user_id = ? AND DATE(completed_at) = ?",
            (user_id, today)
        )
        daily_count = cursor.fetchone()[0]

        stats = get_or_create_stats(user_id, conn)
        new_badges = check_and_award_badges(user_id, stats, conn)

        # Time-based badges
        if local_hour is not None:
            if local_hour < 9 and "early_bird" not in [b for b in new_badges]:
                try:
                    cursor.execute("INSERT INTO badges (user_id, badge_key) VALUES (?, ?)", (user_id, "early_bird"))
                    new_badges.append("early_bird")
                except sqlite3.IntegrityError:
                    pass
            if local_hour >= 22:
                try:
                    cursor.execute("INSERT INTO badges (user_id, badge_key) VALUES (?, ?)", (user_id, "night_owl"))
                    new_badges.append("night_owl")
                except sqlite3.IntegrityError:
                    pass

        if daily_count >= 5 and "speed_runner" not in new_badges:
            try:
                cursor.execute("INSERT INTO badges (user_id, badge_key) VALUES (?, ?)", (user_id, "speed_runner"))
                new_badges.append("speed_runner")
            except sqlite3.IntegrityError:
                pass

        conn.commit()
        stats = get_or_create_stats(user_id, conn)
        level_info = get_level(stats['xp'])

        return {
            'xp_earned': xp_earned,
            'total_xp': stats['xp'],
            'level': level_info,
            'streak': streak,
            'is_new_day': is_new_day,
            'new_badges': new_badges,
            'tasks_completed': stats['tasks_completed'],
        }
    finally:
        conn.close()

# ── END GAMIFICATION ──────────────────────────────────────────────────────────

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

    # If full text fails, try progressively stripping leading words
    if not parsed:
        words = text_normalized.split()
        for i in range(1, len(words)):
            partial = ' '.join(words[i:])
            parsed = dateparser.parse(partial, settings=settings)
            if parsed:
                break

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
    
    # Time-only input: no date keyword found, but a time is present -> default to today/tomorrow
    date_keywords = ['tomorrow', 'today', 'next week', 'monday', 'tuesday', 'wednesday',
                     'thursday', 'friday', 'saturday', 'sunday', 'january', 'february',
                     'march', 'april', 'may', 'june', 'july', 'august', 'september',
                     'october', 'november', 'december', 'in 2 days', 'in 3 days',
                     'in 1 day', 'in 4 days', 'in 5 days', 'in 6 days', 'in 7 days',
                     'in 1 hour', 'in 2 hours', 'in 3 hours', 'in 4 hours',
                     'in 5 hours', 'in 6 hours', 'in 12 hours', 'in 24 hours',
                     'in 30 minutes', 'in 15 minutes', 'in 45 minutes', 'in 10 minutes',
                     'in 20 minutes', 'in 5 minutes', 'in 1 minute',
                     'minute', 'minutes', 'mins', 'hour', 'hours']
    has_date_keyword = any(kw in text_lower for kw in date_keywords)
    has_year = bool(re.search(r'\b20\d{2}\b', text_normalized))

    if not has_date_keyword and not has_year and not parsed:
        time_only_patterns = [
            r'(\d{1,2}):(\d{2})\s*(am|pm)',
            r'(\d{1,2})(\d{2})\s*(am|pm)',
            r'(\d{1,2})\s*(am|pm)',
        ]
        for pattern in time_only_patterns:
            match = re.search(pattern, text_lower)
            if match:
                groups = match.groups()
                hour = int(groups[0])
                minute = int(groups[1]) if len(groups) > 2 and groups[1] and groups[1].isdigit() else 0
                ampm = groups[-1]
                if ampm == 'pm' and hour != 12:
                    hour += 12
                elif ampm == 'am' and hour == 12:
                    hour = 0
                candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                # If that time already passed today, schedule for tomorrow
                if candidate <= now:
                    candidate = candidate + timedelta(days=1)
                return {
                    'datetime': candidate,
                    'date': candidate.strftime('%Y-%m-%d'),
                    'time': candidate.strftime('%H:%M'),
                    'is_recurring': is_recurring
                }

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
    
    # If dateparser worked but date is in past
    if parsed:
        if parsed <= now:
            # Check if user explicitly typed a year — if so, don't auto-bump,
            # let process_natural_input show a helpful error with the right suggestion
            explicit_year = bool(re.search(r'\b(20\d{2})\b', text_normalized))
            if explicit_year:
                # Return as-is so the caller can detect it's in the past and warn the user
                return {
                    'datetime': parsed,
                    'date': parsed.strftime('%Y-%m-%d'),
                    'time': parsed.strftime('%H:%M'),
                    'is_recurring': is_recurring
                }
            try:
                next_year_parsed = dateparser.parse(text_normalized + " next year", settings=settings)
                if next_year_parsed and next_year_parsed > now:
                    parsed = next_year_parsed
                else:
                    # Manually bump year
                    bumped = parsed.replace(year=parsed.year + 1)
                    if bumped > now:
                        parsed = bumped
            except Exception:
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
        r'in \d+ hours?',
        r'in \d+ mins?(?:utes?)?',
        r'in \d+ seconds?',
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

def utc_to_local(utc_time_str, timezone_str, date_str=None):
    try:
        utc = pytz.UTC
        tz = pytz.timezone(timezone_str)
        ref_date = date_str or datetime.now(utc).strftime('%Y-%m-%d')
        utc_dt = datetime.strptime(f"{ref_date} {utc_time_str}", "%Y-%m-%d %H:%M")
        utc_dt = utc.localize(utc_dt)
        local_dt = utc_dt.astimezone(tz)
        return local_dt.strftime("%H:%M")
    except:
        return utc_time_str

def fmt_time(time_str):
    """Convert HH:MM (24h) to 12-hour format e.g. 2:45 PM"""
    try:
        from datetime import datetime as _dt
        return _dt.strptime(time_str, "%H:%M").strftime("%-I:%M %p")
    except:
        return time_str

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
        local_time = utc_to_local(row[3] or row[2], timezone_str, date_str=row[4])
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
CUSTOM_TZ_INPUT = 10  # separate state for custom timezone

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tz = get_user_timezone(user.id)
    friendly = get_friendly_name(tz)
    local_time = get_local_time(tz)
    
    await msg_reply(update,
        f"👋 Hello {user.first_name}!\n\n"
        f"🕐 Your time: {local_time.strftime('%-I:%M %p')} ({friendly})\n\n"
        f"📝 /add - Add task\n"
        f"   Examples:\n"
        f"   • 'Call John tomorrow at 3pm'\n"
        f"   • 'Meeting every Friday at 10am'\n"
        f"   • 'Pay rent March 31 at 2:45pm'\n\n"
        f"🌍 /timezone - Change city\n"
        f"📋 /list - View tasks\n"
        f"❌ /delete - Remove task\n\n"
        f"⚡ /stats - XP, level & streak\n"
        f"🏅 /badges - Your achievements\n\n"
        f"💎 /upgrade - Go Pro ($8/mo)\n"
        f"🧊 /freeze - Protect your streak (Pro)"
    )

async def timezone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(tz, callback_data=f"tz_{tz}") for tz in row] for row in TIMEZONE_DISPLAY]
    await msg_reply(update, "🌍 Select your city:", reply_markup=InlineKeyboardMarkup(keyboard))

async def timezone_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    friendly_name = query.data.replace("tz_", "")

    if friendly_name == "Other...":
        await msg_edit(query,
            "🌍 Type your city or timezone name:\n\n"
            "Examples:\n"
            "• Las Vegas\n"
            "• Lagos\n"
            "• Nairobi\n"
            "• Mumbai\n"
            "• São Paulo\n"
            "• America/Chicago\n"
            "• Europe/Istanbul"
        )
        return CUSTOM_TZ_INPUT

    actual_tz = TIMEZONE_MAP.get(friendly_name, 'UTC')
    user_id = update.effective_user.id

    set_user_timezone(user_id, actual_tz)
    local_time = get_local_time(actual_tz)

    await msg_edit(query, f"✅ Timezone: {friendly_name}\n🕐 Your time: {local_time.strftime('%I:%M %p')}")

# Common city aliases not found directly in pytz
CITY_ALIASES = {
    'las vegas': 'America/Los_Angeles',
    'phoenix': 'America/Phoenix',
    'honolulu': 'Pacific/Honolulu',
    'anchorage': 'America/Anchorage',
    'toronto': 'America/Toronto',
    'montreal': 'America/Montreal',
    'vancouver': 'America/Vancouver',
    'mexico city': 'America/Mexico_City',
    'bogota': 'America/Bogota',
    'lima': 'America/Lima',
    'buenos aires': 'America/Argentina/Buenos_Aires',
    'sao paulo': 'America/Sao_Paulo',
    'santiago': 'America/Santiago',
    'cairo': 'Africa/Cairo',
    'lagos': 'Africa/Lagos',
    'nairobi': 'Africa/Nairobi',
    'johannesburg': 'Africa/Johannesburg',
    'lome': 'Africa/Abidjan',
    'accra': 'Africa/Accra',
    'dakar': 'Africa/Dakar',
    'casablanca': 'Africa/Casablanca',
    'istanbul': 'Europe/Istanbul',
    'amsterdam': 'Europe/Amsterdam',
    'stockholm': 'Europe/Stockholm',
    'oslo': 'Europe/Oslo',
    'brussels': 'Europe/Brussels',
    'vienna': 'Europe/Vienna',
    'zurich': 'Europe/Zurich',
    'rome': 'Europe/Rome',
    'madrid': 'Europe/Madrid',
    'lisbon': 'Europe/Lisbon',
    'athens': 'Europe/Athens',
    'warsaw': 'Europe/Warsaw',
    'prague': 'Europe/Prague',
    'budapest': 'Europe/Budapest',
    'bucharest': 'Europe/Bucharest',
    'helsinki': 'Europe/Helsinki',
    'riyadh': 'Asia/Riyadh',
    'tehran': 'Asia/Tehran',
    'karachi': 'Asia/Karachi',
    'mumbai': 'Asia/Kolkata',
    'delhi': 'Asia/Kolkata',
    'new delhi': 'Asia/Kolkata',
    'kolkata': 'Asia/Kolkata',
    'dhaka': 'Asia/Dhaka',
    'bangkok': 'Asia/Bangkok',
    'jakarta': 'Asia/Jakarta',
    'manila': 'Asia/Manila',
    'hong kong': 'Asia/Hong_Kong',
    'taipei': 'Asia/Taipei',
    'seoul': 'Asia/Seoul',
    'osaka': 'Asia/Tokyo',
    'beijing': 'Asia/Shanghai',
    'melbourne': 'Australia/Melbourne',
    'brisbane': 'Australia/Brisbane',
    'perth': 'Australia/Perth',
}

async def custom_timezone_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle free-text timezone input after user taps Other..."""
    user_input = update.message.text.strip()
    user_id = update.effective_user.id

    matched_tz = None

    # 1. Check city aliases first
    alias_key = user_input.lower().strip()
    if alias_key in CITY_ALIASES:
        matched_tz = CITY_ALIASES[alias_key]

    # 2. Try direct pytz lookup (e.g. "America/Chicago")
    if not matched_tz:
        try:
            pytz.timezone(user_input)
            matched_tz = user_input
        except pytz.exceptions.UnknownTimeZoneError:
            pass

    # 3. Fuzzy match against pytz timezone names
    if not matched_tz:
        search = user_input.lower().replace(' ', '_')
        candidates = [tz for tz in pytz.all_timezones if search in tz.lower()]
        if candidates:
            exact = [tz for tz in candidates if tz.split('/')[-1].lower() == search]
            matched_tz = exact[0] if exact else candidates[0]

    if matched_tz:
        set_user_timezone(user_id, matched_tz)
        local_time = get_local_time(matched_tz)
        await msg_reply(update,
            f"✅ Timezone set to: {matched_tz}\n"
            f"🕐 Your time: {local_time.strftime('%I:%M %p')}"
        )
        return ConversationHandler.END
    else:
        await msg_reply(update,
            f"❌ Couldn't find timezone for *{user_input}*.\n\n"
            f"Try a major nearby city or use a timezone name like:\n"
            f"• Africa/Lagos\n"
            f"• America/Sao\_Paulo\n"
            f"• Asia/Kolkata\n\n"
            f"Full list: en.wikipedia.org/wiki/List\_of\_tz\_database\_time\_zones",
            parse_mode='Markdown'
        )
        return CUSTOM_TZ_INPUT

async def add_smart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await msg_reply(update,
        "📝 What should I remind you about?\n\n"
        "Examples:\n"
        "• Call John tomorrow at 3pm\n"
        "• Meeting every Friday at 10am\n"
        "• Pay rent March 31 at 2:45pm\n"
        "• Doctor appointment in 2 days at 2pm"
    )
    return NATURAL_INPUT

async def _send_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE, task_name: str, parsed: dict):
    """Show confirmation message with task details."""
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

    await msg_reply(update,
        f"📝 Task: {task_name}\n"
        f"📅 Date: {date_display}\n"
        f"⏰ Time: {fmt_time(parsed['time'])}\n"
        f"🔄 Type: {recurring_text}\n\n"
        f"Correct?",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, add it", callback_data='confirm_add')],
            [InlineKeyboardButton("❌ Cancel", callback_data='cancel')]
        ])
    )
    return CONFIRM


async def process_natural_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = update.message.text
    user_id = update.effective_user.id
    timezone_str = get_user_timezone(user_id)

    # If we're waiting for just the task name, capture it now
    if context.user_data.get('awaiting_name'):
        task_name = user_input.strip()
        parsed = context.user_data.get('parsed')
        if not task_name or not parsed:
            await msg_reply(update, "Please enter a task name (e.g., 'Pay rent').")
            return NATURAL_INPUT
        context.user_data['task_name'] = task_name
        context.user_data['awaiting_name'] = False
        return await _send_confirm(update, context, task_name, parsed)

    # If we're waiting for a date/time after a task-name-only input
    if context.user_data.get('awaiting_time'):
        task_name = context.user_data.get('task_name_pending', '').strip()
        user_input_normalized = normalize_time(user_input)
        parsed = parse_natural_date(user_input_normalized, timezone_str)
        if not parsed:
            await msg_reply(update,
                "❌ Still couldn't get the date/time. Try:\n"
                "• tomorrow at 3pm\n"
                "• March 31 at 2:45pm\n"
                "• next Monday at 10am"
            )
            return NATURAL_INPUT
        context.user_data['awaiting_time'] = False
        context.user_data['task_name'] = task_name
        context.user_data['parsed'] = parsed
        context.user_data['original_input'] = user_input
        return await _send_confirm(update, context, task_name, parsed)

    # Normalize input
    user_input_normalized = normalize_time(user_input)

    parsed = parse_natural_date(user_input_normalized, timezone_str)

    if not parsed:
        # No date/time found — save the input as the task name and ask for when
        task_name_candidate = user_input.strip()
        context.user_data['task_name_pending'] = task_name_candidate
        context.user_data['awaiting_time'] = True
        await msg_reply(update,
            f"⏰ When should I remind you about *{task_name_candidate}*?\n\n"
            "Examples:\n"
            "• tomorrow at 3pm\n"
            "• March 31 at 2:45pm\n"
            "• next Monday at 10am\n"
            "• every Friday at 9am",
            parse_mode='Markdown'
        )
        return NATURAL_INPUT

    user_tz = pytz.timezone(timezone_str)
    now = datetime.now(user_tz)
    parsed_dt = parsed['datetime']

    # Check if date is in past (with 1 minute buffer)
    if parsed_dt < now - timedelta(minutes=1):
        parsed_year = parsed_dt.year
        current_year = now.year
        days_diff = (now - parsed_dt).days

        # Explicit past year (e.g. March 31 2025 when it's 2026)
        if parsed_year < current_year:
            next_valid = parsed_dt.replace(year=current_year)
            if next_valid < now:
                next_valid = parsed_dt.replace(year=current_year + 1)
            await msg_reply(update,
                f"❌ {parsed_year} has already passed!\n"
                f"Did you mean {next_valid.strftime('%B %-d, %Y')} at {fmt_time(parsed['time'])}?\n\n"
                f"Please re-enter with the correct year."
            )
        # Same year but date already passed
        elif parsed_year == current_year and days_diff > 0:
            next_valid = parsed_dt.replace(year=current_year + 1)
            await msg_reply(update,
                f"❌ {parsed['date']} has already passed this year!\n"
                f"Did you mean {next_valid.strftime('%B %-d, %Y')} at {fmt_time(parsed['time'])}?\n\n"
                f"Please re-enter with the correct date."
            )
        # Time today is in past
        else:
            await msg_reply(update,
                f"❌ That time ({fmt_time(parsed['time'])}) has already passed today!\n"
                f"Try a future time, or specify tomorrow."
            )
        return NATURAL_INPUT

    task_name = extract_task_name(user_input)

    if not task_name or task_name == user_input:
        await msg_reply(update, "What's the task name? (e.g., 'Pay rent')")
        context.user_data['parsed'] = parsed
        context.user_data['original_input'] = user_input
        context.user_data['awaiting_name'] = True
        return NATURAL_INPUT

    context.user_data['task_name'] = task_name
    context.user_data['parsed'] = parsed
    context.user_data['original_input'] = user_input

    return await _send_confirm(update, context, task_name, parsed)

async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle both Yes and Cancel buttons"""
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    
    if query.data == 'cancel':
        await msg_edit(query, "❌ Cancelled")
        context.user_data.clear()
        return ConversationHandler.END
    
    # Handle confirm_add
    data = context.user_data
    
    if not data.get('parsed'):
        await msg_edit(query, "❌ Error: No task data found")
        return ConversationHandler.END
    
    task_name = data.get('task_name', 'Task')
    parsed = data['parsed']
    
    frequency = 'once'
    if parsed['is_recurring']:
        original = data.get('original_input', '').lower()
        if 'month' in original:
            frequency = 'monthly'
        elif 'week' in original:
            frequency = 'weekly'
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
    
    await msg_edit(query, 
        f"✅ Added!\n\n"
        f"📝 {task_name}\n"
        f"⏰ {fmt_time(parsed['time'])} {when}\n"
        f"{'🔁 ' + frequency if parsed['is_recurring'] else '☑️ One-time'}"
    )
    
    context.user_data.clear()
    return ConversationHandler.END

async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = get_tasks(user_id)
    
    if not tasks:
        await msg_reply(update, "No tasks! Add one with /add")
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
        
        msg += f"{emoji}{date_info} {task['name']} at {fmt_time(task['time'])}\n"
    
    await msg_reply(update, msg)

async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tasks = get_tasks(user_id)
    
    if not tasks:
        await msg_reply(update, "No tasks to delete!")
        return
    
    keyboard = []
    for task in tasks:
        emoji = "🔁" if task['is_recurring'] else "☑️"
        keyboard.append([InlineKeyboardButton(f"{emoji} {task['name']}", callback_data=f"del_{task['id']}")])
    
    await msg_reply(update, "Delete which?", reply_markup=InlineKeyboardMarkup(keyboard))

async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    task_id = int(query.data.split('_')[1])
    user_id = update.effective_user.id
    
    delete_task_db(task_id, user_id)
    await msg_edit(query, "🗑️ Deleted!")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await msg_reply(update, "❌ Cancelled")
    return ConversationHandler.END

async def check_reminders(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.utcnow()
    today = now.strftime("%Y-%m-%d")

    # Use a 2-minute window so a bot restart never silently drops a reminder
    current_minute = now.strftime("%H:%M")
    prev_minute = (now - timedelta(minutes=1)).strftime("%H:%M")
    time_window = list({current_minute, prev_minute})

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Dedup table so we never fire the same reminder twice in the window
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS reminders_sent "
        "(task_id INTEGER, sent_date TEXT, sent_time TEXT, "
        "PRIMARY KEY (task_id, sent_date, sent_time))"
    )
    conn.commit()

    placeholders = ",".join("?" for _ in time_window)
    cursor.execute(
        f"""SELECT t.id, t.user_id, t.task_name, COALESCE(u.timezone, 'UTC'), t.due_date, t.is_recurring
           FROM tasks t
           LEFT JOIN users u ON t.user_id = u.user_id
           WHERE t.reminder_time_utc IN ({placeholders})
           AND t.is_active = 1
           AND (t.is_recurring = 1 OR t.due_date = ? OR t.due_date IS NULL)""",
        (*time_window, today)
    )
    tasks = cursor.fetchall()

    # Filter out already-sent reminders
    due_tasks = []
    for task in tasks:
        task_id = task[0]
        cursor.execute(
            "SELECT 1 FROM reminders_sent WHERE task_id=? AND sent_date=?",
            (task_id, today)
        )
        if not cursor.fetchone():
            due_tasks.append(task)
            cursor.execute(
                "INSERT OR IGNORE INTO reminders_sent (task_id, sent_date, sent_time) VALUES (?,?,?)",
                (task_id, today, current_minute)
            )
    conn.commit()
    conn.close()
    
    for task in due_tasks:
        try:
            task_id, chat_id, task_name, tz, due_date, is_recurring = task
            local_time = utc_to_local(current_time, tz)
            recurring_note = "🔁 " if is_recurring else ""

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ Done", callback_data=f"done_{task_id}"),
                    InlineKeyboardButton("⏭ Skip", callback_data=f"skip_{task_id}"),
                ]
            ])

            await msg_send(
                context.bot, chat_id,
                f"🔔 {recurring_note}Reminder\n\n*{task_name}*\nYour time: {fmt_time(local_time)}",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Failed: {e}")

# ── PRO SUBSCRIPTION ──────────────────────────────────────────────────────────

STRIPE_SECRET_KEY    = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID      = os.environ.get("STRIPE_PRICE_ID", "")   # $8/mo price ID
BOT_USERNAME         = os.environ.get("BOT_USERNAME", "ping_bot")
WEBAPP_URL           = os.environ.get("WEBAPP_URL", "")         # e.g. https://yourapp.railway.app

PRO_XP_MULTIPLIER   = 1.5
PRO_FREEZE_ALLOTMENT = 2   # freeze tokens granted per billing period
PRO_MONTHLY_PRICE   = 8

def init_stripe():
    if STRIPE_SECRET_KEY:
        import stripe as _stripe
        _stripe.api_key = STRIPE_SECRET_KEY
        return _stripe
    return None

def is_pro(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        """SELECT pro_status, pro_expires_at FROM subscriptions
           WHERE user_id = ? AND pro_status = 'active'
           AND (pro_expires_at IS NULL OR pro_expires_at > ?)""",
        (user_id, datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'))
    )
    row = cursor.fetchone()
    conn.close()
    return row is not None

def get_subscription(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    cols = ['user_id','stripe_customer_id','stripe_subscription_id',
            'pro_status','pro_expires_at','freeze_tokens','created_at']
    return dict(zip(cols, row))

def set_pro_active(user_id, customer_id, subscription_id, expires_at=None):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO subscriptions
               (user_id, stripe_customer_id, stripe_subscription_id, pro_status, pro_expires_at, freeze_tokens)
           VALUES (?, ?, ?, 'active', ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               stripe_customer_id=excluded.stripe_customer_id,
               stripe_subscription_id=excluded.stripe_subscription_id,
               pro_status='active',
               pro_expires_at=excluded.pro_expires_at,
               freeze_tokens=CASE WHEN freeze_tokens < ? THEN ? ELSE freeze_tokens END""",
        (user_id, customer_id, subscription_id, expires_at,
         PRO_FREEZE_ALLOTMENT, PRO_FREEZE_ALLOTMENT, PRO_FREEZE_ALLOTMENT)
    )
    conn.commit()
    conn.close()

def set_pro_cancelled(user_id, expires_at):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        """UPDATE subscriptions SET pro_status='cancelled', pro_expires_at=?
           WHERE user_id=?""",
        (expires_at, user_id)
    )
    conn.commit()
    conn.close()

def use_freeze_token(user_id):
    """Use one freeze token to protect today's streak. Returns True if used successfully."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT freeze_tokens FROM subscriptions WHERE user_id=? AND pro_status='active'",
        (user_id,)
    )
    row = cursor.fetchone()
    if not row or row[0] < 1:
        conn.close()
        return False
    # Mark today as completed to protect streak without actual task completion
    cursor.execute(
        """UPDATE user_stats SET last_completed_date=? WHERE user_id=?""",
        (datetime.utcnow().strftime('%Y-%m-%d'), user_id)
    )
    cursor.execute(
        "UPDATE subscriptions SET freeze_tokens=freeze_tokens-1 WHERE user_id=?",
        (user_id,)
    )
    conn.commit()
    conn.close()
    return True

def get_freeze_tokens(user_id):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT freeze_tokens FROM subscriptions WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else 0

# ── END PRO SUBSCRIPTION ──────────────────────────────────────────────────────
# ── GAMIFICATION COMMANDS ─────────────────────────────────────────────────────

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    stats = get_or_create_stats(user_id)
    level_info = get_level(stats['xp'])
    xp_needed, next_level_name = xp_to_next_level(stats['xp'])

    streak_fire = "🔥" * min(stats['streak_current'], 5)
    if not streak_fire:
        streak_fire = "—"

    if xp_needed > 0:
        progress_line = f"📈 {stats['xp']} XP → {xp_needed} XP to {next_level_name}"
    else:
        progress_line = f"📈 {stats['xp']} XP — Max level reached! 👑"

    await msg_reply(update,
        f"⚡ *Your Stats*\n\n"
        f"🏅 Level: {level_info[2]}\n"
        f"{progress_line}\n\n"
        f"🔥 Streak: {stats['streak_current']} day(s) {streak_fire}\n"
        f"🏆 Best Streak: {stats['streak_best']} days\n"
        f"✅ Tasks Done: {stats['tasks_completed']}"
        + (f"\n\n💎 *Pro Member* · 🧊 {get_freeze_tokens(user_id)} freeze token(s)" if is_pro(user_id) else "\n\n⬆️ /upgrade for Pro perks"),
        parse_mode='Markdown'
    )

async def badges_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    earned = get_all_badges(user_id)
    earned_keys = {row[0] for row in earned}

    lines = ["🏅 *Your Badges*\n"]
    for key, (emoji, name, desc) in BADGES.items():
        if key in earned_keys:
            lines.append(f"{emoji} *{name}* — {desc}")
        else:
            lines.append(f"🔒 ~~{name}~~ — {desc}")

    await msg_reply(update, "\n".join(lines), parse_mode='Markdown')

async def done_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ✅ Done button on reminders."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    task_id = int(query.data.split('_')[1])
    timezone_str = get_user_timezone(user_id)
    local_hour = datetime.now(pytz.timezone(timezone_str)).hour

    result = complete_task(user_id, task_id, local_hour=local_hour)

    streak_text = ""
    if result['is_new_day']:
        if result['streak'] > 1:
            streak_text = f"\n🔥 Streak: {result['streak']} days!"
        else:
            streak_text = "\n🔥 Streak started!"

    badge_text = format_badge_notifications(result['new_badges'])
    level_name = result['level'][2]

    await msg_edit(query, 
        f"✅ *Done!* +{result['xp_earned']} XP{streak_text}\n"
        f"⚡ {result['total_xp']} XP · {level_name}"
        f"{badge_text}",
        parse_mode='Markdown'
    )

async def skip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle ⏭ Skip button on reminders (no XP penalty, just dismiss)."""
    query = update.callback_query
    await query.answer()
    await msg_edit(query, "⏭ Skipped.")

# ── END GAMIFICATION COMMANDS ─────────────────────────────────────────────────

# ── PRO COMMANDS ──────────────────────────────────────────────────────────────

async def upgrade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_pro(user_id):
        sub = get_subscription(user_id)
        tokens = sub['freeze_tokens'] if sub else 0
        expires = sub['pro_expires_at'] if sub else "—"
        await msg_reply(update,
            f"💎 *You're already Pro!*\n\n"
            f"🧊 Freeze tokens: {tokens}\n"
            f"📅 Renews: {expires or 'Monthly'}\n\n"
            f"Use /freeze to protect your streak on a rest day.\n"
            f"Use /cancel_pro to cancel your subscription.",
            parse_mode='Markdown'
        )
        return

    stripe = init_stripe()
    if not stripe or not STRIPE_PRICE_ID or not WEBAPP_URL:
        # Fallback: no Stripe configured yet
        await msg_reply(update,
            f"💎 *PingBot Pro — ${PRO_MONTHLY_PRICE}/month*\n\n"
            f"✨ *Pro features:*\n"
            f"• 🧊 Streak Freeze (2 tokens/mo) — protect your streak on rest days\n"
            f"• ⚡ 1.5x XP on every task\n"
            f"• 🤖 AI goal breakdown — split big goals into daily steps\n"
            f"• 💎 Exclusive Pro badge\n\n"
            f"_Stripe not yet configured. Set STRIPE_SECRET_KEY, STRIPE_PRICE_ID, and WEBAPP_URL env vars._",
            parse_mode='Markdown'
        )
        return

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            mode='subscription',
            line_items=[{'price': STRIPE_PRICE_ID, 'quantity': 1}],
            success_url=f"{WEBAPP_URL}/success?session_id={{CHECKOUT_SESSION_ID}}&user_id={user_id}",
            cancel_url=f"{WEBAPP_URL}/cancel",
            metadata={'telegram_user_id': str(user_id)},
            client_reference_id=str(user_id),
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Subscribe for $8/mo", url=session.url)]
        ])
        await msg_reply(update,
            f"💎 *PingBot Pro — ${PRO_MONTHLY_PRICE}/month*\n\n"
            f"✨ *What you get:*\n"
            f"• 🧊 Streak Freeze tokens (2/mo)\n"
            f"• ⚡ 1.5× XP multiplier\n"
            f"• 🤖 AI goal breakdown\n"
            f"• 💎 Exclusive Pro badge\n\n"
            f"Tap below to subscribe securely via Stripe:",
            reply_markup=keyboard,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.error(f"Stripe session error: {e}")
        await msg_reply(update, "❌ Payment link failed. Try again later.")


async def cancel_pro_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_pro(user_id):
        await msg_reply(update, "You don't have an active Pro subscription.")
        return

    stripe = init_stripe()
    sub = get_subscription(user_id)
    if stripe and sub and sub['stripe_subscription_id']:
        try:
            stripe_sub = stripe.Subscription.modify(
                sub['stripe_subscription_id'],
                cancel_at_period_end=True
            )
            period_end = datetime.utcfromtimestamp(
                stripe_sub['current_period_end']
            ).strftime('%Y-%m-%d')
            set_pro_cancelled(user_id, period_end)
            await msg_reply(update,
                f"✅ Subscription cancelled.\n"
                f"You keep Pro access until *{period_end}*.",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Cancel error: {e}")
            await msg_reply(update, "❌ Cancellation failed. Try again later.")
    else:
        await msg_reply(update,
            "⚠️ Contact support to cancel — no Stripe ID found."
        )


async def freeze_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_pro(user_id):
        await msg_reply(update,
            "🧊 Streak Freeze is a *Pro* feature.\n"
            "Upgrade with /upgrade to protect your streak on rest days.",
            parse_mode='Markdown'
        )
        return

    tokens = get_freeze_tokens(user_id)
    if tokens < 1:
        await msg_reply(update,
            "🧊 No freeze tokens left this month.\n"
            "You get 2 fresh tokens on your next billing date."
        )
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🧊 Yes, freeze today", callback_data="freeze_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="freeze_cancel"),
        ]
    ])
    await msg_reply(update,
        f"🧊 *Streak Freeze*\n\n"
        f"This will protect your streak for today without completing a task.\n"
        f"Tokens remaining: {tokens}\n\n"
        f"Use one now?",
        reply_markup=keyboard,
        parse_mode='Markdown'
    )


async def freeze_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == "freeze_cancel":
        await msg_edit(query, "❌ Cancelled.")
        return

    success = use_freeze_token(user_id)
    if success:
        tokens_left = get_freeze_tokens(user_id)
        stats = get_or_create_stats(user_id)
        await msg_edit(query, 
            f"🧊 *Streak frozen!*\n\n"
            f"🔥 Streak protected: {stats['streak_current']} days\n"
            f"Tokens remaining: {tokens_left}",
            parse_mode='Markdown'
        )
    else:
        await msg_edit(query, "❌ No freeze tokens available.")


async def pro_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show Pro status inline in /stats if applicable."""
    pass  # Handled inside stats_cmd

# ── END PRO COMMANDS ──────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route('/')
def home():
    return "<h1>PingBot</h1><p><a href='/status'>Status</a></p>"

@app.route('/status')
def status():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM users")
    users = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM tasks WHERE is_active=1")
    tasks = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM subscriptions WHERE pro_status='active'")
    pros = cursor.fetchone()[0]
    conn.close()
    return f"<h2>PingBot Status</h2><p>Users: {users} | Active tasks: {tasks} | Pro members: {pros}</p>"

@app.route('/success')
def stripe_success():
    return "<h2>✅ Payment successful!</h2><p>Return to Telegram and send /stats to see your Pro status.</p>"

@app.route('/cancel')
def stripe_cancel():
    return "<h2>Payment cancelled.</h2><p>Return to Telegram whenever you're ready to upgrade.</p>"

@app.route('/stripe/webhook', methods=['POST'])
def stripe_webhook():
    import stripe as _stripe
    from flask import request, jsonify
    _stripe.api_key = STRIPE_SECRET_KEY
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature', '')
    try:
        event = _stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({'error': str(e)}), 400

    etype = event['type']
    data = event['data']['object']

    if etype == 'checkout.session.completed':
        user_id = int(data.get('client_reference_id', 0))
        customer_id = data.get('customer', '')
        subscription_id = data.get('subscription', '')
        if user_id:
            set_pro_active(user_id, customer_id, subscription_id)
            logger.info(f"Pro activated for user {user_id}")

    elif etype in ('customer.subscription.updated', 'invoice.paid'):
        subscription_id = data.get('id') or data.get('subscription', '')
        customer_id = data.get('customer', '')
        # Find user by subscription ID
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_id FROM subscriptions WHERE stripe_subscription_id=? OR stripe_customer_id=?",
            (subscription_id, customer_id)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            period_end = data.get('current_period_end')
            expires = datetime.utcfromtimestamp(period_end).strftime('%Y-%m-%d %H:%M:%S') if period_end else None
            set_pro_active(row[0], customer_id, subscription_id, expires)

    elif etype == 'customer.subscription.deleted':
        customer_id = data.get('customer', '')
        period_end = data.get('current_period_end')
        expires = datetime.utcfromtimestamp(period_end).strftime('%Y-%m-%d %H:%M:%S') if period_end else None
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE subscriptions SET pro_status='cancelled', pro_expires_at=? WHERE stripe_customer_id=?",
            (expires, customer_id)
        )
        conn.commit()
        conn.close()

    return jsonify({'status': 'ok'})

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
    tz_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(timezone_callback, pattern='^tz_')],
        states={
            CUSTOM_TZ_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, custom_timezone_input)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )
    application.add_handler(CommandHandler('timezone', timezone_cmd))
    application.add_handler(tz_conv)
    application.add_handler(CommandHandler('list', list_tasks))
    application.add_handler(CommandHandler('delete', delete_start))
    application.add_handler(CommandHandler('stats', stats_cmd))
    application.add_handler(CommandHandler('badges', badges_cmd))
    application.add_handler(CommandHandler('upgrade', upgrade_cmd))
    application.add_handler(CommandHandler('cancel_pro', cancel_pro_cmd))
    application.add_handler(CommandHandler('freeze', freeze_cmd))
    application.add_handler(CallbackQueryHandler(freeze_callback, pattern='^freeze_'))
    application.add_handler(add_conv)
    application.add_handler(CallbackQueryHandler(delete_callback, pattern='^del_'))
    application.add_handler(CallbackQueryHandler(done_callback, pattern='^done_'))
    application.add_handler(CallbackQueryHandler(skip_callback, pattern='^skip_'))
    
    application.job_queue.run_repeating(check_reminders, interval=60, first=10)
    
    logger.info("Bot started!")
    application.run_polling()

if __name__ == '__main__':
    threading.Thread(target=run_flask).start()
    main()
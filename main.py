import logging
import asyncio
import sys
import os
import fcntl
import time
import signal
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from config import BOT_TOKEN, OWNER_ID, REPORT_CATEGORIES, REPORT_TEMPLATES, DEFAULT_TOKENS, REPORT_COST
from database import init_db, get_db
from account_manager import AccountManager
from reporter import Reporter
from models import User, TelegramAccount, Report
from utils import (
    validate_phone_number,
    validate_verification_code,
    validate_target_username,
    parse_targets,
    format_tokens,
    time_ago,
    format_datetime,
    truncate_text,
    stats,
    get_utc_now
)
from datetime import datetime, timezone

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize components as global variables
db = None
account_manager = None
reporter = None
application = None
shutdown_event = asyncio.Event()
lock_file = None
lock_file_path = '/tmp/bot.lock'

def acquire_lock():
    """Acquire a lock file to prevent multiple instances"""
    global lock_file
    try:
        # Check if lock file exists and is stale
        if os.path.exists(lock_file_path):
            try:
                # Try to read the PID from the lock file
                with open(lock_file_path, 'r') as f:
                    old_pid = int(f.read().strip())
                
                # Check if process with that PID is still running
                try:
                    os.kill(old_pid, 0)  # Signal 0 just checks if process exists
                    logger.error(f"❌ Another bot instance is already running with PID {old_pid}")
                    return False
                except OSError:
                    # Process not running, lock is stale
                    logger.info(f"Removing stale lock file from PID {old_pid}")
                    os.remove(lock_file_path)
            except (ValueError, IOError):
                # Invalid lock file, remove it
                try:
                    os.remove(lock_file_path)
                except:
                    pass
        
        # Create new lock file
        lock_file = open(lock_file_path, 'w')
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        logger.info(f"✅ Lock acquired (PID: {os.getpid()})")
        return True
    except (IOError, OSError) as e:
        logger.error(f"❌ Failed to acquire lock: {e}")
        return False

def release_lock():
    """Release the lock file"""
    global lock_file
    if lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()
            if os.path.exists(lock_file_path):
                os.remove(lock_file_path)
            logger.info("✅ Lock released")
        except Exception as e:
            logger.error(f"Error releasing lock: {e}")

def signal_handler(sig, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {sig}, initiating shutdown...")
    if loop and loop.is_running():
        loop.call_soon_threadsafe(shutdown_event.set)

# ==================== COMMAND HANDLERS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    user = update.effective_user
    db_session = db.get_session()
    
    try:
        # Use a fresh session for each operation
        db_user = db_session.query(User).filter_by(user_id=user.id).first()
        
        if not db_user:
            role = 'owner' if user.id == OWNER_ID else 'user'
            db_user = User(
                user_id=user.id,
                username=user.username,
                tokens=999999 if role == 'owner' else DEFAULT_TOKENS,
                role=role
            )
            db_session.add(db_user)
            db_session.commit()
            logger.info(f"New user registered: {user.id}")
            stats.increment('users_registered')
        
        # Update last active - use update() to avoid detached instance issues
        db_session.query(User).filter_by(user_id=user.id).update(
            {"last_active": get_utc_now()}
        )
        db_session.commit()
        
    except Exception as e:
        logger.error(f"Start error: {e}")
        db_session.rollback()
        await update.message.reply_text("❌ Database error. Please try again.")
        return
    finally:
        db_session.close()
    
    # Get fresh user data for display
    db_session = db.get_session()
    try:
        db_user = db_session.query(User).filter_by(user_id=user.id).first()
        
        # Create menu
        keyboard = [
            [InlineKeyboardButton("📊 My Stats", callback_data='stats')],
            [InlineKeyboardButton("📝 Report", callback_data='report_menu')],
            [InlineKeyboardButton("💰 Buy Tokens", callback_data='buy_tokens')],
            [InlineKeyboardButton("👥 My Reports", callback_data='my_reports')],
            [InlineKeyboardButton("📱 Add Account", callback_data='add_account')]
        ]
        
        if db_user.role in ['owner', 'admin']:
            keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data='admin_panel')])
        if db_user.role == 'owner':
            keyboard.append([InlineKeyboardButton("👑 Owner Panel", callback_data='owner_panel')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        welcome_text = f"""
🌟 **Welcome {user.first_name}!** 🌟

━━━━━━━━━━━━━━━━━━━━━
📋 **Your Information**
━━━━━━━━━━━━━━━━━━━━━
🆔 ID: `{user.id}`
💰 Tokens: {format_tokens(db_user.tokens)}
👤 Role: `{db_user.role}`
📊 Reports Made: `{db_user.reports_made}`
━━━━━━━━━━━━━━━━━━━━━

Select an option below:
"""
        await update.message.reply_text(welcome_text, reply_markup=reply_markup, parse_mode='Markdown')
    finally:
        db_session.close()

# ==================== CALLBACK HANDLERS ====================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle button presses"""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    db_session = db.get_session()
    
    try:
        db_user = db_session.query(User).filter_by(user_id=user_id).first()
        
        if not db_user:
            await query.edit_message_text("❌ User not found. Please use /start to register.")
            return
        
        # Update last active using update() to avoid detached instance issues
        db_session.query(User).filter_by(user_id=user_id).update(
            {"last_active": get_utc_now()}
        )
        db_session.commit()
        
    except Exception as e:
        logger.error(f"Database error: {e}")
        db_session.rollback()
        await query.edit_message_text("❌ Database error. Please try again.")
        return
    finally:
        db_session.close()
    
    # Handle different callback data
    if query.data == 'stats':
        await show_stats(query, db_user)
    
    elif query.data == 'report_menu':
        await show_report_menu(query)
    
    elif query.data.startswith('report_cat_'):
        category = query.data.replace('report_cat_', '')
        context.user_data['report_category'] = category
        context.user_data['report_template'] = REPORT_TEMPLATES.get(category, "")
        await show_category_options(query, category)
    
    elif query.data == 'use_template':
        context.user_data['report_text'] = context.user_data['report_template']
        await query.edit_message_text(
            "📝 **Send Target Information**\n\n"
            "Please send me the username(s) or ID(s) of the target(s) to report.\n"
            "You can send multiple by separating with commas or new lines.\n\n"
            "**Examples:**\n"
            "• `@spam_channel`\n"
            "• `-1001234567890`\n"
            "• `@user1, @user2, @user3`"
        )
        context.user_data['awaiting_target'] = True
    
    elif query.data == 'custom_text':
        await query.edit_message_text(
            "✏️ **Send Custom Report Text**\n\n"
            "Please write your custom report message.\n"
            "Be detailed and specific about the violation:"
        )
        context.user_data['awaiting_custom_text'] = True
    
    elif query.data == 'buy_tokens':
        await show_buy_tokens(query, db_user)
    
    elif query.data.startswith('buy_'):
        amount = int(query.data.replace('buy_', ''))
        await query.edit_message_text(
            f"💳 **Purchase {amount} Tokens**\n\n"
            f"To purchase {amount} tokens, please contact @admin\n\n"
            "Payment integration coming soon!\n\n"
            "For now, tokens can be added by admins only."
        )
    
    elif query.data == 'my_reports':
        await show_my_reports(query, user_id)
    
    elif query.data.startswith('report_status_'):
        report_id = int(query.data.replace('report_status_', ''))
        await show_report_status(query, report_id)
    
    elif query.data == 'add_account':
        await query.edit_message_text(
            "📱 **Add Telegram Account**\n\n"
            "Please send me your phone number in international format:\n\n"
            "**Example:** `+1234567890`\n\n"
            "⚠️ This account will be used for reporting content."
        )
        context.user_data['awaiting_phone'] = True
    
    elif query.data == 'resend_code':
        await handle_resend_code(query, context)
    
    elif query.data == 'admin_panel':
        if db_user.role not in ['owner', 'admin']:
            await query.edit_message_text("⛔ **Access Denied!**")
            return
        await show_admin_panel(query)
    
    elif query.data == 'admin_users':
        if db_user.role not in ['owner', 'admin']:
            await query.edit_message_text("⛔ Access Denied!")
            return
        await show_admin_users(query)
    
    elif query.data == 'admin_accounts':
        if db_user.role not in ['owner', 'admin']:
            await query.edit_message_text("⛔ Access Denied!")
            return
        await show_admin_accounts(query)
    
    elif query.data == 'admin_reports':
        if db_user.role not in ['owner', 'admin']:
            await query.edit_message_text("⛔ Access Denied!")
            return
        await show_admin_reports(query)
    
    elif query.data == 'admin_give_tokens':
        if db_user.role not in ['owner', 'admin']:
            await query.edit_message_text("⛔ Access Denied!")
            return
        await query.edit_message_text(
            "💰 **Give Tokens to User**\n\n"
            "Please send the user ID and amount in this format:\n"
            "`USER_ID AMOUNT`\n\n"
            "Example: `123456789 50`"
        )
        context.user_data['awaiting_token_gift'] = True
    
    elif query.data == 'owner_panel':
        if db_user.role != 'owner':
            await query.edit_message_text("⛔ **Access Denied!**")
            return
        await show_owner_panel(query)
    
    elif query.data == 'owner_stats':
        if db_user.role != 'owner':
            await query.edit_message_text("⛔ Access Denied!")
            return
        await show_owner_stats(query)
    
    elif query.data == 'owner_add_tokens':
        if db_user.role != 'owner':
            await query.edit_message_text("⛔ Access Denied!")
            return
        await query.edit_message_text(
            "💰 **Add Tokens to User**\n\n"
            "Please send the user ID and amount in this format:\n"
            "`USER_ID AMOUNT`\n\n"
            "Example: `123456789 1000`"
        )
        context.user_data['awaiting_owner_token_add'] = True
    
    elif query.data == 'owner_add_admin':
        if db_user.role != 'owner':
            await query.edit_message_text("⛔ Access Denied!")
            return
        await query.edit_message_text(
            "👑 **Add Admin**\n\n"
            "Please send the user ID to make admin:\n"
            "Example: `123456789`"
        )
        context.user_data['awaiting_admin_add'] = True
    
    elif query.data == 'confirm_report':
        await handle_confirm_report(query, context, user_id, db_user)
    
    elif query.data == 'back_to_main':
        await handle_back_to_main(query, context, user_id)

# ==================== CALLBACK HELPER FUNCTIONS ====================

async def show_stats(query, db_user):
    """Show user statistics"""
    joined_date = format_datetime(db_user.joined_date, '%Y-%m-%d')
    last_active = time_ago(db_user.last_active)
    
    stats_text = f"""
📊 **Your Statistics**

━━━━━━━━━━━━━━━━━━━━━
🆔 User ID: `{db_user.user_id}`
👤 Username: @{db_user.username or 'N/A'}
💰 Tokens: {format_tokens(db_user.tokens)}
👑 Role: `{db_user.role}`
📊 Reports Made: `{db_user.reports_made}`
📅 Joined: `{joined_date}`
⏰ Last Active: {last_active}
━━━━━━━━━━━━━━━━━━━━━
"""
    keyboard = [[InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(stats_text, reply_markup=reply_markup, parse_mode='Markdown')

async def show_report_menu(query):
    """Show report categories menu"""
    keyboard = []
    for key, value in REPORT_CATEGORIES.items():
        keyboard.append([InlineKeyboardButton(value, callback_data=f'report_cat_{key}')])
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data='back_to_main')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "📝 **Select Report Category**\n\nChoose the type of violation:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def show_category_options(query, category):
    """Show options for selected category"""
    keyboard = [
        [InlineKeyboardButton("📝 Use Template", callback_data='use_template')],
        [InlineKeyboardButton("✏️ Custom Text", callback_data='custom_text')],
        [InlineKeyboardButton("🔙 Back", callback_data='report_menu')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"📋 **Category:** {REPORT_CATEGORIES[category]}\n\n"
        f"**Template:**\n`{REPORT_TEMPLATES[category]}`\n\n"
        "Choose an option:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def show_buy_tokens(query, db_user):
    """Show token purchase options"""
    keyboard = [
        [InlineKeyboardButton("🔟 10 Tokens - $1", callback_data='buy_10')],
        [InlineKeyboardButton("5️⃣0️⃣ 50 Tokens - $4", callback_data='buy_50')],
        [InlineKeyboardButton("1️⃣0️⃣0️⃣ 100 Tokens - $7", callback_data='buy_100')],
        [InlineKeyboardButton("5️⃣0️⃣0️⃣ 500 Tokens - $30", callback_data='buy_500')],
        [InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        f"💰 **Buy Tokens**\n\n"
        f"Your current tokens: {format_tokens(db_user.tokens)}\n\n"
        "Select a package:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def show_my_reports(query, user_id):
    """Show user's recent reports"""
    db_session = db.get_session()
    try:
        reports = await reporter.get_user_reports(user_id, limit=10)
        
        if not reports:
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text("📭 You haven't made any reports yet.", reply_markup=reply_markup)
            return
        
        text = "📋 **Your Recent Reports:**\n\n"
        for report in reports:
            status_emoji = "✅" if report['status'] == 'completed' else "⏳" if report['status'] == 'pending' else "❌"
            text += f"{status_emoji} **ID:** `{report['id']}`\n"
            text += f"   **Target:** `{report['target']}`\n"
            text += f"   **Status:** {report['status']}\n"
            if report['error']:
                text += f"   **Error:** `{truncate_text(report['error'], 50)}`\n"
            text += f"   **Date:** {format_datetime(datetime.fromisoformat(report['created_at']))}\n"
            text += "━━━━━━━━━━━━━━━━━━━━━\n"
        
        # Add view buttons
        view_buttons = []
        for report in reports[:5]:
            view_buttons.append([InlineKeyboardButton(f"🔍 View Report {report['id']}", callback_data=f'report_status_{report["id"]}')])
        
        view_buttons.append([InlineKeyboardButton("🔙 Back", callback_data='back_to_main')])
        reply_markup = InlineKeyboardMarkup(view_buttons)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
        
    finally:
        db_session.close()

async def show_report_status(query, report_id):
    """Show detailed report status"""
    result = await reporter.get_report_status(report_id)
    
    if result['status'] == 'success':
        report = result['report']
        status_emoji = "✅" if report['status'] == 'completed' else "⏳" if report['status'] == 'pending' else "❌"
        
        text = f"""
{status_emoji} **Report Details**

**ID:** `{report['id']}`
**Target:** `{report['target']}`
**Category:** {report['category']}
**Status:** {report['status']}
**Created:** {report['created_at']}
"""
        if report['completed_at']:
            text += f"**Completed:** {report['completed_at']}\n"
        if report['error']:
            text += f"**Error:** `{report['error']}`\n"
        
        keyboard = [[InlineKeyboardButton("🔙 Back to Reports", callback_data='my_reports')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await query.edit_message_text(f"❌ Error: {result['message']}")

async def handle_resend_code(query, context):
    """Handle code resend request"""
    phone = context.user_data.get('phone')
    if phone:
        await query.edit_message_text("🔄 **Cleaning up and resending verification code...**")
        
        await account_manager.cancel_login(phone)
        await asyncio.sleep(2)
        
        result = await account_manager.resend_code(phone)
        if result['status'] == 'code_sent':
            context.user_data['awaiting_code'] = True
            await query.edit_message_text(
                "📱 **New Verification Code Sent!**\n\n"
                "Please enter the new 5-digit code:\n"
                "**Example:** `12345`\n\n"
                "⏰ **Note:** Code expires in 2 minutes."
            )
        else:
            keyboard = [[InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"❌ **Error:** {result.get('error', 'Failed to resend code')}",
                reply_markup=reply_markup
            )
    else:
        await query.edit_message_text("❌ Session expired. Please start over.")
        context.user_data.clear()

async def show_admin_panel(query):
    """Show admin panel"""
    db_session = db.get_session()
    try:
        total_users = db_session.query(User).count()
        total_accounts = db_session.query(TelegramAccount).count()
        active_accounts = db_session.query(TelegramAccount).filter_by(is_active=True).count()
        pending_reports = db_session.query(Report).filter_by(status='pending').count()
        
        keyboard = [
            [InlineKeyboardButton("👥 Users", callback_data='admin_users')],
            [InlineKeyboardButton("📱 Accounts", callback_data='admin_accounts')],
            [InlineKeyboardButton("📊 Reports", callback_data='admin_reports')],
            [InlineKeyboardButton("💰 Give Tokens", callback_data='admin_give_tokens')],
            [InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        text = f"""
⚙️ **Admin Panel**

━━━━━━━━━━━━━━━━━━━━━
📊 **Statistics**
━━━━━━━━━━━━━━━━━━━━━
👥 Total Users: `{total_users}`
📱 Total Accounts: `{total_accounts}`
✅ Active Accounts: `{active_accounts}`
⏳ Pending Reports: `{pending_reports}`
━━━━━━━━━━━━━━━━━━━━━
"""
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    finally:
        db_session.close()

async def show_admin_users(query):
    """Show recent users for admin"""
    db_session = db.get_session()
    try:
        users = db_session.query(User).order_by(User.joined_date.desc()).limit(10).all()
        text = "👥 **Recent Users:**\n\n"
        for user in users:
            text += f"🆔 `{user.user_id}` | @{user.username or 'N/A'} | {user.role} | {format_tokens(user.tokens)}\n"
        
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data='admin_panel')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    finally:
        db_session.close()

async def show_admin_accounts(query):
    """Show Telegram accounts for admin"""
    db_session = db.get_session()
    try:
        accounts = db_session.query(TelegramAccount).limit(10).all()
        text = "📱 **Telegram Accounts:**\n\n"
        for acc in accounts:
            status_emoji = "✅" if acc.is_active else "❌"
            text += f"{status_emoji} `{acc.phone_number}` | Reports: {acc.reports_count} | Status: {acc.status}\n"
        
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data='admin_panel')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    finally:
        db_session.close()

async def show_admin_reports(query):
    """Show recent reports for admin"""
    db_session = db.get_session()
    try:
        reports = db_session.query(Report).order_by(Report.created_at.desc()).limit(10).all()
        text = "📊 **Recent Reports:**\n\n"
        for report in reports:
            status_emoji = "✅" if report.status == 'completed' else "⏳" if report.status == 'pending' else "❌"
            text += f"{status_emoji} ID: `{report.id}` | Target: {report.target_username or report.target_id} | Status: {report.status}\n"
        
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data='admin_panel')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    finally:
        db_session.close()

async def show_owner_panel(query):
    """Show owner panel"""
    keyboard = [
        [InlineKeyboardButton("💰 Add Tokens", callback_data='owner_add_tokens')],
        [InlineKeyboardButton("👑 Add Admin", callback_data='owner_add_admin')],
        [InlineKeyboardButton("📊 System Stats", callback_data='owner_stats')],
        [InlineKeyboardButton("⚙️ Settings", callback_data='owner_settings')],
        [InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("👑 **Owner Panel**", reply_markup=reply_markup, parse_mode='Markdown')

async def show_owner_stats(query):
    """Show system statistics for owner"""
    db_session = db.get_session()
    try:
        total_users = db_session.query(User).count()
        total_accounts = db_session.query(TelegramAccount).count()
        total_reports = db_session.query(Report).count()
        pending_reports = db_session.query(Report).filter_by(status='pending').count()
        completed_reports = db_session.query(Report).filter_by(status='completed').count()
        
        account_stats = await account_manager.get_account_stats()
        all_stats = stats.get_all()
        
        text = f"""
📊 **System Statistics**

━━━━━━━━━━━━━━━━━━━━━
👥 **Users:** `{total_users}`
📱 **Accounts:** `{total_accounts}`
   ├─ Active: `{account_stats['active']}`
   ├─ Available: `{account_stats['available']}`
   └─ Banned: `{account_stats['banned']}`

📋 **Reports:** `{total_reports}`
   ├─ Pending: `{pending_reports}`
   └─ Completed: `{completed_reports}`

📈 **Bot Stats**
   ├─ Reports Submitted: `{all_stats['reports_submitted']}`
   ├─ Reports Completed: `{all_stats['reports_completed']}`
   ├─ Accounts Added: `{all_stats['accounts_added']}`
   └─ Uptime: `{all_stats['uptime_formatted']}`
━━━━━━━━━━━━━━━━━━━━━
"""
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data='owner_panel')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode='Markdown')
    finally:
        db_session.close()

async def handle_confirm_report(query, context, user_id, db_user):
    """Handle report confirmation"""
    targets = context.user_data.get('targets', [])
    category = context.user_data.get('report_category')
    report_text = context.user_data.get('report_text')
    
    if not targets or not category or not report_text:
        await query.edit_message_text("❌ Missing report information. Please start over.")
        context.user_data.clear()
        return
    
    await query.edit_message_text("🔄 **Processing reports...**\nThis may take a few minutes.")
    stats.increment('reports_submitted', len(targets))
    
    result = await reporter.bulk_report(targets, category, report_text, user_id)
    
    if result['status'] == 'success':
        report_ids = result['report_ids']
        stats.increment('reports_completed', result['summary']['successful'])
        
        # Create detailed response
        response_text = f"✅ **Successfully submitted {len(report_ids)} reports!**\n\n"
        response_text += f"**Report IDs:**\n`{', '.join(map(str, report_ids))}`\n\n"
        
        if 'summary' in result:
            response_text += f"**Summary:**\n"
            response_text += f"• Total: {result['summary']['total']}\n"
            response_text += f"• Successful: {result['summary']['successful']}\n"
            if result['summary']['failed'] > 0:
                response_text += f"• Failed: {result['summary']['failed']}\n"
        
        # Add view buttons
        view_buttons = []
        for report_id in report_ids[:5]:
            view_buttons.append([InlineKeyboardButton(f"🔍 View Report {report_id}", callback_data=f'report_status_{report_id}')])
        
        view_buttons.append([InlineKeyboardButton("🔙 Back to Main", callback_data='back_to_main')])
        reply_markup = InlineKeyboardMarkup(view_buttons)
        
        await query.edit_message_text(response_text, reply_markup=reply_markup, parse_mode='Markdown')
        
    else:
        stats.increment('reports_failed', len(targets))
        keyboard = [[InlineKeyboardButton("🔙 Back", callback_data='back_to_main')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            f"❌ **Error:** {result['message']}",
            reply_markup=reply_markup
        )
    
    context.user_data.clear()

async def handle_back_to_main(query, context, user_id):
    """Handle back to main menu"""
    context.user_data.clear()
    db_session = db.get_session()
    
    try:
        db_user = db_session.query(User).filter_by(user_id=user_id).first()
        if not db_user:
            await query.edit_message_text("❌ User not found. Please use /start.")
            return
        
        # Create main menu
        keyboard = [
            [InlineKeyboardButton("📊 My Stats", callback_data='stats')],
            [InlineKeyboardButton("📝 Report", callback_data='report_menu')],
            [InlineKeyboardButton("💰 Buy Tokens", callback_data='buy_tokens')],
            [InlineKeyboardButton("👥 My Reports", callback_data='my_reports')],
            [InlineKeyboardButton("📱 Add Account", callback_data='add_account')]
        ]
        
        if db_user.role in ['owner', 'admin']:
            keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data='admin_panel')])
        if db_user.role == 'owner':
            keyboard.append([InlineKeyboardButton("👑 Owner Panel", callback_data='owner_panel')])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(
            f"🌟 **Welcome back!** 🌟\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Tokens: {format_tokens(db_user.tokens)}\n"
            f"👤 Role: `{db_user.role}`\n"
            f"📊 Reports: `{db_user.reports_made}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Select an option below:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )
    finally:
        db_session.close()

# ==================== MESSAGE HANDLERS ====================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages - FIXED NoneType error"""
    # Check if this is a valid message with text
    if not update.message or not update.message.text:
        logger.debug("Received update without text message, ignoring")
        return
    
    user_id = update.effective_user.id
    text = update.message.text
    db_session = db.get_session()
    
    try:
        # Handle token gifting
        if context.user_data.get('awaiting_token_gift'):
            await handle_token_gift(update, context, text, db_session)
            return
        
        # Handle owner adding tokens
        elif context.user_data.get('awaiting_owner_token_add'):
            await handle_owner_token_add(update, context, text, db_session)
            return
        
        # Handle owner adding admin
        elif context.user_data.get('awaiting_admin_add'):
            await handle_admin_add(update, context, text, db_session)
            return
        
        # Handle phone number
        elif context.user_data.get('awaiting_phone'):
            await handle_phone_input(update, context, text)
            return
        
        # Handle verification code
        elif context.user_data.get('awaiting_code'):
            await handle_code_input(update, context, text)
            return
        
        # Handle 2FA password
        elif context.user_data.get('awaiting_password'):
            await handle_password_input(update, context, text)
            return
        
        # Handle custom report text
        elif context.user_data.get('awaiting_custom_text'):
            await handle_custom_text(update, context, text)
            return
        
        # Handle target input
        elif context.user_data.get('awaiting_target'):
            await handle_target_input(update, context, text, user_id, db_session)
            return
        
    finally:
        db_session.close()

# ==================== MESSAGE HELPER FUNCTIONS ====================

async def handle_phone_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Handle phone number input"""
    phone = text.strip()
    
    # Validate phone number
    is_valid, error = validate_phone_number(phone)
    if not is_valid:
        await update.message.reply_text(f"❌ {error}")
        return
    
    context.user_data['phone'] = phone
    context.user_data['awaiting_phone'] = False
    context.user_data['awaiting_code'] = True
    
    await update.message.chat.send_action(action='typing')
    
    result = await account_manager.add_account(phone)
    
    if result['status'] == 'code_sent':
        await update.message.reply_text(
            "📱 **Verification Code Sent!**\n\n"
            "Please enter the 5-digit code you received:\n"
            "**Example:** `12345`\n\n"
            "⏰ **Note:** Code expires in 2 minutes."
        )
    elif result['status'] == 'flood_wait':
        wait_time = result.get('wait_time', 60)
        await update.message.reply_text(
            f"⏳ **Too Many Attempts**\n\n"
            f"Please wait {wait_time} seconds before trying again."
        )
        context.user_data.clear()
    else:
        await update.message.reply_text(
            f"❌ **Error:** {result.get('error', 'Unknown error')}"
        )
        context.user_data.clear()

async def handle_code_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Handle verification code input"""
    code = text.strip()
    phone = context.user_data.get('phone')
    
    # Validate code
    is_valid, error = validate_verification_code(code)
    if not is_valid:
        await update.message.reply_text(f"❌ {error}")
        return
    
    await update.message.chat.send_action(action='typing')
    
    result = await account_manager.add_account(phone, verification_code=code)
    
    if result['status'] == 'password_needed':
        context.user_data['awaiting_password'] = True
        await update.message.reply_text(
            "🔐 **Two-Step Verification Enabled**\n\n"
            "Please enter your account password:"
        )
    elif result['status'] == 'code_expired':
        keyboard = [
            [InlineKeyboardButton("🔄 Resend Code", callback_data='resend_code')],
            [InlineKeyboardButton("❌ Cancel", callback_data='back_to_main')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "⏰ **Verification Code Expired**\n\n"
            "Would you like a new code?",
            reply_markup=reply_markup
        )
    elif result['status'] == 'code_invalid':
        await update.message.reply_text(
            "❌ **Invalid Code**\n\n"
            "Please try again:"
        )
    elif result['status'] == 'success':
        await update.message.reply_text(
            "✅ **Account Added Successfully!**\n\n"
            "You can now use this account for reporting."
        )
        stats.increment('accounts_added')
        context.user_data.clear()
    else:
        await update.message.reply_text(
            f"❌ **Error:** {result.get('error', 'Unknown error')}"
        )
        context.user_data.clear()

async def handle_password_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Handle 2FA password input"""
    password = text
    phone = context.user_data.get('phone')
    
    await update.message.chat.send_action(action='typing')
    
    result = await account_manager.add_account(phone, password=password)
    
    if result['status'] == 'success':
        await update.message.reply_text(
            "✅ **Account Added Successfully!**\n\n"
            "You can now use this account for reporting."
        )
        stats.increment('accounts_added')
        context.user_data.clear()
    elif result['status'] == 'password_error':
        await update.message.reply_text(
            "❌ **Incorrect Password**\n\n"
            "Please try again:"
        )
    elif result['status'] == 'flood_wait':
        wait_time = result.get('wait_time', 60)
        await update.message.reply_text(
            f"⏳ **Too Many Attempts**\n\n"
            f"Please wait {wait_time} seconds."
        )
        context.user_data.clear()
    else:
        await update.message.reply_text(
            f"❌ **Error:** {result.get('error', 'Unknown error')}"
        )
        context.user_data.clear()

async def handle_custom_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Handle custom report text input"""
    context.user_data['report_text'] = text
    context.user_data['awaiting_custom_text'] = False
    context.user_data['awaiting_target'] = True
    await update.message.reply_text(
        "📝 **Send Target Information**\n\n"
        "Please send me the username(s) or ID(s) of the target(s) to report.\n"
        "You can send multiple by separating with commas or new lines.\n\n"
        "**Examples:**\n"
        "• `@spam_channel`\n"
        "• `-1001234567890`\n"
        "• `@user1, @user2, @user3`"
    )

async def handle_target_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, user_id: int, db_session):
    """Handle target input for reporting"""
    targets = parse_targets(text)
    
    if not targets:
        await update.message.reply_text("❌ No valid targets found! Please try again.")
        return
    
    # Check user tokens
    db_user = db_session.query(User).filter_by(user_id=user_id).first()
    if not db_user:
        await update.message.reply_text("❌ User not found. Please use /start.")
        return
    
    required_tokens = len(targets) * REPORT_COST
    if db_user.role != 'owner' and db_user.tokens < required_tokens:
        await update.message.reply_text(
            f"❌ **Insufficient Tokens!**\n\n"
            f"Required: `{required_tokens}` tokens\n"
            f"Your balance: {format_tokens(db_user.tokens)}"
        )
        return
    
    # Get category and report text
    category = context.user_data.get('report_category')
    report_text = context.user_data.get('report_text')
    
    if not category or not report_text:
        await update.message.reply_text("❌ Missing report information. Please start over.")
        context.user_data.clear()
        return
    
    # Show confirmation
    targets_display = "\n".join([f"• `{t.get('username') or t.get('id')}`" for t in targets[:5]])
    if len(targets) > 5:
        targets_display += f"\n• ... and {len(targets) - 5} more"
    
    confirm_text = f"""
📝 **Report Confirmation**

━━━━━━━━━━━━━━━━━━━━━
📋 **Category:** `{REPORT_CATEGORIES.get(category, category)}`
🎯 **Targets:** `{len(targets)}`
{targets_display}
💰 **Cost:** `{required_tokens}` tokens
💳 **Your Balance:** {format_tokens(db_user.tokens)}
━━━━━━━━━━━━━━━━━━━━━

**Report Text:**
`{truncate_text(report_text)}`

Proceed with reporting?
"""
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes, Proceed", callback_data='confirm_report'),
            InlineKeyboardButton("❌ Cancel", callback_data='back_to_main')
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    context.user_data['targets'] = targets
    await update.message.reply_text(confirm_text, reply_markup=reply_markup, parse_mode='Markdown')

async def handle_token_gift(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, db_session):
    """Handle admin token gift"""
    try:
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text("❌ Invalid format! Use: `USER_ID AMOUNT`")
            return
        
        target_user_id = int(parts[0])
        amount = int(parts[1])
        
        target_user = db_session.query(User).filter_by(user_id=target_user_id).first()
        if not target_user:
            await update.message.reply_text("❌ User not found!")
            return
        
        target_user.tokens += amount
        db_session.commit()
        
        await update.message.reply_text(
            f"✅ Added {format_tokens(amount)} to user {target_user_id}\n"
            f"New balance: {format_tokens(target_user.tokens)}"
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid amount! Please enter a number.")
    except Exception as e:
        logger.error(f"Token gift error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    
    context.user_data.clear()

async def handle_owner_token_add(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, db_session):
    """Handle owner adding tokens"""
    try:
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text("❌ Invalid format! Use: `USER_ID AMOUNT`")
            return
        
        target_user_id = int(parts[0])
        amount = int(parts[1])
        
        target_user = db_session.query(User).filter_by(user_id=target_user_id).first()
        if not target_user:
            target_user = User(
                user_id=target_user_id,
                username=f"user_{target_user_id}",
                tokens=amount,
                role='user'
            )
            db_session.add(target_user)
            await update.message.reply_text(f"✅ Created new user and added {format_tokens(amount)}")
        else:
            target_user.tokens += amount
            await update.message.reply_text(
                f"✅ Added {format_tokens(amount)} to user {target_user_id}\n"
                f"New balance: {format_tokens(target_user.tokens)}"
            )
        
        db_session.commit()
        
    except ValueError:
        await update.message.reply_text("❌ Invalid amount! Please enter a number.")
    except Exception as e:
        logger.error(f"Owner token add error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    
    context.user_data.clear()

async def handle_admin_add(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, db_session):
    """Handle owner adding admin"""
    try:
        target_user_id = int(text.strip())
        
        target_user = db_session.query(User).filter_by(user_id=target_user_id).first()
        if not target_user:
            await update.message.reply_text("❌ User not found!")
            return
        
        target_user.role = 'admin'
        db_session.commit()
        
        await update.message.reply_text(
            f"✅ User {target_user_id} is now an admin!"
        )
        
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID! Please enter a number.")
    except Exception as e:
        logger.error(f"Admin add error: {e}")
        await update.message.reply_text(f"❌ Error: {str(e)}")
    
    context.user_data.clear()

# ==================== ERROR HANDLER ====================

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}")
    
    try:
        if update and update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                "❌ An error occurred. Please try again or use /start"
            )
        elif update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ An error occurred. Please try again later."
            )
    except:
        pass

# ==================== MAIN FUNCTIONS ====================

async def async_main():
    """Async main function"""
    global db, account_manager, reporter, application
    
    try:
        # Initialize database
        from config import DATABASE_URL
        db = init_db(DATABASE_URL)
        logger.info("✅ Database initialized")
        
        # Initialize account manager
        account_manager = AccountManager()
        await account_manager.start()
        logger.info("✅ Account manager started")
        
        # Initialize reporter
        reporter = Reporter(account_manager)
        logger.info("✅ Reporter initialized")
        
        # Create application with custom settings to avoid conflicts
        builder = Application.builder().token(BOT_TOKEN)
        
        # Add custom connection settings
        builder.connect_timeout(30)
        builder.read_timeout(30)
        builder.write_timeout(30)
        builder.pool_timeout(30)
        
        application = builder.build()
        
        # Add handlers
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CallbackQueryHandler(button_handler))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_error_handler(error_handler)
        
        logger.info("🤖 Bot started successfully!")
        logger.info(f"Bot Token: {BOT_TOKEN[:10]}...")
        logger.info(f"Owner ID: {OWNER_ID}")
        
        # Initialize and start the application
        await application.initialize()
        await application.start()
        
        # Start polling with custom settings - FIXED conflict issue
        await application.updater.start_polling(
            poll_interval=1.0,
            timeout=30,
            read_latency=2.0,
            bootstrap_retries=5,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True  # This clears any stuck updates
        )
        
        # Wait for shutdown signal
        await shutdown_event.wait()
            
    except Exception as e:
        logger.error(f"Fatal error in async_main: {e}")
        raise
    finally:
        # Cleanup
        logger.info("Starting shutdown...")
        
        # Stop application
        if application:
            try:
                if application.updater:
                    await application.updater.stop()
                await application.stop()
                await application.shutdown()
            except Exception as e:
                logger.error(f"Error stopping application: {e}")
        
        # Stop account manager
        if account_manager:
            try:
                await account_manager.stop()
            except Exception as e:
                logger.error(f"Error stopping account manager: {e}")
        
        # Close database
        if db:
            try:
                db.close_session()
            except Exception as e:
                logger.error(f"Error closing database: {e}")
        
        logger.info("✅ Shutdown complete")

def main():
    """Synchronous main entry point"""
    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Try to acquire lock to prevent multiple instances
    if not acquire_lock():
        logger.error("Another bot instance is already running. Exiting.")
        sys.exit(1)
    
    global loop
    loop = None
    try:
        # Handle Windows event loop policy
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
        # Create new event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Run the async main function
        loop.run_until_complete(async_main())
        
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
        # Signal shutdown
        if loop and loop.is_running():
            loop.call_soon_threadsafe(shutdown_event.set)
    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
    finally:
        # Release lock
        release_lock()
        
        # Clean up loop
        if loop and not loop.is_closed():
            # Cancel all tasks
            for task in asyncio.all_tasks(loop):
                task.cancel()
            
            # Run loop one last time to let tasks finish
            if not loop.is_running():
                try:
                    loop.run_until_complete(asyncio.sleep(0.1))
                except:
                    pass
            
            # Close loop
            loop.close()
            logger.info("✅ Event loop closed")

if __name__ == '__main__':
    main()
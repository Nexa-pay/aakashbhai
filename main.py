import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
from config import BOT_TOKEN, OWNER_ID, REPORT_CATEGORIES, REPORT_TEMPLATES, DEFAULT_TOKENS, REPORT_COST
from database import init_db, get_db
from account_manager import AccountManager
from reporter import Reporter
import asyncio
from datetime import datetime, timezone

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize components
db = None
account_manager = None
reporter = None

async def startup():
    """Initialize components on startup"""
    global db, account_manager, reporter
    from config import DATABASE_URL
    
    # Initialize database
    db = init_db(DATABASE_URL)
    
    # Initialize account manager
    account_manager = AccountManager()
    await account_manager.start()
    
    # Initialize reporter
    reporter = Reporter(account_manager)
    
    logger.info("✅ All components initialized")

async def shutdown():
    """Cleanup on shutdown"""
    if account_manager:
        await account_manager.stop()
    if db:
        db.close_session()
    logger.info("✅ Shutdown complete")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command handler"""
    user = update.effective_user
    db_session = db.get_session()
    
    try:
        from models import User
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
            logger.info(f"New user: {user.id}")
        
        db_user.last_active = datetime.now(timezone.utc)
        db_session.commit()
        
    except Exception as e:
        logger.error(f"Start error: {e}")
        db_session.rollback()
        await update.message.reply_text("❌ Database error. Please try again.")
        return
    finally:
        db_session.close()
    
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
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🌟 Welcome {user.first_name}!\n\n"
        f"💰 Tokens: {db_user.tokens}\n"
        f"👤 Role: {db_user.role}\n"
        f"📊 Reports: {db_user.reports_made}",
        reply_markup=reply_markup
    )

# [Rest of your button_handler and message handlers from previous version]
# (Include all the button handlers and message handlers from your working main.py)

def main():
    """Start the bot"""
    try:
        # Run startup
        asyncio.run(startup())
        
        # Create application
        application = Application.builder().token(BOT_TOKEN).build()
        
        # Add handlers
        application.add_handler(CommandHandler("start", start))
        # Add all your other handlers here
        
        # Add shutdown handler
        application.post_shutdown = shutdown
        
        logger.info("🤖 Bot started!")
        application.run_polling()
        
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        asyncio.run(shutdown())
        raise

if __name__ == '__main__':
    main()
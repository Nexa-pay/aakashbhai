import os
import logging
from dotenv import load_dotenv

load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Bot Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN is not set!")

try:
    API_ID = int(os.getenv('API_ID', '0'))
    if API_ID == 0:
        raise ValueError("API_ID is not set or invalid!")
except ValueError:
    raise ValueError("❌ API_ID must be a number!")

API_HASH = os.getenv('API_HASH')
if not API_HASH:
    raise ValueError("❌ API_HASH is not set!")

OWNER_ID = int(os.getenv('OWNER_ID', '0'))

# Database URL - must be PostgreSQL for Railway
DATABASE_URL = os.getenv('DATABASE_URL')
if not DATABASE_URL:
    raise ValueError("❌ DATABASE_URL is not set! Add PostgreSQL to your Railway project.")

# Fix PostgreSQL URL format
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

# Report Categories
REPORT_CATEGORIES = {
    'child_abuse': 'Child abuse',
    'violence': 'Violence',
    'illegal_goods': 'Illegal goods and services',
    'illegal_adult': 'Illegal adult content',
    'personal_data': 'Personal data sharing',
    'scam_fraud': 'Scam or fraud',
    'copyright': 'Copyright infringement',
    'spam': 'Spam',
    'pornography': 'Pornography',
    'illegal_sexual': 'Illegal sexual services',
    'animal_abuse': 'Animal abuse',
    'non_consensual': 'Non-consensual sexual imagery',
    'other_illegal': 'Other illegal sexual content'
}

# Report Templates
REPORT_TEMPLATES = {
    'child_abuse': "This channel/group is sharing content involving child abuse and exploitation.",
    'violence': "This channel promotes and shares violent content and hate speech.",
    'illegal_goods': "This group is involved in trading illegal goods and services.",
    'illegal_adult': "This channel is sharing illegal adult content without proper age verification.",
    'personal_data': "This group is sharing personal data and private information without consent.",
    'scam_fraud': "This is a scam/fraud channel attempting to deceive users.",
    'copyright': "This channel is sharing copyrighted content without authorization.",
    'spam': "This channel is spreading spam and unsolicited messages.",
    'pornography': "This channel is sharing pornographic content without age restrictions.",
    'illegal_sexual': "This group is promoting illegal sexual services.",
    'animal_abuse': "This channel contains content showing animal abuse.",
    'non_consensual': "This group shares non-consensual intimate imagery.",
    'other_illegal': "This channel contains illegal sexual content violating Telegram's terms."
}

# Token System
DEFAULT_TOKENS = 10
REPORT_COST = 1
REPORT_INTERVAL = 10  # seconds between reports

logger.info("✅ Configuration loaded successfully")
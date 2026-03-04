import logging
import asyncio
import re
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List, Tuple
import os

logger = logging.getLogger(__name__)

# ==================== Date/Time Utilities ====================

def get_utc_now():
    """Get current UTC datetime"""
    return datetime.now(timezone.utc)

def format_datetime(dt: Optional[datetime], format: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format datetime for display"""
    if not dt:
        return "N/A"
    return dt.strftime(format)

def time_ago(dt: datetime) -> str:
    """Get human-readable time difference"""
    if not dt:
        return "unknown"
    
    diff = get_utc_now() - dt.replace(tzinfo=timezone.utc)
    seconds = diff.total_seconds()
    
    if seconds < 60:
        return f"{int(seconds)} seconds ago"
    elif seconds < 3600:
        return f"{int(seconds / 60)} minutes ago"
    elif seconds < 86400:
        return f"{int(seconds / 3600)} hours ago"
    else:
        return f"{int(seconds / 86400)} days ago"

# ==================== Validation Utilities ====================

def validate_phone_number(phone: str) -> Tuple[bool, Optional[str]]:
    """
    Validate international phone number format
    Returns (is_valid, error_message)
    """
    if not phone:
        return False, "Phone number is required"
    
    phone = phone.strip()
    
    # Check if it's a username (contains @)
    if '@' in phone:
        return False, "Please enter a phone number, not a username"
    
    # Must start with +
    if not phone.startswith('+'):
        return False, "Phone number must start with country code (e.g., +1234567890)"
    
    # Remove spaces and check length
    clean = phone.replace(' ', '').replace('-', '')
    if len(clean) < 10 or len(clean) > 15:
        return False, "Phone number should be 10-15 digits with country code"
    
    # Check if rest are digits
    if not clean[1:].isdigit():
        return False, "Phone number should contain only digits after country code"
    
    return True, None

def validate_verification_code(code: str) -> Tuple[bool, Optional[str]]:
    """Validate 5-digit verification code"""
    if not code:
        return False, "Code is required"
    
    code = code.strip()
    
    if not code.isdigit():
        return False, "Code should contain only digits"
    
    if len(code) != 5:
        return False, "Code should be exactly 5 digits"
    
    return True, None

def validate_target_username(target: str) -> Tuple[bool, Optional[str]]:
    """Validate target username or ID for reporting"""
    if not target:
        return False, "Target is required"
    
    target = target.strip()
    
    # Check for valid patterns
    if target.startswith('@'):
        # Username format: @username
        if len(target) < 2:
            return False, "Username too short"
        return True, None
    elif target.startswith('-100'):
        # Channel ID format: -1001234567890
        if not target[4:].isdigit():
            return False, "Invalid channel ID format"
        return True, None
    elif target.isdigit():
        # User ID format: 1234567890
        return True, None
    else:
        return False, "Invalid target format. Use @username, -100channel_id, or user_id"

def parse_targets(text: str) -> List[Dict[str, str]]:
    """
    Parse multiple targets from text input
    Returns list of targets with type and identifier
    """
    targets = []
    lines = text.split('\n')
    
    for line in lines:
        # Split by commas and clean
        items = [item.strip() for item in line.split(',') if item.strip()]
        
        for item in items:
            # Clean up the target
            target = item.strip()
            if not target:
                continue
            
            # Determine target type
            if target.startswith('@'):
                target_type = 'channel'
                identifier = target  # Keep @ for username
            elif target.startswith('-100'):
                target_type = 'channel'
                identifier = target  # Full channel ID
            elif target.isdigit():
                target_type = 'user'
                identifier = target  # Numeric user ID
            else:
                # Try to extract username
                if target.startswith('t.me/'):
                    target = target.replace('t.me/', '')
                if not target.startswith('@'):
                    target = f"@{target}"
                target_type = 'channel'
                identifier = target
            
            targets.append({
                'type': target_type,
                'username': identifier if identifier.startswith('@') else None,
                'id': identifier if not identifier.startswith('@') else None
            })
    
    return targets

# ==================== Formatting Utilities ====================

def format_number(num: int) -> str:
    """Format large numbers with commas"""
    return f"{num:,}"

def format_tokens(amount: int) -> str:
    """Format token amount with appropriate emoji"""
    if amount >= 1000:
        return f"💰 {format_number(amount)}"
    elif amount >= 100:
        return f"🪙 {amount}"
    else:
        return f"🔹 {amount}"

def format_report_status(status: str) -> Tuple[str, str]:
    """Get emoji and color for report status"""
    status_map = {
        'pending': ('⏳', 'yellow'),
        'in_progress': ('🔄', 'blue'),
        'completed': ('✅', 'green'),
        'failed': ('❌', 'red')
    }
    return status_map.get(status, ('❓', 'gray'))

def truncate_text(text: str, max_length: int = 200) -> str:
    """Truncate text with ellipsis"""
    if len(text) <= max_length:
        return text
    return text[:max_length-3] + "..."

# ==================== JSON Utilities ====================

def safe_json_dumps(data: Any) -> str:
    """Safely dump JSON with error handling"""
    try:
        return json.dumps(data, default=str)
    except Exception as e:
        logger.error(f"JSON dump error: {e}")
        return "{}"

def safe_json_loads(json_str: str) -> Dict:
    """Safely load JSON with error handling"""
    if not json_str:
        return {}
    try:
        return json.loads(json_str)
    except Exception as e:
        logger.error(f"JSON load error: {e}")
        return {}

# ==================== Async Utilities ====================

async def retry_async(func, *args, max_retries=3, delay=1, **kwargs):
    """Retry an async function with exponential backoff"""
    for attempt in range(max_retries):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            wait = delay * (2 ** attempt)
            logger.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {wait}s")
            await asyncio.sleep(wait)

def create_task_log_exception(loop, coro):
    """Create async task with exception logging"""
    async def _wrap():
        try:
            return await coro
        except Exception as e:
            logger.exception(f"Task failed: {e}")
    return loop.create_task(_wrap())

# ==================== File Utilities ====================

def ensure_dir(path: str) -> bool:
    """Ensure directory exists"""
    try:
        os.makedirs(path, exist_ok=True)
        return True
    except Exception as e:
        logger.error(f"Failed to create directory {path}: {e}")
        return False

def safe_file_write(path: str, content: str) -> bool:
    """Safely write content to file"""
    try:
        ensure_dir(os.path.dirname(path))
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        return True
    except Exception as e:
        logger.error(f"Failed to write file {path}: {e}")
        return False

def safe_file_read(path: str) -> Optional[str]:
    """Safely read content from file"""
    try:
        if not os.path.exists(path):
            return None
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        logger.error(f"Failed to read file {path}: {e}")
        return None

# ==================== Text Utilities ====================

def escape_markdown(text: str) -> str:
    """Escape special characters for Telegram Markdown"""
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

def split_long_message(text: str, max_length: int = 4096) -> List[str]:
    """Split long message into chunks for Telegram"""
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    current_chunk = ""
    
    # Split by paragraphs first
    paragraphs = text.split('\n\n')
    
    for para in paragraphs:
        if len(current_chunk) + len(para) + 2 <= max_length:
            if current_chunk:
                current_chunk += '\n\n' + para
            else:
                current_chunk = para
        else:
            if current_chunk:
                chunks.append(current_chunk)
            
            # If paragraph itself is too long, split by sentences
            if len(para) > max_length:
                sentences = para.split('. ')
                current_chunk = ""
                for sent in sentences:
                    if len(current_chunk) + len(sent) + 2 <= max_length:
                        if current_chunk:
                            current_chunk += '. ' + sent
                        else:
                            current_chunk = sent
                    else:
                        chunks.append(current_chunk)
                        current_chunk = sent
            else:
                current_chunk = para
    
    if current_chunk:
        chunks.append(current_chunk)
    
    return chunks

# ==================== Rate Limiting Utilities ====================

class RateLimiter:
    """Simple rate limiter for API calls"""
    
    def __init__(self, max_calls: int, period: int):
        self.max_calls = max_calls
        self.period = period
        self.calls = []
    
    async def acquire(self):
        """Acquire permission to make a call"""
        now = time.time()
        # Remove old calls
        self.calls = [t for t in self.calls if now - t < self.period]
        
        if len(self.calls) >= self.max_calls:
            # Wait until oldest call expires
            wait_time = self.period - (now - self.calls[0])
            if wait_time > 0:
                await asyncio.sleep(wait_time)
            self.calls = self.calls[1:]
        
        self.calls.append(now)
        return True

# ==================== Environment Utilities ====================

def is_railway() -> bool:
    """Check if running on Railway"""
    return os.getenv('RAILWAY_SERVICE_ID') is not None

def get_railway_url() -> Optional[str]:
    """Get Railway public URL if available"""
    return os.getenv('RAILWAY_PUBLIC_URL')

def get_env_var(name: str, default: Any = None, required: bool = False) -> Any:
    """Get environment variable with validation"""
    value = os.getenv(name, default)
    if required and value is None:
        raise ValueError(f"Required environment variable {name} is not set")
    return value

# ==================== Logging Utilities ====================

def setup_logging(level=logging.INFO):
    """Setup logging configuration"""
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=level,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('bot.log')
        ]
    )
    
    # Suppress noisy loggers
    logging.getLogger('telethon').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    
    return logging.getLogger(__name__)

# ==================== Database Utilities ====================

def parse_database_url(url: str) -> Dict[str, str]:
    """Parse database URL into components"""
    # postgresql://user:pass@host:port/dbname
    pattern = r'postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)'
    match = re.match(pattern, url)
    
    if match:
        return {
            'user': match.group(1),
            'password': match.group(2),
            'host': match.group(3),
            'port': match.group(4),
            'database': match.group(5)
        }
    return {}

# ==================== Statistics Utilities ====================

class Statistics:
    """Simple statistics tracker"""
    
    def __init__(self):
        self.stats = {
            'reports_submitted': 0,
            'reports_completed': 0,
            'reports_failed': 0,
            'accounts_added': 0,
            'tokens_used': 0,
            'start_time': get_utc_now()
        }
    
    def increment(self, key: str, amount: int = 1):
        """Increment a statistic"""
        if key in self.stats:
            self.stats[key] += amount
    
    def get(self, key: str) -> Any:
        """Get a statistic"""
        return self.stats.get(key, 0)
    
    def get_all(self) -> Dict:
        """Get all statistics"""
        uptime = get_utc_now() - self.stats['start_time']
        return {
            **self.stats,
            'uptime_seconds': uptime.total_seconds(),
            'uptime_formatted': str(uptime).split('.')[0]
        }
    
    def reset(self):
        """Reset statistics"""
        self.stats = {
            'reports_submitted': 0,
            'reports_completed': 0,
            'reports_failed': 0,
            'accounts_added': 0,
            'tokens_used': 0,
            'start_time': get_utc_now()
        }

# Global statistics instance
stats = Statistics()
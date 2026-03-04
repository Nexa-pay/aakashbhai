import warnings
warnings.filterwarnings("ignore", message="cryptg module not installed")

from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError, 
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    FloodWaitError,
    PhoneNumberInvalidError,
    PasswordHashInvalidError
)
from telethon.sessions import StringSession
from database import get_db
from models import TelegramAccount
import logging
import os
import time
import asyncio
from config import API_ID, API_HASH
from datetime import datetime, timezone, timedelta

# Create sessions directory in /tmp for Railway
SESSIONS_DIR = '/tmp/telegram_sessions'
os.makedirs(SESSIONS_DIR, exist_ok=True)

logger = logging.getLogger(__name__)

class AccountManager:
    def __init__(self):
        self.db = get_db()
        self.active_sessions = {}  # phone -> {client, phone_code_hash, created_at}
        self.account_locks = {}  # phone -> asyncio.Lock
        self.report_queue = asyncio.Queue()
        self.is_running = False
        self.report_task = None
    
    async def start(self):
        """Start the report processor"""
        self.is_running = True
        self.report_task = asyncio.create_task(self._process_reports())
        logger.info("✅ Account manager started")
    
    async def stop(self):
        """Stop the report processor"""
        self.is_running = False
        if self.report_task:
            self.report_task.cancel()
            try:
                await self.report_task
            except:
                pass
        # Disconnect all active sessions
        for phone, session_data in list(self.active_sessions.items()):
            try:
                await session_data['client'].disconnect()
            except:
                pass
        self.active_sessions.clear()
        logger.info("✅ Account manager stopped")
    
    def _get_lock(self, phone):
        """Get or create a lock for a phone number"""
        if phone not in self.account_locks:
            self.account_locks[phone] = asyncio.Lock()
        return self.account_locks[phone]
    
    async def add_account(self, phone_number, verification_code=None, password=None):
        """Add a new Telegram account for reporting"""
        
        # --- PHONE NUMBER VALIDATION ---
        if not phone_number:
            return {'status': 'error', 'error': 'Phone number is required'}
        
        phone_number = str(phone_number).strip()
        
        if '@' in phone_number or not phone_number.startswith('+'):
            return {
                'status': 'error',
                'error': 'Please enter a valid phone number with country code (e.g., +1234567890)'
            }
        
        phone_number = phone_number.replace(' ', '')
        
        # Use lock to prevent concurrent operations on same phone
        async with self._get_lock(phone_number):
            return await self._add_account_locked(phone_number, verification_code, password)
    
    async def _add_account_locked(self, phone_number, verification_code=None, password=None):
        """Add account with lock held"""
        client = None
        try:
            clean_phone = phone_number.replace('+', '').replace(' ', '')
            session_path = os.path.join(SESSIONS_DIR, clean_phone)
            session_file = session_path + '.session'
            
            # Clean up old session
            if os.path.exists(session_file):
                try:
                    os.remove(session_file)
                except:
                    pass
            
            await self._cancel_login(phone_number)
            
            # Create new client
            client = TelegramClient(session_path, API_ID, API_HASH)
            await client.connect()
            
            # Check if already authorized
            if await client.is_user_authorized():
                return await self._save_authorized_client(client, phone_number, session_file)
            
            # Step 1: Send code
            if verification_code is None and password is None:
                try:
                    result = await client.send_code_request(phone_number)
                    
                    self.active_sessions[phone_number] = {
                        'client': client,
                        'phone_code_hash': result.phone_code_hash,
                        'created_at': time.time()
                    }
                    
                    return {
                        'status': 'code_sent',
                        'phone': phone_number,
                        'message': 'Verification code sent. Please enter it within 2 minutes.'
                    }
                    
                except FloodWaitError as e:
                    wait_time = e.seconds
                    await client.disconnect()
                    return {
                        'status': 'flood_wait',
                        'wait_time': wait_time,
                        'message': f'Please wait {wait_time} seconds'
                    }
                    
                except PhoneNumberInvalidError:
                    await client.disconnect()
                    return {'status': 'error', 'error': 'Invalid phone number format'}
                    
                except Exception as e:
                    await client.disconnect()
                    logger.error(f"Error sending code: {e}")
                    return {'status': 'error', 'error': str(e)}
            
            # Step 2: Verify code
            elif verification_code and password is None:
                session_data = self.active_sessions.get(phone_number)
                
                if not session_data:
                    return {'status': 'error', 'error': 'Session expired. Please start over.'}
                
                client = session_data['client']
                phone_code_hash = session_data['phone_code_hash']
                created_at = session_data['created_at']
                
                if time.time() - created_at > 120:
                    await client.disconnect()
                    self.active_sessions.pop(phone_number, None)
                    return {'status': 'code_expired', 'message': 'Code expired'}
                
                try:
                    await client.sign_in(
                        phone_number,
                        code=verification_code,
                        phone_code_hash=phone_code_hash
                    )
                    
                    return await self._save_authorized_client(client, phone_number, session_file)
                    
                except SessionPasswordNeededError:
                    session_data['created_at'] = time.time()
                    return {
                        'status': 'password_needed',
                        'message': '2FA enabled. Please enter your password.'
                    }
                    
                except PhoneCodeExpiredError:
                    await client.disconnect()
                    self.active_sessions.pop(phone_number, None)
                    return {'status': 'code_expired', 'message': 'Code expired'}
                    
                except PhoneCodeInvalidError:
                    return {'status': 'code_invalid', 'message': 'Invalid code'}
                    
                except Exception as e:
                    logger.error(f"Code verification error: {e}")
                    return {'status': 'error', 'error': str(e)}
            
            # Step 3: Enter password (2FA)
            elif password:
                session_data = self.active_sessions.get(phone_number)
                
                if not session_data:
                    return {'status': 'error', 'error': 'Session expired'}
                
                client = session_data['client']
                
                if not client.is_connected():
                    try:
                        await client.connect()
                    except:
                        return {'status': 'error', 'error': 'Connection lost'}
                
                try:
                    await client.sign_in(password=password)
                    return await self._save_authorized_client(client, phone_number, session_file)
                    
                except PasswordHashInvalidError:
                    return {'status': 'password_error', 'error': 'Invalid password'}
                    
                except FloodWaitError as e:
                    return {
                        'status': 'flood_wait',
                        'wait_time': e.seconds,
                        'message': f'Please wait {e.seconds} seconds'
                    }
                    
                except Exception as e:
                    logger.error(f"Password error: {e}")
                    return {'status': 'password_error', 'error': str(e)}
            
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            if client:
                try:
                    await client.disconnect()
                except:
                    pass
            return {'status': 'error', 'error': str(e)}
    
    async def _save_authorized_client(self, client, phone_number, session_file):
        """Save authorized client to database"""
        try:
            session_string = StringSession.save(client.session)
            db_session = self.db.get_session()
            
            try:
                existing = db_session.query(TelegramAccount).filter_by(phone_number=phone_number).first()
                
                if existing:
                    existing.session_string = session_string
                    existing.is_active = True
                    existing.status = 'available'
                else:
                    account = TelegramAccount(
                        phone_number=phone_number,
                        session_string=session_string,
                        is_active=True,
                        status='available'
                    )
                    db_session.add(account)
                
                db_session.commit()
                logger.info(f"✅ Account added: {phone_number}")
                
            finally:
                db_session.close()
            
            await client.disconnect()
            self.active_sessions.pop(phone_number, None)
            
            if os.path.exists(session_file):
                try:
                    os.remove(session_file)
                except:
                    pass
            
            return {
                'status': 'success',
                'phone': phone_number,
                'message': 'Account added successfully!'
            }
            
        except Exception as e:
            logger.error(f"Error saving account: {e}")
            await client.disconnect()
            return {'status': 'error', 'error': f'Database error: {str(e)}'}
    
    async def _cancel_login(self, phone_number):
        """Cancel login attempt"""
        if phone_number in self.active_sessions:
            try:
                await self.active_sessions[phone_number]['client'].disconnect()
            except:
                pass
            self.active_sessions.pop(phone_number, None)
        
        clean_phone = phone_number.replace('+', '').replace(' ', '')
        session_file = os.path.join(SESSIONS_DIR, clean_phone) + '.session'
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
            except:
                pass
    
    async def get_available_accounts(self, limit=5):
        """Get available accounts for reporting"""
        db_session = self.db.get_session()
        try:
            now = datetime.now(timezone.utc)
            accounts = db_session.query(TelegramAccount).filter(
                TelegramAccount.is_active == True,
                TelegramAccount.status == 'available',
                (TelegramAccount.cooldown_until.is_(None) | (TelegramAccount.cooldown_until <= now))
            ).limit(limit).all()
            return accounts
        finally:
            db_session.close()
    
    async def report_target(self, account, target_username, category, custom_text):
        """Report a target using specific account"""
        client = None
        try:
            if not target_username:
                return {'status': 'failed', 'reason': 'No target'}
            
            client = TelegramClient(
                StringSession(account.session_string), 
                API_ID, 
                API_HASH
            )
            await client.connect()
            
            if not await client.is_user_authorized():
                account.is_active = False
                db_session = self.db.get_session()
                db_session.add(account)
                db_session.commit()
                db_session.close()
                return {'status': 'failed', 'reason': 'unauthorized'}
            
            # Get entity
            if target_username.startswith('@'):
                target_username = target_username[1:]
            
            try:
                entity = await client.get_entity(target_username)
            except ValueError:
                try:
                    entity = await client.get_entity(int(target_username))
                except:
                    return {'status': 'failed', 'reason': 'target_not_found'}
            
            # Try reporting methods
            report_sent = False
            
            # Method 1: ReportRequest
            try:
                from telethon.tl.functions.messages import ReportRequest
                from telethon.tl.types import InputReportReasonOther
                
                await client(ReportRequest(
                    peer=entity,
                    id=[],
                    reason=InputReportReasonOther(),
                    message=custom_text
                ))
                report_sent = True
            except:
                pass
            
            # Method 2: @SpamBot
            if not report_sent:
                try:
                    await client.send_message('@SpamBot', f'/report {target_username}')
                    report_sent = True
                except:
                    pass
            
            await client.disconnect()
            
            if report_sent:
                # Update account with cooldown
                db_session = self.db.get_session()
                account.status = 'cooldown'
                account.last_used = datetime.now(timezone.utc)
                account.cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=10)
                account.reports_count += 1
                db_session.add(account)
                db_session.commit()
                db_session.close()
                
                return {'status': 'success'}
            else:
                return {'status': 'failed', 'reason': 'all_methods_failed'}
            
        except Exception as e:
            logger.error(f"Report error: {e}")
            return {'status': 'failed', 'reason': str(e)}
    
    async def queue_report(self, target, category, text, user_id):
        """Queue a report for processing"""
        await self.report_queue.put({
            'target': target,
            'category': category,
            'text': text,
            'user_id': user_id,
            'timestamp': time.time()
        })
    
    async def _process_reports(self):
        """Process reports from queue with 10-second interval"""
        while self.is_running:
            try:
                # Get next report from queue
                try:
                    report_data = await asyncio.wait_for(self.report_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                # Get available accounts
                accounts = await self.get_available_accounts(limit=3)
                
                if not accounts:
                    logger.warning("No accounts available, requeuing report")
                    await asyncio.sleep(5)
                    await self.report_queue.put(report_data)
                    continue
                
                # Report with each account
                for account in accounts:
                    result = await self.report_target(
                        account,
                        report_data['target'],
                        report_data['category'],
                        report_data['text']
                    )
                    
                    if result['status'] == 'success':
                        logger.info(f"Reported {report_data['target']} with {account.phone_number}")
                    else:
                        logger.warning(f"Failed to report with {account.phone_number}: {result.get('reason')}")
                    
                    # 10-second interval between reports
                    await asyncio.sleep(10)
                
                self.report_queue.task_done()
                
            except Exception as e:
                logger.error(f"Report processor error: {e}")
                await asyncio.sleep(5)
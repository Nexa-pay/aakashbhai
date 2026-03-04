import warnings
warnings.filterwarnings("ignore", message="cryptg module not installed")

from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError, 
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    FloodWaitError,
    PhoneNumberInvalidError,
    PasswordHashInvalidError,
    PhoneCodeHashEmptyError,
    PhoneNumberBannedError,
    PhoneNumberOccupiedError
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
        self.active_sessions = {}  # phone -> {client, phone_code_hash, created_at, step}
        self.account_locks = {}  # phone -> asyncio.Lock
        self.report_queue = asyncio.Queue()
        self.is_running = False
        self.report_task = None
        self.cleanup_task = None
    
    async def start(self):
        """Start the report processor"""
        self.is_running = True
        self.report_task = asyncio.create_task(self._process_reports())
        self.cleanup_task = asyncio.create_task(self._cleanup_expired_sessions())
        logger.info("✅ Account manager started")
    
    async def stop(self):
        """Stop the report processor"""
        logger.info("Stopping account manager...")
        self.is_running = False
        
        # Cancel report task
        if self.report_task and not self.report_task.done():
            self.report_task.cancel()
            try:
                await self.report_task
            except asyncio.CancelledError:
                logger.info("Report task cancelled")
            except Exception as e:
                logger.error(f"Error cancelling report task: {e}")
        
        # Cancel cleanup task
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                logger.info("Cleanup task cancelled")
            except Exception as e:
                logger.error(f"Error cancelling cleanup task: {e}")
        
        # Disconnect all active sessions
        disconnect_tasks = []
        for phone, session_data in list(self.active_sessions.items()):
            try:
                task = asyncio.create_task(session_data['client'].disconnect())
                disconnect_tasks.append(task)
            except Exception as e:
                logger.error(f"Error disconnecting {phone}: {e}")
        
        if disconnect_tasks:
            await asyncio.gather(*disconnect_tasks, return_exceptions=True)
        
        self.active_sessions.clear()
        self.account_locks.clear()
        logger.info("✅ Account manager stopped")
    
    def _get_lock(self, phone):
        """Get or create a lock for a phone number"""
        if phone not in self.account_locks:
            self.account_locks[phone] = asyncio.Lock()
        return self.account_locks[phone]
    
    async def _cleanup_phone_sessions(self, phone_number):
        """Completely clean up all traces of a phone number session"""
        logger.info(f"Performing complete cleanup for {phone_number}")
        
        # Remove from active sessions
        if phone_number in self.active_sessions:
            try:
                await self.active_sessions[phone_number]['client'].disconnect()
            except:
                pass
            del self.active_sessions[phone_number]
        
        # Remove all session files for this phone
        clean_phone = phone_number.replace('+', '').replace(' ', '')
        
        # Remove .session file
        session_file = os.path.join(SESSIONS_DIR, clean_phone) + '.session'
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
                logger.info(f"Removed session file: {session_file}")
            except Exception as e:
                logger.error(f"Error removing session file: {e}")
        
        # Remove .session-journal file (sometimes created by Telethon)
        journal_file = os.path.join(SESSIONS_DIR, clean_phone) + '.session-journal'
        if os.path.exists(journal_file):
            try:
                os.remove(journal_file)
                logger.info(f"Removed journal file: {journal_file}")
            except Exception as e:
                logger.error(f"Error removing journal file: {e}")
        
        # Wait a moment to ensure files are removed
        await asyncio.sleep(1)
    
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
            
            # Log the current step for debugging
            logger.info(f"Account addition step - Phone: {phone_number}, Code provided: {bool(verification_code)}, Password provided: {bool(password)}")
            
            # If this is a new request (no code or password), do a thorough cleanup first
            if verification_code is None and password is None:
                await self._cleanup_phone_sessions(phone_number)
            
            # Check if we have an existing session for this phone
            existing_session = self.active_sessions.get(phone_number)
            if existing_session:
                logger.info(f"Found existing session for {phone_number} created at {datetime.fromtimestamp(existing_session['created_at']).isoformat()}")
                
                # Check if session is too old (more than 3 minutes)
                if time.time() - existing_session['created_at'] > 180:
                    logger.info(f"Existing session for {phone_number} expired, cleaning up")
                    await self._cleanup_phone_sessions(phone_number)
                    existing_session = None
            
            # If we have an existing session and we're in the middle of authentication, reuse it
            if existing_session and (verification_code or password):
                client = existing_session['client']
                logger.info(f"Reusing existing session for {phone_number}")
            else:
                # Create new client
                client = TelegramClient(session_path, API_ID, API_HASH)
                await client.connect()
                logger.info(f"Created new client for {phone_number}")
            
            # Check if already authorized
            if await client.is_user_authorized():
                logger.info(f"Client for {phone_number} is already authorized")
                return await self._save_authorized_client(client, phone_number, session_file)
            
            # Step 1: Send code
            if verification_code is None and password is None:
                try:
                    logger.info(f"Sending code request to {phone_number}")
                    result = await client.send_code_request(phone_number)
                    
                    self.active_sessions[phone_number] = {
                        'client': client,
                        'phone_code_hash': result.phone_code_hash,
                        'created_at': time.time(),
                        'step': 'code_sent'
                    }
                    
                    logger.info(f"Code sent successfully to {phone_number}, hash: {result.phone_code_hash[:10]}...")
                    
                    return {
                        'status': 'code_sent',
                        'phone': phone_number,
                        'message': 'Verification code sent. Please enter it within 2 minutes.'
                    }
                    
                except FloodWaitError as e:
                    wait_time = e.seconds
                    logger.warning(f"Flood wait for {phone_number}: {wait_time} seconds")
                    await client.disconnect()
                    await self._cleanup_phone_sessions(phone_number)
                    return {
                        'status': 'flood_wait',
                        'wait_time': wait_time,
                        'message': f'Please wait {wait_time} seconds'
                    }
                    
                except PhoneNumberInvalidError:
                    logger.error(f"Invalid phone number: {phone_number}")
                    await client.disconnect()
                    await self._cleanup_phone_sessions(phone_number)
                    return {'status': 'error', 'error': 'Invalid phone number format'}
                    
                except Exception as e:
                    logger.error(f"Error sending code: {e}")
                    await client.disconnect()
                    await self._cleanup_phone_sessions(phone_number)
                    return {'status': 'error', 'error': str(e)}
            
            # Step 2: Verify code
            elif verification_code and password is None:
                session_data = self.active_sessions.get(phone_number)
                
                if not session_data:
                    logger.error(f"No active session found for {phone_number}")
                    await self._cleanup_phone_sessions(phone_number)
                    return {'status': 'error', 'error': 'Session expired. Please start over.'}
                
                client = session_data['client']
                phone_code_hash = session_data['phone_code_hash']
                created_at = session_data['created_at']
                
                logger.info(f"Session age: {time.time() - created_at:.1f} seconds")
                
                # Check if code expired (2 minutes)
                if time.time() - created_at > 120:
                    logger.warning(f"Code expired for {phone_number}")
                    await client.disconnect()
                    await self._cleanup_phone_sessions(phone_number)
                    return {'status': 'code_expired', 'message': 'Code expired'}
                
                try:
                    logger.info(f"Attempting to sign in {phone_number} with code")
                    await client.sign_in(
                        phone_number,
                        code=verification_code,
                        phone_code_hash=phone_code_hash
                    )
                    
                    logger.info(f"Code sign in successful for {phone_number}")
                    return await self._save_authorized_client(client, phone_number, session_file)
                    
                except SessionPasswordNeededError:
                    logger.info(f"2FA required for {phone_number}")
                    session_data['created_at'] = time.time()  # Reset timer for password step
                    session_data['step'] = 'password_needed'
                    return {
                        'status': 'password_needed',
                        'message': '2FA enabled. Please enter your password.'
                    }
                    
                except PhoneCodeExpiredError:
                    logger.warning(f"Code expired for {phone_number}")
                    await client.disconnect()
                    await self._cleanup_phone_sessions(phone_number)
                    return {'status': 'code_expired', 'message': 'Code expired'}
                    
                except PhoneCodeInvalidError:
                    logger.warning(f"Invalid code for {phone_number}")
                    # Don't clean up on invalid code, allow retry
                    return {'status': 'code_invalid', 'message': 'Invalid code'}
                    
                except Exception as e:
                    logger.error(f"Code verification error: {e}")
                    await client.disconnect()
                    await self._cleanup_phone_sessions(phone_number)
                    return {'status': 'error', 'error': str(e)}
            
            # Step 3: Enter password (2FA)
            elif password:
                session_data = self.active_sessions.get(phone_number)
                
                if not session_data:
                    logger.error(f"No active session found for {phone_number} during password step")
                    await self._cleanup_phone_sessions(phone_number)
                    return {'status': 'error', 'error': 'Session expired. Please start over.'}
                
                client = session_data['client']
                created_at = session_data['created_at']
                
                logger.info(f"Password step - Session age: {time.time() - created_at:.1f} seconds")
                
                # Check if session expired (3 minutes total)
                if time.time() - created_at > 180:
                    logger.warning(f"Password session expired for {phone_number}")
                    await client.disconnect()
                    await self._cleanup_phone_sessions(phone_number)
                    return {'status': 'error', 'error': 'Session expired. Please start over.'}
                
                if not client.is_connected():
                    logger.info(f"Client disconnected, reconnecting for {phone_number}")
                    try:
                        await client.connect()
                    except Exception as e:
                        logger.error(f"Reconnection failed: {e}")
                        await self._cleanup_phone_sessions(phone_number)
                        return {'status': 'error', 'error': 'Connection lost'}
                
                try:
                    logger.info(f"Attempting to sign in {phone_number} with password")
                    await client.sign_in(password=password)
                    
                    logger.info(f"Password sign in successful for {phone_number}")
                    return await self._save_authorized_client(client, phone_number, session_file)
                    
                except PasswordHashInvalidError:
                    logger.warning(f"Invalid password for {phone_number}")
                    return {'status': 'password_error', 'error': 'Invalid password'}
                    
                except FloodWaitError as e:
                    wait_time = e.seconds
                    logger.warning(f"Flood wait during password for {phone_number}: {wait_time} seconds")
                    return {
                        'status': 'flood_wait',
                        'wait_time': wait_time,
                        'message': f'Please wait {wait_time} seconds'
                    }
                    
                except Exception as e:
                    logger.error(f"Password error: {e}")
                    await self._cleanup_phone_sessions(phone_number)
                    return {'status': 'password_error', 'error': str(e)}
            
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            if client:
                try:
                    await client.disconnect()
                except:
                    pass
            await self._cleanup_phone_sessions(phone_number)
            return {'status': 'error', 'error': str(e)}
    
    async def _save_authorized_client(self, client, phone_number, session_file):
        """Save authorized client to database with session string"""
        try:
            # Generate session string - THIS IS THE KEY PART
            session_string = StringSession.save(client.session)
            logger.info(f"✅ Generated session string for {phone_number}")
            logger.info(f"Session string length: {len(session_string)} characters")
            logger.info(f"Session string preview: {session_string[:50]}...")
            
            db_session = self.db.get_session()
            try:
                # Check if account already exists
                existing = db_session.query(TelegramAccount).filter_by(phone_number=phone_number).first()
                
                if existing:
                    existing.session_string = session_string
                    existing.is_active = True
                    existing.status = 'available'
                    existing.added_date = datetime.now(timezone.utc)
                    logger.info(f"Updated existing account: {phone_number}")
                else:
                    account = TelegramAccount(
                        phone_number=phone_number,
                        session_string=session_string,
                        is_active=True,
                        status='available'
                    )
                    db_session.add(account)
                    logger.info(f"Created new account: {phone_number}")
                
                db_session.commit()
                logger.info(f"✅ Account saved to database: {phone_number}")
                
                # Verify it was saved
                saved = db_session.query(TelegramAccount).filter_by(phone_number=phone_number).first()
                if saved and saved.session_string:
                    logger.info(f"✅ Verified: Session string exists in DB for {phone_number}")
                    logger.info(f"Stored session string length: {len(saved.session_string)}")
                else:
                    logger.error(f"❌ Verification failed: Session string not found in DB for {phone_number}")
                
                # Count total accounts
                count = db_session.query(TelegramAccount).count()
                logger.info(f"Total accounts in DB: {count}")
                
            except Exception as e:
                logger.error(f"Database error: {e}")
                db_session.rollback()
                raise
            finally:
                db_session.close()
            
            await client.disconnect()
            
            # Do a thorough cleanup after successful save
            await self._cleanup_phone_sessions(phone_number)
            
            return {
                'status': 'success',
                'phone': phone_number,
                'message': '✅ Account added successfully!'
            }
            
        except Exception as e:
            logger.error(f"Error saving account: {e}")
            await client.disconnect()
            await self._cleanup_phone_sessions(phone_number)
            return {'status': 'error', 'error': f'Database error: {str(e)}'}
    
    async def resend_code(self, phone_number):
        """Resend verification code with complete cleanup"""
        try:
            logger.info(f"Resending code for {phone_number}")
            
            # Do a thorough cleanup before resending
            await self._cleanup_phone_sessions(phone_number)
            
            # Wait a bit to ensure cleanup
            await asyncio.sleep(3)
            
            # Create new client
            clean_phone = phone_number.replace('+', '').replace(' ', '')
            session_path = os.path.join(SESSIONS_DIR, clean_phone)
            client = TelegramClient(session_path, API_ID, API_HASH)
            await client.connect()
            
            # Send new code
            result = await client.send_code_request(phone_number)
            logger.info(f"New code sent to {phone_number}, hash: {result.phone_code_hash[:10]}...")
            
            # Store in active sessions
            self.active_sessions[phone_number] = {
                'client': client,
                'phone_code_hash': result.phone_code_hash,
                'created_at': time.time(),
                'step': 'code_sent'
            }
            
            return {
                'status': 'code_sent',
                'phone': phone_number,
                'message': 'New verification code sent. Please enter it within 2 minutes.'
            }
            
        except FloodWaitError as e:
            wait_time = e.seconds
            logger.warning(f"Flood wait for {phone_number}: {wait_time} seconds")
            return {
                'status': 'flood_wait',
                'wait_time': wait_time,
                'message': f'Please wait {wait_time} seconds'
            }
            
        except Exception as e:
            logger.error(f"Error resending code: {e}")
            await self._cleanup_phone_sessions(phone_number)
            return {'status': 'error', 'error': str(e)}
    
    async def cancel_login(self, phone_number):
        """Cancel an ongoing login attempt"""
        logger.info(f"Cancelling login for {phone_number}")
        await self._cleanup_phone_sessions(phone_number)
        return {'status': 'success'}
    
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
            
            logger.info(f"Found {len(accounts)} available accounts")
            for acc in accounts:
                logger.info(f"Account {acc.phone_number} - Session string length: {len(acc.session_string) if acc.session_string else 0}")
            
            return accounts
        finally:
            db_session.close()
    
    async def report_target(self, account, target_username, category, custom_text):
        """Report a target using specific account"""
        client = None
        try:
            if not target_username:
                return {'status': 'failed', 'reason': 'No target'}
            
            logger.info(f"Reporting {target_username} with account {account.phone_number}")
            
            # Use the saved session string
            if not account.session_string:
                logger.error(f"Account {account.phone_number} has no session string!")
                return {'status': 'failed', 'reason': 'no_session'}
            
            client = TelegramClient(
                StringSession(account.session_string), 
                API_ID, 
                API_HASH
            )
            await client.connect()
            
            if not await client.is_user_authorized():
                logger.warning(f"Account {account.phone_number} not authorized")
                account.is_active = False
                db_session = self.db.get_session()
                try:
                    db_session.add(account)
                    db_session.commit()
                finally:
                    db_session.close()
                return {'status': 'failed', 'reason': 'unauthorized'}
            
            # Get entity
            original_target = target_username
            if target_username.startswith('@'):
                target_username = target_username[1:]
            
            try:
                entity = await client.get_entity(target_username)
                logger.info(f"Found entity: {getattr(entity, 'title', getattr(entity, 'username', 'Unknown'))}")
            except ValueError:
                try:
                    entity = await client.get_entity(int(target_username))
                except:
                    return {'status': 'failed', 'reason': 'target_not_found'}
            except Exception as e:
                logger.error(f"Error getting entity: {e}")
                return {'status': 'failed', 'reason': f'target_error: {str(e)}'}
            
            # Try reporting methods
            report_sent = False
            
            # Method 1: ReportRequest (most official)
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
                logger.info(f"Reported via ReportRequest: {original_target}")
            except Exception as e:
                logger.debug(f"ReportRequest failed: {e}")
            
            # Method 2: @SpamBot
            if not report_sent:
                try:
                    await client.send_message('@SpamBot', f'/report {target_username}')
                    report_sent = True
                    logger.info(f"Reported via @SpamBot: {original_target}")
                except Exception as e:
                    logger.debug(f"@SpamBot failed: {e}")
            
            # Method 3: Telegram support
            if not report_sent:
                try:
                    await client.send_message(
                        'Telegram', 
                        f"Report about {original_target}\n\nCategory: {category}\n\nDetails: {custom_text}"
                    )
                    report_sent = True
                    logger.info(f"Reported via Telegram support: {original_target}")
                except Exception as e:
                    logger.debug(f"Telegram support failed: {e}")
            
            await client.disconnect()
            
            if report_sent:
                # Update account with cooldown
                db_session = self.db.get_session()
                try:
                    account.status = 'cooldown'
                    account.last_used = datetime.now(timezone.utc)
                    account.cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=10)
                    account.reports_count += 1
                    db_session.add(account)
                    db_session.commit()
                    logger.info(f"Account {account.phone_number} updated with cooldown")
                finally:
                    db_session.close()
                
                return {'status': 'success'}
            else:
                logger.warning(f"All report methods failed for {original_target}")
                return {'status': 'failed', 'reason': 'all_methods_failed'}
            
        except Exception as e:
            logger.error(f"Report error: {e}")
            if client:
                try:
                    await client.disconnect()
                except:
                    pass
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
        logger.info(f"Queued report for {target}")
    
    async def _process_reports(self):
        """Process reports from queue with 10-second interval"""
        logger.info("Report processor started")
        while self.is_running:
            try:
                # Get next report from queue with timeout
                try:
                    report_data = await asyncio.wait_for(self.report_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                
                logger.info(f"Processing report for {report_data['target']}")
                
                # Get available accounts
                accounts = await self.get_available_accounts(limit=3)
                
                if not accounts:
                    logger.warning("No accounts available, requeuing report")
                    await asyncio.sleep(5)
                    await self.report_queue.put(report_data)
                    self.report_queue.task_done()
                    continue
                
                # Report with each account
                success_count = 0
                for account in accounts:
                    try:
                        result = await self.report_target(
                            account,
                            report_data['target'],
                            report_data['category'],
                            report_data['text']
                        )
                        
                        if result['status'] == 'success':
                            success_count += 1
                            logger.info(f"✅ Reported {report_data['target']} with {account.phone_number}")
                        else:
                            logger.warning(f"❌ Failed to report with {account.phone_number}: {result.get('reason')}")
                        
                        # 10-second interval between reports
                        await asyncio.sleep(10)
                        
                    except Exception as e:
                        logger.error(f"Error using account {account.phone_number}: {e}")
                        await asyncio.sleep(10)
                
                logger.info(f"Report processing complete for {report_data['target']}: {success_count} successful")
                self.report_queue.task_done()
                
            except asyncio.CancelledError:
                logger.info("Report processor cancelled")
                break
            except Exception as e:
                logger.error(f"Report processor error: {e}")
                await asyncio.sleep(5)
        
        logger.info("Report processor stopped")
    
    async def _cleanup_expired_sessions(self):
        """Periodically clean up expired login sessions"""
        logger.info("Session cleanup task started")
        while self.is_running:
            try:
                await asyncio.sleep(30)  # Check every 30 seconds
                
                now = time.time()
                expired = []
                
                for phone, session_data in self.active_sessions.items():
                    session_age = now - session_data.get('created_at', 0)
                    if session_age > 180:  # 3 minutes
                        logger.info(f"Session for {phone} expired after {session_age:.1f} seconds")
                        expired.append(phone)
                
                for phone in expired:
                    await self._cleanup_phone_sessions(phone)
                
                if expired:
                    logger.info(f"Cleaned up {len(expired)} expired sessions")
                    
            except asyncio.CancelledError:
                logger.info("Cleanup task cancelled")
                break
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
        
        logger.info("Session cleanup task stopped")
    
    async def check_account_status(self, account_id):
        """Check if an account is still valid"""
        db_session = self.db.get_session()
        try:
            account = db_session.query(TelegramAccount).filter_by(id=account_id).first()
            if not account:
                return {'status': 'error', 'reason': 'account_not_found'}
            
            if not account.session_string:
                return {'status': 'error', 'reason': 'no_session_string'}
            
            client = TelegramClient(StringSession(account.session_string), API_ID, API_HASH)
            await client.connect()
            
            if await client.is_user_authorized():
                await client.disconnect()
                return {'status': 'active'}
            else:
                account.is_active = False
                db_session.commit()
                await client.disconnect()
                return {'status': 'inactive'}
                
        except Exception as e:
            logger.error(f"Error checking account: {e}")
            return {'status': 'error', 'reason': str(e)}
        finally:
            db_session.close()
    
    async def remove_account(self, account_id):
        """Remove an account from the system"""
        db_session = self.db.get_session()
        try:
            account = db_session.query(TelegramAccount).filter_by(id=account_id).first()
            if account:
                db_session.delete(account)
                db_session.commit()
                return {'status': 'success'}
            return {'status': 'error', 'reason': 'account_not_found'}
        except Exception as e:
            logger.error(f"Error removing account: {e}")
            return {'status': 'error', 'reason': str(e)}
        finally:
            db_session.close()
    
    async def get_account_stats(self):
        """Get statistics about all accounts"""
        db_session = self.db.get_session()
        try:
            total = db_session.query(TelegramAccount).count()
            active = db_session.query(TelegramAccount).filter_by(is_active=True).count()
            available = db_session.query(TelegramAccount).filter_by(status='available', is_active=True).count()
            with_session = db_session.query(TelegramAccount).filter(TelegramAccount.session_string.isnot(None)).count()
            banned = db_session.query(TelegramAccount).filter_by(is_active=False).count()
            
            logger.info(f"DB Stats - Total: {total}, Active: {active}, With Session: {with_session}")
            
            return {
                'total': total,
                'active': active,
                'available': available,
                'with_session': with_session,
                'banned': banned
            }
        except Exception as e:
            logger.error(f"Error getting account stats: {e}")
            return {
                'total': 0,
                'active': 0,
                'available': 0,
                'with_session': 0,
                'banned': 0
            }
        finally:
            db_session.close()
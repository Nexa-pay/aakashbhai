from sqlalchemy import Column, Integer, String, Boolean, DateTime, Text, BigInteger
from sqlalchemy.ext.declarative import declarative_base
from datetime import datetime, timezone

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String, nullable=True)
    tokens = Column(Integer, default=10)
    role = Column(String, default='user')  # 'owner', 'admin', 'user'
    is_active = Column(Boolean, default=True)
    reports_made = Column(Integer, default=0)
    joined_date = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_active = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

class TelegramAccount(Base):
    __tablename__ = 'telegram_accounts'
    
    id = Column(Integer, primary_key=True)
    phone_number = Column(String, unique=True, nullable=False, index=True)
    session_string = Column(Text)
    is_active = Column(Boolean, default=True)
    added_by = Column(BigInteger, nullable=True)
    added_date = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    reports_count = Column(Integer, default=0)
    status = Column(String, default='available')  # 'available', 'busy', 'banned', 'cooldown'
    last_used = Column(DateTime, nullable=True)
    cooldown_until = Column(DateTime, nullable=True)

class Report(Base):
    __tablename__ = 'reports'
    
    id = Column(Integer, primary_key=True)
    target_type = Column(String)  # 'user', 'group', 'channel'
    target_id = Column(String, nullable=True)
    target_username = Column(String, nullable=True, index=True)
    category = Column(String)
    custom_text = Column(Text)
    reported_by = Column(BigInteger, nullable=False, index=True)
    accounts_used = Column(Text, nullable=True)  # JSON string
    status = Column(String, default='pending', index=True)  # 'pending', 'in_progress', 'completed', 'failed'
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    completed_at = Column(DateTime, nullable=True)
    retry_count = Column(Integer, default=0)

class Transaction(Base):
    __tablename__ = 'transactions'
    
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    amount = Column(Integer)
    type = Column(String)  # 'purchase', 'reward', 'deduction'
    description = Column(String, nullable=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
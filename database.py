import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, scoped_session
from models import Base
import logging
import time

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, database_url):
        self.database_url = database_url
        self.engine = None
        self.Session = None
        self.connect()
    
    def connect(self):
        """Establish database connection with retry logic"""
        max_retries = 5
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                self.engine = create_engine(
                    self.database_url,
                    pool_size=10,
                    max_overflow=20,
                    pool_pre_ping=True,
                    pool_recycle=3600,
                    pool_use_lifo=True,
                    connect_args={
                        'connect_timeout': 10,
                        'keepalives': 1,
                        'keepalives_idle': 30,
                        'keepalives_interval': 10,
                        'keepalives_count': 5
                    },
                    echo=False
                )
                
                # Test connection
                with self.engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                    conn.commit()
                
                # Create tables
                Base.metadata.create_all(self.engine)
                
                # Create session factory
                session_factory = sessionmaker(bind=self.engine)
                self.Session = scoped_session(session_factory)
                
                logger.info(f"✅ Database connected successfully (attempt {attempt + 1})")
                return
                
            except Exception as e:
                logger.error(f"❌ Database connection failed (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay * (attempt + 1))
                else:
                    logger.critical("❌ Failed to connect to database after multiple attempts")
                    raise
    
    def get_session(self):
        """Get a new database session"""
        if not self.Session:
            self.connect()
        return self.Session()
    
    def close_session(self, session=None):
        """Close database session"""
        if session:
            session.close()
        if self.Session:
            self.Session.remove()
    
    def health_check(self):
        """Check database health"""
        try:
            session = self.get_session()
            session.execute(text("SELECT 1"))
            session.close()
            return True
        except:
            return False

# Global database instance
db = None

def init_db(database_url):
    """Initialize database globally"""
    global db
    db = Database(database_url)
    return db

def get_db():
    """Get database instance"""
    global db
    if not db:
        raise Exception("Database not initialized. Call init_db first.")
    return db
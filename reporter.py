import asyncio
import json
import logging
from datetime import datetime, timezone
from database import get_db
from models import Report, User
from account_manager import AccountManager

logger = logging.getLogger(__name__)

class Reporter:
    def __init__(self, account_manager):
        self.account_manager = account_manager
        self.db = get_db()
    
    async def create_report(self, target_type, target, category, text, user_id):
        """Create a new report record"""
        db_session = self.db.get_session()
        try:
            report = Report(
                target_type=target_type,
                target_username=target if target.startswith('@') else None,
                target_id=target if not target.startswith('@') else None,
                category=category,
                custom_text=text,
                reported_by=user_id,
                status='pending'
            )
            db_session.add(report)
            db_session.commit()
            return report.id
        finally:
            db_session.close()
    
    async def bulk_report(self, targets, category, text, user_id):
        """Report multiple targets"""
        db_session = self.db.get_session()
        try:
            # Check user tokens
            user = db_session.query(User).filter_by(user_id=user_id).first()
            if not user:
                return {'status': 'error', 'message': 'User not found'}
            
            required_tokens = len(targets)
            if user.role != 'owner' and user.tokens < required_tokens:
                return {
                    'status': 'error',
                    'message': f'Insufficient tokens. Need {required_tokens}, have {user.tokens}'
                }
            
            report_ids = []
            successful = 0
            failed = 0
            
            for target in targets:
                target_username = target.get('username') or target.get('id')
                
                # Create report record
                report = Report(
                    target_type=target['type'],
                    target_username=target_username if target_username and target_username.startswith('@') else None,
                    target_id=target_username if target_username and not target_username.startswith('@') else None,
                    category=category,
                    custom_text=text,
                    reported_by=user_id,
                    status='pending'
                )
                db_session.add(report)
                db_session.commit()
                report_ids.append(report.id)
                
                # Queue for reporting
                await self.account_manager.queue_report(
                    target_username,
                    category,
                    text,
                    user_id
                )
                
                successful += 1
            
            # Deduct tokens if not owner
            if user.role != 'owner':
                user.tokens -= required_tokens
                user.reports_made += successful
                db_session.commit()
            
            return {
                'status': 'success',
                'report_ids': report_ids,
                'summary': {
                    'total': len(targets),
                    'successful': successful,
                    'failed': failed
                }
            }
            
        except Exception as e:
            logger.error(f"Bulk report error: {e}")
            db_session.rollback()
            return {'status': 'error', 'message': str(e)}
        finally:
            db_session.close()
    
    async def get_report_status(self, report_id):
        """Get status of a report"""
        db_session = self.db.get_session()
        try:
            report = db_session.query(Report).filter_by(id=report_id).first()
            if not report:
                return {'status': 'error', 'message': 'Report not found'}
            
            return {
                'status': 'success',
                'report': {
                    'id': report.id,
                    'target': report.target_username or report.target_id,
                    'category': report.category,
                    'status': report.status,
                    'error': report.error_message,
                    'created_at': report.created_at.isoformat() if report.created_at else None,
                    'completed_at': report.completed_at.isoformat() if report.completed_at else None
                }
            }
        finally:
            db_session.close()
    
    async def get_user_reports(self, user_id, limit=10):
        """Get recent reports for a user"""
        db_session = self.db.get_session()
        try:
            reports = db_session.query(Report).filter_by(
                reported_by=user_id
            ).order_by(
                Report.created_at.desc()
            ).limit(limit).all()
            
            return [{
                'id': r.id,
                'target': r.target_username or r.target_id,
                'category': r.category,
                'status': r.status,
                'error': r.error_message,
                'created_at': r.created_at.isoformat() if r.created_at else None
            } for r in reports]
        finally:
            db_session.close()
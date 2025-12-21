"""Admin log model for tracking admin actions."""
from datetime import datetime
from app import db


class AdminLog(db.Model):
    """Log of admin actions for audit trail."""
    __tablename__ = "admin_logs"
    
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    
    # Action details
    action = db.Column(db.String(50), nullable=False)  # ban, unban, subscription_update, etc.
    target_user_id = db.Column(db.Integer, nullable=True)  # The user affected by the action
    details = db.Column(db.Text, nullable=True)  # JSON or text details
    
    # Timestamp
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # IP address for security
    ip_address = db.Column(db.String(45), nullable=True)
    
    # Relationship to admin user
    admin = db.relationship('User', foreign_keys=[admin_id], backref='admin_actions')
    
    @staticmethod
    def log_action(admin_id: int, action: str, target_user_id: int = None, 
                   details: str = None, ip_address: str = None):
        """Create a new admin log entry."""
        log = AdminLog(
            admin_id=admin_id,
            action=action,
            target_user_id=target_user_id,
            details=details,
            ip_address=ip_address
        )
        db.session.add(log)
        db.session.commit()
        return log
    
    def __repr__(self):
        return f'<AdminLog {self.action} by {self.admin_id}>'

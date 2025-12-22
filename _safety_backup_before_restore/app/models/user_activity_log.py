from datetime import datetime
from app import db

class UserActivityLog(db.Model):
    """Log of user actions for admin visibility."""
    __tablename__ = "user_activity_logs"
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    
    action = db.Column(db.String(50), nullable=False) # e.g. 'update_stock', 'delete_product'
    marketplace = db.Column(db.String(20), nullable=True) # e.g. 'trendyol', 'n11'
    details = db.Column(db.Text, nullable=True) # JSON or descriptive text
    
    ip_address = db.Column(db.String(45), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    
    user = db.relationship('User', backref='activity_logs')
    
    def __repr__(self):
        return f'<UserLog {self.action} by {self.user_id}>'

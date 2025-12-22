from datetime import datetime
from app import db

class Announcement(db.Model):
    __tablename__ = 'announcements'

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    content = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    priority = db.Column(db.String(20), default='normal') # normal, high, critical
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)
    
    # Optional: Target specific user roles or IDs if needed later
    # target_role = db.Column(db.String(50), nullable=True) 

    def __repr__(self):
        return f'<Announcement {self.title}>'

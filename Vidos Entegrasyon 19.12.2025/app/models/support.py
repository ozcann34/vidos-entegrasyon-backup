"""Support Ticket models."""
from datetime import datetime
from app import db

class SupportTicket(db.Model):
    """Support Ticket model."""
    __tablename__ = 'support_tickets'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    subject = db.Column(db.String(200), nullable=False)
    status = db.Column(db.String(20), default='open')  # open, answered, resolved, closed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    messages = db.relationship('SupportMessage', backref='ticket', lazy=True, cascade="all, delete-orphan")
    user = db.relationship('User', backref='support_tickets', lazy=True)

    def __repr__(self):
        return f'<SupportTicket {self.id} - {self.subject}>'

class SupportMessage(db.Model):
    """Support Message model."""
    __tablename__ = 'support_messages'

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey('support_tickets.id'), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True) # Null if system/admin (or we can use a flag)
    # Better approach: store sender_id. If sender_id match ticket.user_id -> user, else -> admin. 
    # Or explicitly add is_admin_reply flag.
    is_admin_reply = db.Column(db.Boolean, default=False)
    
    message = db.Column(db.Text, nullable=False)
    attachment_path = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    sender = db.relationship('User', backref='support_messages', lazy=True)

    def __repr__(self):
        return f'<SupportMessage {self.id} for Ticket {self.ticket_id}>'

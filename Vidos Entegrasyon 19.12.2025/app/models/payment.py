"""Payment model for tracking transactions and payment history."""
from datetime import datetime
from app import db


class Payment(db.Model):
    """Payment transaction tracking model."""
    __tablename__ = "payments"
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    subscription_id = db.Column(db.Integer, db.ForeignKey('subscriptions.id'), nullable=True)
    
    # Payment details
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(10), default='TRY')
    plan = db.Column(db.String(50), nullable=False)  # basic, pro, enterprise
    billing_cycle = db.Column(db.String(20), default='monthly')  # monthly, yearly
    
    # Gateway information
    gateway = db.Column(db.String(50), nullable=True)  # shopier, iyzico, etc.
    transaction_id = db.Column(db.String(200), nullable=True)  # Gateway transaction ID
    payment_reference = db.Column(db.String(200), nullable=True)  # Internal reference
    
    # Status tracking
    status = db.Column(db.String(20), default='pending')  # pending, completed, failed, refunded, cancelled
    
    # Metadata
    payment_method = db.Column(db.String(50), nullable=True)  # credit_card, bank_transfer, etc.
    ip_address = db.Column(db.String(50), nullable=True)
    user_agent = db.Column(db.Text, nullable=True)
    
    # Additional payment data (JSON)
    payment_metadata = db.Column(db.Text, nullable=True)  # JSON string for extra data (renamed from 'metadata')
    
    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    user = db.relationship('User', backref='payments')
    subscription = db.relationship('Subscription', backref='payments')
    
    @property
    def is_completed(self) -> bool:
        """Check if payment is completed."""
        return self.status == 'completed'
    
    @property
    def is_pending(self) -> bool:
        """Check if payment is pending."""
        return self.status == 'pending'
    
    @property
    def is_failed(self) -> bool:
        """Check if payment failed."""
        return self.status == 'failed'
    
    def mark_completed(self, transaction_id=None):
        """Mark payment as completed."""
        self.status = 'completed'
        self.completed_at = datetime.utcnow()
        if transaction_id:
            self.transaction_id = transaction_id
    
    def mark_failed(self):
        """Mark payment as failed."""
        self.status = 'failed'
    
    def __repr__(self):
        return f'<Payment {self.id} - {self.plan} - {self.status}>'

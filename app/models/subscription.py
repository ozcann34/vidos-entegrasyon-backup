"""Subscription model for managing user subscriptions."""
from datetime import datetime
from app import db


class Subscription(db.Model):
    """Subscription model for user plans."""
    __tablename__ = "subscriptions"
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, unique=True)
    
    # Plan details
    plan = db.Column(db.String(50), default='free')  # free, pro, enterprise
    billing_cycle = db.Column(db.String(20), default='monthly')  # monthly, yearly
    status = db.Column(db.String(20), default='active')  # active, expired, cancelled, suspended
    is_approved = db.Column(db.Boolean, default=False)  # Admin approval flag
    
    # Dates
    start_date = db.Column(db.DateTime, default=datetime.utcnow)
    end_date = db.Column(db.DateTime, nullable=True)  # NULL = unlimited
    
    # Metadata
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Payment details
    payment_reference = db.Column(db.String(100), nullable=True)
    price_paid = db.Column(db.Float, default=0.0)
    currency = db.Column(db.String(10), default='TRY')
    
    # Plan limits (configurable per plan)
    max_products = db.Column(db.Integer, default=100)  # -1 = unlimited
    max_marketplaces = db.Column(db.Integer, default=1)  # -1 = unlimited
    max_xml_sources = db.Column(db.Integer, default=1)  # -1 = unlimited
    
    @property
    def is_active(self) -> bool:
        """Check if subscription is currently active."""
        if self.status != 'active':
            return False
        if self.end_date is None:
            return True
        return datetime.utcnow() < self.end_date
    
    @property
    def days_remaining(self) -> int:
        """Days remaining in subscription."""
        if self.end_date is None:
            return -1  # Unlimited
        delta = self.end_date - datetime.utcnow()
        return max(0, delta.days)
    
    @property
    def plan_display_name(self) -> str:
        """Get display name for plan."""
        plan_names = {
            'free': 'Ücretsiz',
            'pro': 'Pro',
            'enterprise': 'Enterprise'
        }
        return plan_names.get(self.plan, self.plan.title())
    
    def __repr__(self):
        return f'<Subscription {self.user_id} - {self.plan}>'

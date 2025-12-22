from datetime import datetime
from app import db

class Expense(db.Model):
    __tablename__ = 'expenses'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    
    category = db.Column(db.String(100), nullable=False, index=True) # Kira, Personel, Pazarlama, Vergi, Diğer
    amount = db.Column(db.Float, nullable=False, default=0.0)
    currency = db.Column(db.String(3), default='TRY')
    description = db.Column(db.String(255), nullable=True)
    
    date = db.Column(db.Date, nullable=False, default=lambda: datetime.utcnow().date())
    is_recurring = db.Column(db.Boolean, default=False) # Aylık tekrar eden gider mi?
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Expense {self.category}: {self.amount} {self.currency}>"

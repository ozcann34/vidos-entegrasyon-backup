from app import db
from datetime import datetime

class Blacklist(db.Model):
    __tablename__ = 'blacklist'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    type = db.Column(db.String(20), nullable=False) # 'brand' or 'category'
    value = db.Column(db.String(255), nullable=False) # Brand name or Category name
    reason = db.Column(db.String(255), nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Blacklist {self.type}:{self.value}>"

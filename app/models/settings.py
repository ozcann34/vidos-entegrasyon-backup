import json
from typing import Dict, Any
from app import db

class Setting(db.Model):
    __tablename__ = "settings"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    key = db.Column(db.String, nullable=False)
    value = db.Column(db.Text, nullable=True)
    
    # Unique constraint: one key per user
    __table_args__ = (
        db.UniqueConstraint('user_id', 'key', name='unique_user_key'),
    )

    @staticmethod
    def get(k, default=None, user_id=None):
        try:
            s = Setting.query.filter_by(key=k, user_id=user_id).first()
            return s.value if s else default
            return s.value if s else default
        except Exception as e:
            print(f"Error getting setting {k}: {str(e)}")
            return default

    @staticmethod
    def set(k, v, user_id=None):
        try:
            if v is None:
                v = ""
            else:
                v = str(v)
            
            s = Setting.query.filter_by(key=k, user_id=user_id).first()
            
            if s:
                s.value = v
            else:
                db.session.add(Setting(key=k, value=v, user_id=user_id))
            
            db.session.commit()
            print(f"Setting saved - {k}: {v}")
            return True
        except Exception as e:
            print(f"Error saving setting {k}: {str(e)}")
            db.session.rollback()
            return False

class BatchLog(db.Model):
    __tablename__ = "batch_logs"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    batch_id = db.Column(db.String, unique=True, nullable=False)
    timestamp = db.Column(db.String, nullable=False)
    success = db.Column(db.Boolean, nullable=False)
    product_count = db.Column(db.Integer, default=0)
    success_count = db.Column(db.Integer, default=0)
    fail_count = db.Column(db.Integer, default=0)
    details_json = db.Column(db.Text, nullable=True)
    marketplace = db.Column(db.String, default='trendyol')
    job_type = db.Column(db.String, nullable=True)

    def get_details(self) -> Dict[str, Any]:
        return json.loads(self.details_json) if self.details_json else {}


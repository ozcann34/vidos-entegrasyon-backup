import json
from typing import Dict, Any
from datetime import datetime
from app import db


class AutoSync(db.Model):
    """Otomatik senkronizasyon ayarları"""
    __tablename__ = "auto_sync"
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    marketplace = db.Column(db.String(50), nullable=False)
    enabled = db.Column(db.Boolean, default=False, nullable=False)
    last_sync = db.Column(db.String, nullable=True)  # ISO format timestamp
    sync_interval_minutes = db.Column(db.Integer, default=60, nullable=False)
    created_at = db.Column(db.String, default=lambda: datetime.utcnow().isoformat())
    updated_at = db.Column(db.String, default=lambda: datetime.utcnow().isoformat(), onupdate=lambda: datetime.utcnow().isoformat())
    
    # Unique constraint per user
    __table_args__ = (
        db.UniqueConstraint('user_id', 'marketplace', name='unique_user_marketplace'),
    )
    
    @staticmethod
    def get_or_create(marketplace: str) -> 'AutoSync':
        """Get or create AutoSync record for marketplace"""
        sync = AutoSync.query.filter_by(marketplace=marketplace).first()
        if not sync:
            sync = AutoSync(marketplace=marketplace)
            db.session.add(sync)
            db.session.commit()
        return sync
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            'id': self.id,
            'marketplace': self.marketplace,
            'enabled': self.enabled,
            'last_sync': self.last_sync,
            'sync_interval_minutes': self.sync_interval_minutes,
            'created_at': self.created_at,
            'updated_at': self.updated_at
        }


class SyncLog(db.Model):
    """Senkronizasyon log kayıtları"""
    __tablename__ = "sync_logs"
    
    id = db.Column(db.Integer, primary_key=True)
    marketplace = db.Column(db.String(50), nullable=False)
    timestamp = db.Column(db.String, nullable=False, default=lambda: datetime.utcnow().isoformat())
    products_updated = db.Column(db.Integer, default=0)
    stock_changes = db.Column(db.Integer, default=0)
    price_changes = db.Column(db.Integer, default=0)
    success = db.Column(db.Boolean, default=True, nullable=False)
    details_json = db.Column(db.Text, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    
    def get_details(self) -> Dict[str, Any]:
        """Parse details JSON"""
        if self.details_json:
            try:
                return json.loads(self.details_json)
            except:
                return {}
        return {}
    
    def set_details(self, details: Dict[str, Any]):
        """Set details from dictionary"""
        self.details_json = json.dumps(details, ensure_ascii=False)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary"""
        return {
            'id': self.id,
            'marketplace': self.marketplace,
            'timestamp': self.timestamp,
            'products_updated': self.products_updated,
            'stock_changes': self.stock_changes,
            'price_changes': self.price_changes,
            'success': self.success,
            'details': self.get_details(),
            'error_message': self.error_message
        }

from app import db
from datetime import datetime

class SyncException(db.Model):
    """
    Senkronizasyon Harici Listesi (Exclusion List)
    Stok kodu veya barkodu bu listede olan ürünler:
    1. Otomatik senkronizasyon ile güncellenmez.
    2. XML'de olmasa bile stokları sıfırlanmaz (Diff Sync koruması).
    """
    __tablename__ = "sync_exceptions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    
    # Eşleşme değeri (Genellikle Stok Kodu, opsiyonel olarak Barkod)
    value = db.Column(db.String(100), nullable=False, index=True)
    
    # Neye göre eşleştiği: 'stock_code' veya 'barcode'
    # Kullanıcı "stok kodu" dediği için varsayılan 'stock_code' olacak ama esneklik iyidir.
    match_type = db.Column(db.String(20), default='stock_code') 
    
    note = db.Column(db.String(255), nullable=True) # Kullanıcı notu (örn: "Mağaza özel ürün")
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'value', 'match_type', name='unique_user_exception'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'value': self.value,
            'match_type': self.match_type,
            'note': self.note,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else None
        }

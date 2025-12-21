"""
Excel File Model - Kalıcı Excel dosyası depolama
"""
from datetime import datetime
from app import db


class ExcelFile(db.Model):
    """Yüklenen Excel dosyalarını saklar"""
    __tablename__ = 'excel_files'
    
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    file_id = db.Column(db.String(100), unique=True, nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    total_products = db.Column(db.Integer, default=0)
    matched_columns = db.Column(db.Integer, default=0)
    total_columns = db.Column(db.Integer, default=0)
    column_mapping = db.Column(db.Text)  # JSON string
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def to_dict(self):
        import json
        return {
            'id': self.id,
            'file_id': self.file_id,
            'filename': self.filename,
            'original_filename': self.original_filename,
            'total_products': self.total_products,
            'matched_columns': self.matched_columns,
            'total_columns': self.total_columns,
            'column_mapping': json.loads(self.column_mapping) if self.column_mapping else {},
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
    
    @classmethod
    def get_all(cls, user_id=None):
        query = cls.query
        if user_id:
            query = query.filter_by(user_id=user_id)
        return query.order_by(cls.created_at.desc()).all()
    
    @classmethod
    def get_by_file_id(cls, file_id, user_id=None):
        query = cls.query.filter_by(file_id=file_id)
        if user_id:
            query = query.filter_by(user_id=user_id)
        return query.first()

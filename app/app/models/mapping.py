from datetime import datetime
from app import db

class CategoryMapping(db.Model):
    __tablename__ = 'category_mappings'

    id = db.Column(db.Integer, primary_key=True)
    source_category = db.Column(db.String(255), nullable=False) # XML'den gelen kategori
    marketplace = db.Column(db.String(50), nullable=False) # trendyol, pazarama vb.
    target_category_id = db.Column(db.Integer, nullable=False) # Pazaryeri Kategori ID
    target_category_path = db.Column(db.String(500), nullable=True) # "Giyim > Erkek..."
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('source_category', 'marketplace', name='uq_category_mapping'),
        db.Index('idx_cat_mapping_source', 'source_category'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'source_category': self.source_category,
            'marketplace': self.marketplace,
            'target_category_id': self.target_category_id,
            'target_category_path': self.target_category_path,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

class BrandMapping(db.Model):
    __tablename__ = 'brand_mappings'

    id = db.Column(db.Integer, primary_key=True)
    source_brand = db.Column(db.String(255), nullable=False) # XML'den gelen marka
    marketplace = db.Column(db.String(50), nullable=False)
    target_brand_id = db.Column(db.Integer, nullable=False)
    target_brand_name = db.Column(db.String(255), nullable=True)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('source_brand', 'marketplace', name='uq_brand_mapping'),
        db.Index('idx_brand_mapping_source', 'source_brand'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'source_brand': self.source_brand,
            'marketplace': self.marketplace,
            'target_brand_id': self.target_brand_id,
            'target_brand_name': self.target_brand_name,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

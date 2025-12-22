from datetime import datetime
from app import db

class SupplierXML(db.Model):
    __tablename__ = "supplier_xmls"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    name = db.Column(db.String, nullable=False)
    url = db.Column(db.Text, nullable=False)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.String, default=lambda: datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    barcode = db.Column(db.String, nullable=False, index=True)
    stockCode = db.Column(db.String, nullable=True)
    title = db.Column(db.String, nullable=False)
    description = db.Column(db.Text, nullable=True)
    listPrice = db.Column(db.Float, nullable=True)
    vatRate = db.Column(db.Float, nullable=True)
    cost_price = db.Column(db.Float, default=0.0) # Maliyet Fiyatı
    cost_currency = db.Column(db.String(3), default='TRY')
    quantity = db.Column(db.Integer, default=0)
    categoryId = db.Column(db.Integer, default=0)
    brandId = db.Column(db.Integer, default=0)
    top_category = db.Column(db.String, nullable=True) # Category display name
    brand = db.Column(db.String, nullable=True)       # Brand name string
    desi = db.Column(db.Float, default=1.0)           # Product desi
    images_json = db.Column(db.Text, nullable=True)   # JSON string list of {url}
    attributes_json = db.Column(db.Text, nullable=True) # JSON for extra attributes
    
    # Per-marketplace extra data for manual products
    marketplace_id = db.Column(db.String, nullable=True) # trendyol, hepsiburada...
    marketplace_category_id = db.Column(db.String, nullable=True)
    marketplace_attributes_json = db.Column(db.Text, nullable=True) # JSON list of {id, name, value}
    
    xml_source_id = db.Column(db.Integer, db.ForeignKey('supplier_xmls.id'), nullable=True)
    xml_source = db.relationship('SupplierXML', backref='products_linked')
    
    is_archived = db.Column(db.Boolean, default=False, index=True)
    archived_at = db.Column(db.DateTime, nullable=True)
    
    # Unique constraint per user
    __table_args__ = (
        db.UniqueConstraint('user_id', 'barcode', name='unique_user_barcode'),
    )

    @property
    def get_images(self):
        import json
        if not self.images_json:
            return []
        try:
            return json.loads(self.images_json)
        except:
            return []

class MarketplaceProduct(db.Model):
    __tablename__ = "marketplace_products"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    marketplace = db.Column(db.String(50), nullable=False, index=True) # trendyol, n11, pazarama, idefix
    
    barcode = db.Column(db.String, nullable=False, index=True)
    stock_code = db.Column(db.String, nullable=True, index=True)
    title = db.Column(db.String, nullable=True)
    description = db.Column(db.Text, nullable=True)
    
    price = db.Column(db.Float, default=0.0)
    sale_price = db.Column(db.Float, default=0.0)
    quantity = db.Column(db.Integer, default=0)
    
    brand = db.Column(db.String, nullable=True)
    category = db.Column(db.String, nullable=True)
    
    status = db.Column(db.String, nullable=True) # Active, Suspended, etc.
    approval_status = db.Column(db.String, nullable=True) # Approved, Rejected
    on_sale = db.Column(db.Boolean, default=True)
    
    images_json = db.Column(db.Text, nullable=True)
    raw_data = db.Column(db.Text, nullable=True) # Full JSON from API
    
    last_sync_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    __table_args__ = (
        db.UniqueConstraint('user_id', 'marketplace', 'barcode', name='unique_mp_product'),
    )

    @property
    def get_images(self):
        import json
        if not self.images_json:
            return []
        try:
            return json.loads(self.images_json)
        except:
            return []


from datetime import datetime
from app import db

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
    
    xml_source_id = db.Column(db.Integer, nullable=True)
    
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

class SupplierXML(db.Model):
    __tablename__ = "supplier_xmls"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    name = db.Column(db.String, nullable=False)
    url = db.Column(db.Text, nullable=False)
    active = db.Column(db.Boolean, default=True)
    use_random_barcode = db.Column(db.Boolean, default=False) # Kullanıcı isteğine bağlı random barkod
    last_cached_at = db.Column(db.DateTime, nullable=True)     # Son cache'lenme zamanı
    created_at = db.Column(db.String, default=lambda: datetime.now().strftime('%Y-%m-%d %H:%M:%S'))


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
    xml_source_id = db.Column(db.Integer, nullable=True, index=True)
    
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


class CachedXmlProduct(db.Model):
    __tablename__ = 'cached_xml_products'
    
    id = db.Column(db.Integer, primary_key=True)
    xml_source_id = db.Column(db.Integer, db.ForeignKey('supplier_xmls.id'), nullable=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    
    stock_code = db.Column(db.String(200), index=True, nullable=False)
    barcode = db.Column(db.String(200), index=True)
    title = db.Column(db.String(500))
    price = db.Column(db.Float, default=0.0)
    quantity = db.Column(db.Integer, default=0)
    brand = db.Column(db.String(200))
    category = db.Column(db.String(500))
    images_json = db.Column(db.Text)
    raw_data = db.Column(db.Text) # Kaynak verinin tamamı (JSON)
    last_updated = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        db.Index('idx_xml_stock_code', 'xml_source_id', 'stock_code'),
    )

class PersistentJob(db.Model):
    __tablename__ = 'persistent_jobs'
    
    id = db.Column(db.String(50), primary_key=True) # UUID
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    marketplace = db.Column(db.String(50), index=True)
    job_type = db.Column(db.String(100), index=True)
    
    status = db.Column(db.String(20), default='pending', index=True) # pending, running, completed, failed, cancelled
    progress_current = db.Column(db.Integer, default=0)
    progress_total = db.Column(db.Integer, default=100)
    progress_message = db.Column(db.String(500))
    
    params_json = db.Column(db.Text) # JSON serialized parameters
    result_json = db.Column(db.Text) # JSON serialized result
    errors_json = db.Column(db.Text) # JSON serialized errors list
    logs_json = db.Column(db.Text)   # JSON serialized logs
    
    cancel_requested = db.Column(db.Boolean, default=False)
    
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    started_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    def get_params(self):
        import json
        return json.loads(self.params_json) if self.params_json else {}
    
    def get_logs(self):
        import json
        return json.loads(self.logs_json) if self.logs_json else []

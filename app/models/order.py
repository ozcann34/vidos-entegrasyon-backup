from datetime import datetime
from app import db

class Order(db.Model):
    __tablename__ = 'orders'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    marketplace = db.Column(db.String(50), nullable=False, index=True) # trendyol, pazarama, etc.
    marketplace_order_id = db.Column(db.String(100), nullable=False, index=True) # Remote ID
    order_number = db.Column(db.String(100), nullable=False, index=True) # Display number
    shipment_package_id = db.Column(db.BigInteger, nullable=True, index=True)  # Trendyol package ID for status updates
    cargo_code = db.Column(db.String(50), nullable=True) # Marketplace Cargo/Campaign Code
    status = db.Column(db.String(50), default='Created') # Created, Shipped, Delivered, Cancelled
    
    total_price = db.Column(db.Float, default=0.0)
    currency = db.Column(db.String(3), default='TRY')
    
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=True)
    customer = db.relationship('Customer', backref='orders')
    customer_name = db.Column(db.String(255), nullable=True)  # Cached customer name for display
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Financial Analysis Fields
    commission_amount = db.Column(db.Float, default=0.0) # Komisyon
    shipping_fee = db.Column(db.Float, default=0.0)    # Kargo ücreti
    service_fee = db.Column(db.Float, default=0.0)     # Hizmet/İşlem bedeli
    tax_amount = db.Column(db.Float, default=0.0)      # Hesaplanan KDV
    total_deductions = db.Column(db.Float, default=0.0) # Toplam kesinti
    net_profit = db.Column(db.Float, default=0.0)       # Net Kâr (Hesaplanmış)
    
    items_json = db.Column(db.Text, nullable=True) # JSON details if needed separate from items table

    # Raw JSON data for debugging/fallback
    raw_data = db.Column(db.Text, nullable=True)
    admin_note = db.Column(db.Text, nullable=True) # Admin/System notes (e.g. BUG-Z code)

    items = db.relationship('OrderItem', backref='order', cascade='all, delete-orphan')
    
    # Unique constraint per user
    __table_args__ = (
        db.UniqueConstraint('user_id', 'marketplace_order_id', name='unique_user_marketplace_order'),
    )

    def __repr__(self):
        return f"<Order {self.marketplace}#{self.order_number}>"

class OrderItem(db.Model):
    __tablename__ = 'order_items'

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=True) # Link to local product if exists
    barcode = db.Column(db.String(100), index=True)
    sku = db.Column(db.String(100), nullable=True)  # Merchant SKU
    product_name = db.Column(db.String(255))
    
    quantity = db.Column(db.Integer, default=1)
    price = db.Column(db.Float, default=0.0)  # Line item total price
    unit_price = db.Column(db.Float, default=0.0)
    vat_rate = db.Column(db.Float, default=20.0) # KDV Oranı
    currency = db.Column(db.String(3), default='TRY')
    
    status = db.Column(db.String(50), default='Active') # Active, Cancelled, Returned

    def __repr__(self):
        return f"<OrderItem {self.barcode} x{self.quantity}>"

class Customer(db.Model):
    __tablename__ = 'customers'

    id = db.Column(db.Integer, primary_key=True)
    # Marketplaces might not give full details or consistent IDs, so we might duplicate customers per marketplace or try to dedupe by email/phone
    # For simplicity, we'll store what we get.
    
    first_name = db.Column(db.String(100))
    last_name = db.Column(db.String(100))
    email = db.Column(db.String(150), index=True, nullable=True)
    phone = db.Column(db.String(50), nullable=True)
    
    city = db.Column(db.String(100))
    district = db.Column(db.String(100))
    address = db.Column(db.Text)
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Customer {self.first_name} {self.last_name}>"

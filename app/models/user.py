"""User model for authentication and authorization."""
import json
from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from app import db


# Default permissions for new users
DEFAULT_PERMISSIONS = {
    'dashboard': True,
    'xml_products': True,
    'excel_products': True,
    'trendyol': True,
    'pazarama': True,
    'hepsiburada': True,
    'idefix': True,
    'batch_logs': True,
    'orders': True,
    'auto_sync': True,
    'reports': True,
    'settings': True,
    'assistant': True,
    'product_edit': True,
    'product_delete': True,
    'order_sync': True,
    'ikas': True,
}


class User(db.Model, UserMixin):
    """User model for multi-tenant system."""
    __tablename__ = "users"
    
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    full_name = db.Column(db.String(100), nullable=True)
    
    # Detailed Company & Personal Information
    company_title = db.Column(db.String(200), nullable=True)  # Ünvan
    first_name = db.Column(db.String(100), nullable=True)  # Ad
    last_name = db.Column(db.String(100), nullable=True)  # Soyad
    tc_no = db.Column(db.String(11), nullable=True)  # TC Kimlik No
    tax_no = db.Column(db.String(20), nullable=True)  # Vergi Numarası
    tax_office = db.Column(db.String(100), nullable=True)  # Vergi Dairesi
    phone = db.Column(db.String(20), nullable=True)  # Cep Telefonu
    
    # Address Information
    country = db.Column(db.String(100), nullable=True, default='Türkiye')  # Ülke
    city = db.Column(db.String(100), nullable=True)  # İl
    district = db.Column(db.String(100), nullable=True)  # İlçe
    address = db.Column(db.Text, nullable=True)  # Adres
    
    # Legacy fields (keep for compatibility)
    company_name = db.Column(db.String(200), nullable=True)

    
    # Status flags
    is_admin = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    is_banned = db.Column(db.Boolean, default=False)
    ban_reason = db.Column(db.Text, nullable=True)
    is_email_verified = db.Column(db.Boolean, default=False)
    
    # Permissions (JSON string)
    permissions_json = db.Column(db.Text, nullable=True)
    
    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True)
    
    # Verification & Reset tokens
    email_otp = db.Column(db.String(6), nullable=True)
    otp_expiry = db.Column(db.DateTime, nullable=True)
    reset_token = db.Column(db.String(100), nullable=True)
    reset_token_expiry = db.Column(db.DateTime, nullable=True)
    
    # Relationships
    subscription = db.relationship('Subscription', backref='user', uselist=False, lazy=True)
    settings = db.relationship('Setting', backref='user', lazy=True)
    products = db.relationship('Product', backref='user', lazy=True)
    supplier_xmls = db.relationship('SupplierXML', backref='user', lazy=True)
    batch_logs = db.relationship('BatchLog', backref='user', lazy=True)
    orders = db.relationship('Order', backref='user', lazy=True)
    auto_syncs = db.relationship('AutoSync', backref='user', lazy=True)
    excel_files = db.relationship('ExcelFile', backref='user', lazy=True)
    
    def set_password(self, password: str):
        """Hash and set password."""
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password: str) -> bool:
        """Check password against hash."""
        return check_password_hash(self.password_hash, password)
    
    @property
    def permissions(self) -> dict:
        """Get permissions as dictionary."""
        if not self.permissions_json:
            return DEFAULT_PERMISSIONS.copy()
        try:
            perms = json.loads(self.permissions_json)
            # Merge with defaults for any missing keys
            result = DEFAULT_PERMISSIONS.copy()
            result.update(perms)
            return result
        except Exception:
            return DEFAULT_PERMISSIONS.copy()
    
    @permissions.setter
    def permissions(self, value: dict):
        """Set permissions from dictionary."""
        self.permissions_json = json.dumps(value)
    
    def has_permission(self, permission: str) -> bool:
        """Check if user has a specific permission."""
        # 1. Check Global Kill-Switches (Global settings)
        # If a feature is disabled globally, even admins can't use it in UI (optional logic)
        # But here we follow the request: "if global permission is off, it's off for everyone"
        from app.models.settings import Setting
        global_enabled = Setting.get(f'global_{permission}_enabled', 'true', user_id=None)
        if global_enabled == 'false':
            return False

        # 2. Admins have all personal permissions
        if self.is_admin:
            return True
        return self.permissions.get(permission, True)
    
    def set_permission(self, permission: str, value: bool):
        """Set a specific permission."""
        perms = self.permissions
        perms[permission] = value
        self.permissions = perms
    
    def get_restricted_pages(self) -> list:
        """Get list of pages user cannot access."""
        restricted = []
        for page, allowed in self.permissions.items():
            if not allowed:
                restricted.append(page)
        return restricted
    

    @property
    def is_super_admin(self) -> bool:
        """Check if user is the main super admin."""
        return self.email == "bugraerkaradeniz34@gmail.com"

    def __repr__(self):
        return f'<User {self.email}>'


from flask_login import AnonymousUserMixin

class AnonymousUser(AnonymousUserMixin):
    """Anonymous user class for unauthenticated requests."""
    
    @property
    def is_admin(self):
        return False
        
    def has_permission(self, permission: str) -> bool:
        """Anonymous users generally have no permissions."""
        return False



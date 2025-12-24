"""User service for authentication and user management."""
from datetime import datetime
from typing import Optional
from app import db
from app.models import User, Subscription


def create_admin_user_if_not_exists():
    """Create admin user on first run if not exists."""
    admin_email = "bugraerkaradeniz34@gmail.com"
    
    existing = User.query.filter_by(email=admin_email).first()
    if existing:
        return existing
    
    # Create admin user
    admin = User(
        email=admin_email,
        full_name="Buğra Erkaradeniz",
        is_admin=True,
        is_active=True
    )
    admin.set_password("admin1")
    db.session.add(admin)
    db.session.flush()  # Get the ID
    
    # Create unlimited enterprise subscription
    subscription = Subscription(
        user_id=admin.id,
        plan='enterprise',
        status='active',
        start_date=datetime.utcnow(),
        end_date=None,  # Unlimited
        max_products=-1,  # Unlimited
        max_xml_sources=-1  # Unlimited
    )
    db.session.add(subscription)
    
    # Migrate existing data to admin user
    migrate_existing_data_to_user(admin.id)
    
    db.session.commit()
    print(f"✅ Admin kullanıcısı oluşturuldu: {admin_email}")
    return admin


def migrate_existing_data_to_user(user_id: int):
    """Migrate existing data (with NULL user_id) to specified user."""
    from app.models import Setting, Product, SupplierXML, BatchLog, Order, AutoSync, ExcelFile
    
    # Update all records with NULL user_id
    Setting.query.filter_by(user_id=None).update({'user_id': user_id})
    Product.query.filter_by(user_id=None).update({'user_id': user_id})
    SupplierXML.query.filter_by(user_id=None).update({'user_id': user_id})
    BatchLog.query.filter_by(user_id=None).update({'user_id': user_id})
    Order.query.filter_by(user_id=None).update({'user_id': user_id})
    AutoSync.query.filter_by(user_id=None).update({'user_id': user_id})
    ExcelFile.query.filter_by(user_id=None).update({'user_id': user_id})
    
    print(f"📦 Mevcut veriler kullanıcı ID {user_id}'ye aktarıldı")


def create_user(email: str, password: str, **kwargs) -> Optional[User]:
    """Create a new user with free subscription and full profile."""
    # Check if email exists
    if User.query.filter_by(email=email.lower()).first():
        return None
    
    # Create user
    first_name = kwargs.get('first_name')
    last_name = kwargs.get('last_name')
    full_name = f"{first_name} {last_name}".strip() if first_name or last_name else None
    
    user = User(
        email=email.lower(),
        first_name=first_name,
        last_name=last_name,
        full_name=full_name,
        tc_no=kwargs.get('tc_no'),
        company_title=kwargs.get('company_title'),
        tax_office=kwargs.get('tax_office'),
        tax_no=kwargs.get('tax_no'),
        phone=kwargs.get('phone'),
        city=kwargs.get('city'),
        district=kwargs.get('district'),
        address=kwargs.get('address'),
        is_admin=kwargs.get('is_admin', False),
        is_active=True
    )
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    
    # Create default subscription
    plan = kwargs.get('plan', 'free')
    subscription = Subscription(
        user_id=user.id,
        plan=plan,
        status='active',
        start_date=datetime.utcnow(),
        end_date=None,
        max_products=100 if plan == 'free' else 1000 if plan == 'pro' else -1,
        max_xml_sources=3 if plan == 'free' else 10 if plan == 'pro' else -1
    )
    db.session.add(subscription)
    db.session.commit()
    
    return user


def authenticate_user(email: str, password: str) -> Optional[User]:
    """Authenticate user by email and password."""
    user = User.query.filter_by(email=email.lower()).first()
    
    if user and user.check_password(password):
        if user.is_banned:
            return None
        user.last_login = datetime.utcnow()
        db.session.commit()
        return user
    
    return None


def get_user_by_id(user_id: int) -> Optional[User]:
    """Get user by ID."""
    return User.query.get(user_id)


def get_all_users(page: int = 1, per_page: int = 20):
    """Get all users with pagination."""
    return User.query.order_by(User.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )


def ban_user(user_id: int, reason: str = None) -> bool:
    """Ban a user."""
    user = User.query.get(user_id)
    if not user or user.is_admin:
        return False
    
    user.is_banned = True
    user.ban_reason = reason
    db.session.commit()
    return True


def unban_user(user_id: int) -> bool:
    """Unban a user."""
    user = User.query.get(user_id)
    if not user:
        return False
    
    user.is_banned = False
    user.ban_reason = None
    db.session.commit()
    return True


def update_subscription(user_id: int, plan: str, end_date: datetime = None, 
                        max_products: int = None, max_xml_sources: int = None,
                        max_marketplaces: int = None) -> bool:
    """Update user subscription with optional overrides."""
    from app.models import Subscription
    from app.services.payment_service import get_plan_details
    
    subscription = Subscription.query.filter_by(user_id=user_id).first()
    if not subscription:
        return False
    
    subscription.plan = plan
    subscription.end_date = end_date
    subscription.status = 'active'
    
    # Get plan defaults
    plan_details = get_plan_details(plan)
    
    # Update limits: Use override if provided, otherwise use plan default, otherwise legacy fallback
    if max_products is not None:
        subscription.max_products = max_products
    elif plan_details:
        subscription.max_products = plan_details.get('max_products', 100)
    else:
        # Legacy fallbacks
        if plan == 'free': subscription.max_products = 100
        elif plan == 'pro': subscription.max_products = 1000
        elif plan == 'enterprise': subscription.max_products = -1
        
    if max_xml_sources is not None:
        subscription.max_xml_sources = max_xml_sources
    elif plan_details:
        subscription.max_xml_sources = plan_details.get('max_xml_sources', 3)
    else:
        # Legacy fallbacks
        if plan == 'free': subscription.max_xml_sources = 3
        elif plan == 'pro': subscription.max_xml_sources = 10
        elif plan == 'enterprise': subscription.max_xml_sources = -1

    if max_marketplaces is not None:
        subscription.max_marketplaces = max_marketplaces
    elif plan_details:
        subscription.max_marketplaces = plan_details.get('max_marketplaces', 1)
    else:
        # Legacy fallbacks
        if plan == 'free': subscription.max_marketplaces = 1
        elif plan == 'pro': subscription.max_marketplaces = 3
        elif plan == 'enterprise': subscription.max_marketplaces = 10
    
    db.session.commit()
    return True

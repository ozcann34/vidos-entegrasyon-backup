"""Subscription service for managing user subscriptions."""
from datetime import datetime, timedelta
from typing import Optional
from app import db
from app.models.subscription import Subscription


def get_subscription(user_id: int) -> Optional[Subscription]:
    """Get user's subscription."""
    return Subscription.query.filter_by(user_id=user_id).first()


def activate_subscription(user_id: int, plan: str, payment_id: int = None, billing_cycle: str = 'monthly', price_paid: float = 0.0) -> Optional[Subscription]:
    """Activate or upgrade user subscription."""
    from app.services.payment_service import get_plan_details
    
    plan_details = get_plan_details(plan)
    
    if not plan_details:
        return None
    
    # Get or create subscription
    subscription = get_subscription(user_id)
    
    if subscription:
        # Upgrade existing subscription
        subscription.plan = plan
        subscription.status = 'active'
        subscription.start_date = datetime.utcnow()
        subscription.end_date = datetime.utcnow() + timedelta(days=plan_details['duration_days'])
        subscription.max_products = plan_details['max_products']
        subscription.max_marketplaces = plan_details['max_marketplaces']
        subscription.max_xml_sources = plan_details.get('max_xml_sources', 1) # Default to 1
        subscription.billing_cycle = billing_cycle
        subscription.price_paid = price_paid
        if payment_id:
            subscription.payment_reference = str(payment_id)
    else:
        # Create new subscription
        subscription = Subscription(
            user_id=user_id,
            plan=plan,
            billing_cycle=billing_cycle,
            price_paid=price_paid,
            status='active',
            start_date=datetime.utcnow(),
            end_date=datetime.utcnow() + timedelta(days=plan_details['duration_days']),
            max_products=plan_details['max_products'],
            max_marketplaces=plan_details['max_marketplaces'],
            max_xml_sources=plan_details.get('max_xml_sources', 1),
            payment_reference=str(payment_id) if payment_id else None
        )
        db.session.add(subscription)
    
    subscription.updated_at = datetime.utcnow()
    db.session.commit()
    
    return subscription


def cancel_subscription(user_id: int) -> bool:
    """Cancel user subscription."""
    subscription = get_subscription(user_id)
    
    if not subscription:
        return False
    
    subscription.status = 'cancelled'
    subscription.updated_at = datetime.utcnow()
    db.session.commit()
    
    return True


def check_and_update_expired_subscriptions():
    """Check and update expired subscriptions (cron job)."""
    expired = Subscription.query.filter(
        Subscription.status == 'active',
        Subscription.end_date < datetime.utcnow()
    ).all()
    
    for subscription in expired:
        subscription.status = 'expired'
        subscription.updated_at = datetime.utcnow()
    
    db.session.commit()
    
    return len(expired)
def check_usage_limit(user_id: int, metric_type: str, current_count: int = None) -> bool:
    """Check if user has exceeded their plan limits."""
    subscription = get_subscription(user_id)
    if not subscription or not subscription.is_active:
        return False
        
    limit = -1
    usage = 0
    
    if metric_type == 'products':
        limit = subscription.max_products
        if limit is None: limit = 100
        
        if current_count is None:
            from app.models import Product
            usage = Product.query.filter_by(user_id=user_id).count()
        else:
            usage = current_count
            
    elif metric_type == 'xml_sources':
        limit = subscription.max_xml_sources
        if limit is None: limit = 1
        
        if current_count is None:
            from app.models import SupplierXML
            usage = SupplierXML.query.filter_by(user_id=user_id).count()
        else:
            usage = current_count
            
    elif metric_type == 'marketplaces':
        limit = subscription.max_marketplaces
        if limit is None: limit = 1
        
        if current_count is None:
            usage = len(get_active_marketplaces(user_id))
        else:
            usage = current_count
            
    if limit == -1:
        return True
        
    return usage < limit

def check_expiring_subscriptions():
    """
    Check for subscriptions expiring in the next 3 days and notify users.
    Should be run daily.
    """
    from app.models.notification import Notification
    
    three_days_later = datetime.utcnow() + timedelta(days=3)
    
    # Get active subscriptions expiring within 3 days
    expiring = Subscription.query.filter(
        Subscription.status == 'active',
        Subscription.end_date <= three_days_later,
        Subscription.end_date > datetime.utcnow()
    ).all()
    
    count = 0
    for sub in expiring:
        # Check if notification already sent today to avoid spam
        existing = Notification.query.filter_by(
            user_id=sub.user_id,
            title="Abonelik Hatırlatması"
        ).filter(Notification.created_at >= datetime.utcnow().date()).first()
        
        if not existing:
            days = sub.days_remaining
            notif = Notification(
                user_id=sub.user_id,
                title="Abonelik Hatırlatması",
                message=f"Aboneliğiniz {days} gün içinde sona erecektir. Kesintisiz hizmet için lütfen yenileyin.",
                type='warning'
            )
            db.session.add(notif)
            count += 1
            
    db.session.commit()
    return count

def get_active_marketplaces(user_id: int) -> list:
    """Check which marketplaces have configured API credentials."""
    from app.models import Setting
    active = []
    
    # Trendyol
    if Setting.get("SELLER_ID", user_id=user_id) or Setting.get("API_KEY", user_id=user_id):
        active.append('trendyol')
    
    # Hepsiburada
    if Setting.get("HB_MERCHANT_ID", user_id=user_id):
        active.append('hepsiburada')
        
    # N11
    if Setting.get("N11_API_KEY", user_id=user_id):
        active.append('n11')
        
    # Pazarama
    if Setting.get("PAZARAMA_API_KEY", user_id=user_id):
        active.append('pazarama')
        
    # Idefix
    if Setting.get("IDEFIX_API_KEY", user_id=user_id):
        active.append('idefix')
        
    return active
    

def get_usage_stats(user_id: int):
    """Get detailed usage statistics for dashboard."""
    subscription = get_subscription(user_id)
    if not subscription:
        return None
        
    from app.models import Product, SupplierXML
    
    product_usage = Product.query.filter_by(user_id=user_id).count()
    xml_usage = SupplierXML.query.filter_by(user_id=user_id).count()
    mp_usage = len(get_active_marketplaces(user_id))
    
    return {
        'products': {
            'used': product_usage,
            'limit': subscription.max_products,
            'percent': (product_usage / subscription.max_products * 100) if subscription.max_products and subscription.max_products > 0 else 0,
            'is_unlimited': subscription.max_products == -1
        },
        'xml_sources': {
            'used': xml_usage,
            'limit': subscription.max_xml_sources,
            'percent': (xml_usage / subscription.max_xml_sources * 100) if subscription.max_xml_sources and subscription.max_xml_sources > 0 else 0,
            'is_unlimited': subscription.max_xml_sources == -1
        },
        'marketplaces': {
            'used': mp_usage,
            'limit': subscription.max_marketplaces,
            'percent': (mp_usage / subscription.max_marketplaces * 100) if subscription.max_marketplaces and subscription.max_marketplaces > 0 else 0,
            'is_unlimited': subscription.max_marketplaces == -1
        }
    }


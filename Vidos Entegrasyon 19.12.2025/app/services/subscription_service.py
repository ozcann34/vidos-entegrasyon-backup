"""Subscription service for managing user subscriptions."""
from datetime import datetime, timedelta
from typing import Optional
from app import db
from app.models.subscription import Subscription


def get_subscription(user_id: int) -> Optional[Subscription]:
    """Get user's subscription."""
    return Subscription.query.filter_by(user_id=user_id).first()


def activate_subscription(user_id: int, plan: str, payment_id: int = None) -> Optional[Subscription]:
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
        subscription.max_xml_sources = plan_details['max_marketplaces']
        if payment_id:
            subscription.payment_reference = str(payment_id)
    else:
        # Create new subscription
        subscription = Subscription(
            user_id=user_id,
            plan=plan,
            status='active',
            start_date=datetime.utcnow(),
            end_date=datetime.utcnow() + timedelta(days=plan_details['duration_days']),
            max_products=plan_details['max_products'],
            max_xml_sources=plan_details['max_marketplaces'],
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
    """
    Check if user has exceeded their plan limits.
    
    Args:
        user_id: The user ID
        metric_type: 'products' or 'xml_sources'
        current_count: Optional current count to check against. If None, it will be calculated.
        
    Returns:
        True if within limit, False if exceeded.
    """
    subscription = get_subscription(user_id)
    if not subscription or not subscription.is_active:
        return False
        
    # Get effective limits (database stores effective limit including plan defaults, 
    # but let's be safe and fallback if they are 0/None which shouldn't happen with new logic)
    
    limit = -1
    usage = 0
    
    if metric_type == 'products':
        limit = subscription.max_products
        if limit is None: limit = 100 # Fallback
        
        # Calculate usage if not provided
        if current_count is None:
            # This requires querying Product table or similar
            # Ideally we pass current_count to avoid circular imports or heavy queries here if possible
            # But for convenience let's do import locally
            from app.models import Product
            usage = Product.query.filter_by(user_id=user_id).count()
        else:
            usage = current_count
            
    elif metric_type == 'xml_sources':
        limit = subscription.max_xml_sources
        if limit is None: limit = 3 # Fallback
        
        if current_count is None:
            from app.models import SupplierXML
            usage = SupplierXML.query.filter_by(user_id=user_id).count()
        else:
            usage = current_count
            
    # Check limit (-1 means unlimited)
    if limit == -1:
        return True
        
    return usage < limit
    

def get_usage_stats(user_id: int):
    """Get detailed usage statistics for dashboard."""
    subscription = get_subscription(user_id)
    if not subscription:
        return None
        
    from app.models import Product, SupplierXML
    
    product_usage = Product.query.filter_by(user_id=user_id).count()
    xml_usage = SupplierXML.query.filter_by(user_id=user_id).count()
    
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
        }
    }

"""Payment service for handling payment gateway operations."""
import hashlib
import secrets
from typing import Optional, Dict, Any
from datetime import datetime
from app import db
from app.models.payment import Payment
from app.models.subscription import Subscription


# Plan configuration
SUBSCRIPTION_PLANS = {
    'basic': {
        'name': 'Giriş Paketi',
        'price': 790,
        'currency': 'TRY',
        'features': ['1 Pazaryeri Entegrasyonu', '10.000 Ürün Limiti', '1 XML Tedarikçi', 'Günde 2 Kez Güncelleme', 'Temel Sipariş Listeleme', 'E-posta Destek'],
        'max_products': 10000,
        'max_marketplaces': 1,
        'max_xml_sources': 1,
        'duration_days': 30,
        'sync_interval_hours': 12
    },
    'pro': {
        'name': 'Ticaret Paketi',
        'price': 1490,
        'currency': 'TRY',
        'features': ['3 Pazaryeri Entegrasyonu', '25.000 Ürün Limiti', '5 XML Tedarikçi', '2 Saatte Bir Güncelleme', 'Gelişmiş Sipariş Yönetimi', 'Excel İşlemleri', 'Öncelikli Destek'],
        'max_products': 25000,
        'max_marketplaces': 3,
        'max_xml_sources': 5,
        'duration_days': 30,
        'sync_interval_hours': 2
    },
    'enterprise': {
        'name': 'Kurumsal Paket',
        'price': 2990,
        'currency': 'TRY',
        'features': ['10 Pazaryeri Entegrasyonu', '50.000 Ürün Limiti', 'Sınırsız XML Kaynağı', '30 Dk. Hızlı Senkronizasyon', 'Özel Fiyat Motoru', '7/24 WhatsApp Desteği', 'Kişisel Hesap Yöneticisi'],
        'max_products': 50000,
        'max_marketplaces': 10,
        'max_xml_sources': -1,
        'duration_days': 30,
        'sync_interval_mins': 30
    }
}


def get_plan_details(plan_name: str) -> Optional[Dict[str, Any]]:
    """Get subscription plan details."""
    return SUBSCRIPTION_PLANS.get(plan_name)


def get_all_plans() -> Dict[str, Dict[str, Any]]:
    """Get all subscription plans."""
    return SUBSCRIPTION_PLANS


def generate_payment_reference() -> str:
    """Generate unique payment reference."""
    timestamp = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    random_part = secrets.token_hex(8)
    return f"PAY-{timestamp}-{random_part}"


def create_payment(user_id: int, plan: str, billing_cycle: str = 'monthly', ip_address: str = None, user_agent: str = None) -> Optional[Payment]:
    """Create a new payment record."""
    plan_details = get_plan_details(plan)
    
    if not plan_details:
        return None
    
    # Calculate amount based on billing cycle
    amount = plan_details['price']
    if billing_cycle == 'yearly':
        amount = int(plan_details['price'] * 12 * 0.8)  # 20% discount
    
    payment = Payment(
        user_id=user_id,
        amount=amount,
        currency=plan_details['currency'],
        plan=plan,
        billing_cycle=billing_cycle,
        payment_reference=generate_payment_reference(),
        status='pending',
        ip_address=ip_address,
        user_agent=user_agent
    )
    
    db.session.add(payment)
    db.session.commit()
    
    return payment


def complete_payment(payment_id: int, transaction_id: str = None, gateway: str = None) -> bool:
    """Mark payment as completed and activate subscription."""
    payment = Payment.query.get(payment_id)
    
    if not payment:
        return False
    
    if payment.status == 'completed':
        return True  # Already completed
    
    # Mark payment as completed
    payment.mark_completed(transaction_id)
    if gateway:
        payment.gateway = gateway
    
    # Create or update subscription
    from app.services.subscription_service import activate_subscription
    # Pass billing_cycle and amount to activate_subscription
    subscription = activate_subscription(
        payment.user_id, 
        payment.plan, 
        payment.id, 
        billing_cycle=payment.billing_cycle, 
        price_paid=payment.amount
    )
    
    if subscription:
        payment.subscription_id = subscription.id
    
    db.session.commit()
    
    return True


def cancel_payment(payment_id: int) -> bool:
    """Cancel a pending payment."""
    payment = Payment.query.get(payment_id)
    
    if not payment or payment.status != 'pending':
        return False
    
    payment.status = 'cancelled'
    db.session.commit()
    
    return True


def get_user_payments(user_id: int, limit: int = 10):
    """Get user's payment history."""
    return Payment.query.filter_by(user_id=user_id).order_by(Payment.created_at.desc()).limit(limit).all()


# ===== PAYMENT GATEWAY ADAPTERS =====
# These are placeholder functions for future gateway integration

class PaymentGateway:
    """Base payment gateway interface."""
    
    def initiate_payment(self, payment: Payment) -> Dict[str, Any]:
        """Initiate payment and get redirect URL."""
        raise NotImplementedError
    
    def verify_callback(self, callback_data: Dict[str, Any]) -> bool:
        """Verify payment callback data."""
        raise NotImplementedError


class ShopierAdapter(PaymentGateway):
    """Shopier payment gateway adapter - PLACEHOLDER."""
    
    def __init__(self, api_key: str = None, api_secret: str = None):
        self.api_key = api_key or "SHOPIER_API_KEY_PLACEHOLDER"
        self.api_secret = api_secret or "SHOPIER_SECRET_PLACEHOLDER"
    
    def initiate_payment(self, payment: Payment) -> Dict[str, Any]:
        """
        Initiate Shopier payment.
        TODO: Implement actual Shopier API integration
        """
        return {
            'success': False,
            'message': 'Shopier entegrasyonu henüz aktif değil',
            'redirect_url': None
        }
    
    def verify_callback(self, callback_data: Dict[str, Any]) -> bool:
        """
        Verify Shopier callback.
        TODO: Implement actual callback verification
        """
        return False


class IyzicoAdapter(PaymentGateway):
    """Iyzico payment gateway adapter - PLACEHOLDER."""
    
    def __init__(self, api_key: str = None, secret_key: str = None):
        self.api_key = api_key or "IYZICO_API_KEY_PLACEHOLDER"
        self.secret_key = secret_key or "IYZICO_SECRET_PLACEHOLDER"
    
    def initiate_payment(self, payment: Payment) -> Dict[str, Any]:
        """
        Initiate Iyzico payment.
        TODO: Implement actual Iyzico API integration
        """
        return {
            'success': False,
            'message': 'İyzico entegrasyonu henüz aktif değil',
            'redirect_url': None
        }
    
    def verify_callback(self, callback_data: Dict[str, Any]) -> bool:
        """
        Verify Iyzico callback.
        TODO: Implement actual callback verification
        """
        return False


class MockAdapter(PaymentGateway):
    """Local development mock gateway."""
    
    def initiate_payment(self, payment: Payment) -> Dict[str, Any]:
        """Instant success for testing."""
        # Auto complete
        complete_payment(payment.id, transaction_id=f"MOCK-TRX-{payment.id}", gateway="mock")
        
        return {
            'success': True,
            'message': 'Mock ödeme başarılı. Yönlendiriliyor...',
            'redirect_url': f'/payment/success?payment_id={payment.id}' # Mock success URL
        }


def get_payment_gateway(gateway_name: str = 'shopier') -> PaymentGateway:
    """Get payment gateway adapter."""
    # Force Mock if in local dev or explicitly requested (Environment variable check is better but for now hardcode logic)
    import os
    if os.environ.get('FLASK_ENV') == 'development' or gateway_name == 'mock':
        return MockAdapter()
        
    if gateway_name == 'shopier':
        return ShopierAdapter()
    elif gateway_name == 'iyzico':
        return IyzicoAdapter()
    else:
        raise ValueError(f"Unknown payment gateway: {gateway_name}")

def process_mock_payment(payment_id: int) -> bool:
    """Manually process a mock payment (useful for admin testing)."""
    return complete_payment(payment_id, f"MANUAL-MOCK-{payment_id}", "manual_mock")

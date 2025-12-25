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
        'price': 1290, # Aylık liste fiyatı
        'yearly_price_monthly': 790, # Yıllık alımda aylık bedel
        'currency': 'TRY',
        'features': ['3 Pazaryeri Entegrasyonu', '5.000 Ürün Limiti', '3 XML Tedarikçi', 'Günde 2 Kez Otomatik Güncelleme', 'Temel Sipariş Listeleme', '7/24 Destek'],
        'max_products': 5000,
        'max_marketplaces': 3,
        'max_xml_sources': 3,
        'duration_days': 30,
        'sync_interval_hours': 12
    },
    'pro': {
        'name': 'Ticaret Paketi',
        'price': 2390, # Aylık liste fiyatı
        'yearly_price_monthly': 1490, # Yıllık alımda aylık bedel
        'currency': 'TRY',
        'features': ['5 Pazaryeri Entegrasyonu', '15.000 Ürün Limiti', '6 XML Tedarikçi', '2 Saatte Bir Otomatik Güncelleme', 'Gelişmiş Sipariş Yönetimi', '7/24 Destek', 'Excel İle Yönetim', 'Sosyal Medya AI Taslakları'],
        'max_products': 15000,
        'max_marketplaces': 5,
        'max_xml_sources': 6,
        'duration_days': 30,
        'sync_interval_hours': 2
    },
    'enterprise': {
        'name': 'Kurumsal Paket',
        'price': 4290, # Aylık liste fiyatı
        'yearly_price_monthly': 2990, # Yıllık alımda aylık bedel
        'currency': 'TRY',
        'features': ['10 Pazaryeri Entegrasyonu', '30.000 Ürün Limiti', 'Sınırsız XML Tedarikçi', '1 Saatte Bir Sınırsız Güncelleme', 'Gelişmiş Sipariş Yönetimi', '7/24 Destek', 'Excel İle Yönetim', 'Zamanlamalı Sosyal Medya Paylaşımı', 'Kişisel E-ticaret Entegrasyonu'],
        'max_products': 30000,
        'max_marketplaces': 10,
        'max_xml_sources': -1,
        'duration_days': 30, # Default to 30 days
        'sync_interval_mins': 60
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
    if billing_cycle == 'yearly':
        amount = plan_details.get('yearly_price_monthly', plan_details['price']) * 12
    else:
        amount = plan_details['price']
    
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
    
    def initiate_payment(self, payment: Payment, callback_url: str = None) -> Dict[str, Any]:
        """Initiate payment and get redirect URL."""
        raise NotImplementedError
    
    def verify_callback(self, callback_data: Dict[str, Any]) -> bool:
        """Verify payment callback data."""
        raise NotImplementedError


class ShopierAdapter(PaymentGateway):
    """
    Shopier payment gateway adapter with HMAC SHA256 verification.
    """
    
    def __init__(self, api_key: str = None, api_secret: str = None):
        if not api_key:
             from app.models import Setting
             api_key = Setting.get('SHOPIER_API_KEY')
        if not api_secret:
             from app.models import Setting
             api_secret = Setting.get('SHOPIER_API_SECRET')
             
        self.api_key = api_key
        self.api_secret = api_secret
        from app.models import Setting
        self.is_test = (Setting.get('SHOPIER_TEST_MODE') == 'on')
    
    def initiate_payment(self, payment: Payment, callback_url: str = None) -> Dict[str, Any]:
        """
        Initiate Shopier payment and return redirect info.
        Generates sign and returns parameters for a dynamic form.
        """
        if not self.api_key or not self.api_secret:
            return {
                'success': False,
                'message': 'Shopier API anahtarları yapılandırılmamış.',
                'redirect_url': None
            }

        from app.models.user import User
        user = User.query.get(payment.user_id)
        
        # Shopier API parameters
        import json
        import base64
        import hmac
        import hashlib

        # Basic user info
        first_name = user.first_name or user.full_name.split()[0] if user.full_name else "Müşteri"
        last_name = user.last_name or (user.full_name.split()[-1] if user.full_name and len(user.full_name.split()) > 1 else "Soyadı")
        
        res_data = {
            'API_KEY': self.api_key,
            'user_name': first_name,
            'user_surname': last_name,
            'user_email': user.email,
            'user_phone': user.phone or "05555555555",
            'user_address': user.address or "Türkiye",
            'product_name': f"Vidos - {payment.plan.title()} Paket",
            'product_price': payment.amount,
            'currency': 'TRY',
            'platform_order_id': payment.payment_reference,
            'callback_url': callback_url or "https://vidosentegrasyon.com.tr/payment/callback",
            'modul_version': '1.0.0',
            'type': 'vidos'
        }

        # Signature generation
        # data = platform_order_id + product_price + currency
        data_to_sign = f"{res_data['platform_order_id']}{res_data['product_price']}{res_data['currency']}"
        signature = hmac.new(self.api_secret.encode(), data_to_sign.encode(), hashlib.sha256).digest()
        signature = base64.b64encode(signature).decode()
        
        res_data['signature'] = signature

        return {
            'success': True,
            'message': 'Shopier ödemesi hazırlanıyor...',
            'params': res_data,
            'post_url': "https://www.shopier.com/ShowProduct/api/pay4post" 
        }
    
    def verify_callback(self, callback_data: Dict[str, Any]) -> bool:
        """
        Verify Shopier callback using HMAC SHA256.
        Shopier sends: platform_order_id, status, installment, signature
        """
        import hmac
        import base64
        import hashlib
        
        try:
            signature = callback_data.get('signature')
            platform_order_id = callback_data.get('platform_order_id')
            random_nr = callback_data.get('random_nr')
            
            if not signature or not platform_order_id or not random_nr:
                return False
                
            # Verify signature: HMAC-SHA256(API_SECRET, random_nr + platform_order_id)
            data_to_sign = f"{random_nr}{platform_order_id}"
            expected_sig_raw = hmac.new(
                self.api_secret.encode(),
                data_to_sign.encode(),
                hashlib.sha256
            ).digest()
            expected_signature = base64.b64encode(expected_sig_raw).decode()
            
            return hmac.compare_digest(signature, expected_signature)
        except Exception as e:
            print(f"Shopier Verification Error: {e}")
            return False



class IyzicoAdapter(PaymentGateway):
    """Iyzico payment gateway adapter - PLACEHOLDER."""
    
    def __init__(self, api_key: str = None, secret_key: str = None):
        self.api_key = api_key or "IYZICO_API_KEY_PLACEHOLDER"
        self.secret_key = secret_key or "IYZICO_SECRET_PLACEHOLDER"
    
    def initiate_payment(self, payment: Payment, callback_url: str = None) -> Dict[str, Any]:
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
    
    def initiate_payment(self, payment: Payment, callback_url: str = None) -> Dict[str, Any]:
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

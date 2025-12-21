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
        'price_monthly': 790,
        'price_annual': 7590,  # ~632/ay (%20 indirimli)
        'currency': 'TRY',
        'features': ['3 Pazaryeri Entegrasyonu', '5.000 Ürün Limiti', '3 XML Tedarikçi', 'Günde 2 Kez Güncelleme', 'Temel Sipariş Listeleme', '7/24 Destek'],
        'max_products': 5000,
        'max_marketplaces': 3,
        'sync_interval_hours': 12
    },
    'pro': {
        'name': 'Ticaret Paketi',
        'price_monthly': 1490,
        'price_annual': 14300, # ~1190/ay (%20 indirimli)
        'currency': 'TRY',
        'features': ['5 Pazaryeri Entegrasyonu', '15.000 Ürün Limiti', '6 XML Tedarikçi', '2 Saatte Bir Güncelleme', 'Gelişmiş Sipariş Yönetimi', 'Excel İle Yönetim', 'Sosyal Medya AI Desteği', '7/24 Destek'],
        'max_products': 15000,
        'max_marketplaces': 5,
        'sync_interval_hours': 2
    },
    'enterprise': {
        'name': 'Kurumsal Paket',
        'price_monthly': 2990,
        'price_annual': 28700, # ~2390/ay (%20 indirimli)
        'currency': 'TRY',
        'features': ['10 Pazaryeri Entegrasyonu', '30.000 Ürün Limiti', 'Sınırsız XML Tedarikçi', '1 Saatte Bir Güncelleme', 'Gelişmiş Sipariş Yönetimi', 'Excel İle Yönetim', 'AI Destekli Zamanlı Paylaşım', '7/24 Destek'],
        'max_products': 30000,
        'max_marketplaces': 10,
        'sync_interval_hours': 1
    }
}


def get_plan_details(plan_name: str, cycle: str = 'monthly') -> Optional[Dict[str, Any]]:
    """Get subscription plan details with price based on cycle."""
    plan = SUBSCRIPTION_PLANS.get(plan_name)
    if not plan:
        return None
    
    details = plan.copy()
    details['price'] = plan['price_annual'] if cycle == 'annual' else plan['price_monthly']
    details['duration_days'] = 365 if cycle == 'annual' else 30
    details['cycle'] = cycle
    return details


def get_all_plans() -> Dict[str, Dict[str, Any]]:
    """Get all subscription plans."""
    return SUBSCRIPTION_PLANS


def generate_payment_reference() -> str:
    """Generate unique payment reference."""
    timestamp = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    random_part = secrets.token_hex(8)
    return f"PAY-{timestamp}-{random_part}"


def create_payment(user_id: int, plan: str, cycle: str = 'monthly', ip_address: str = None, user_agent: str = None, metadata: Dict[str, Any] = None) -> Optional[Payment]:
    """Create a new payment record."""
    plan_details = get_plan_details(plan, cycle)
    
    if not plan_details:
        return None
    
    import json
    payment_metadata = metadata or {}
    payment_metadata['cycle'] = cycle
    
    payment = Payment(
        user_id=user_id,
        amount=plan_details['price'],
        currency=plan_details['currency'],
        plan=plan,
        payment_reference=generate_payment_reference(),
        status='pending',
        ip_address=ip_address,
        user_agent=user_agent,
        payment_metadata=json.dumps(payment_metadata)
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
    
    import json
    marketplaces = None
    cycle = 'monthly'
    if payment.payment_metadata:
        try:
            meta = json.loads(payment.payment_metadata)
            marketplaces = meta.get('marketplaces')
            cycle = meta.get('cycle', 'monthly')
        except:
            pass
            
    subscription = activate_subscription(payment.user_id, payment.plan, payment.id, cycle=cycle, marketplaces=marketplaces)
    
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
    """Actual Iyzico payment gateway integration."""
    
    def __init__(self, api_key: str = None, secret_key: str = None, base_url: str = None):
        import os
        from flask import current_app
        self.api_key = api_key or current_app.config.get('IYZICO_API_KEY')
        self.secret_key = secret_key or current_app.config.get('IYZICO_SECRET_KEY')
        self.base_url = base_url or current_app.config.get('IYZICO_BASE_URL', 'https://sandbox-api.iyzipay.com')
    
    def initiate_payment(self, payment: Payment) -> Dict[str, Any]:
        """Initiate Iyzico Checkout Form."""
        try:
            import iyzipay
            from flask import url_for, request
            import logging

            options = {
                'api_key': self.api_key,
                'secret_key': self.secret_key,
                'base_url': self.base_url
            }

            from app.models.user import User
            user = User.query.get(payment.user_id)
            
            # Prepare request
            request_data = {
                'locale': 'tr',
                'conversationId': payment.payment_reference,
                'price': str(payment.amount),
                'paidPrice': str(payment.amount),
                'currency': iyzipay.Currency.TRY.value,
                'basketId': f"B-{payment.id}",
                'paymentGroup': iyzipay.PaymentGroup.PRODUCT.value,
                'callbackUrl': url_for('payment.callback', _external=True),
                'enabledInstallments': ['1'] # Disable installments as requested (standard integration)
            }

            # Buyer info
            buyer = {
                'id': str(user.id),
                'name': user.first_name or 'Customer',
                'surname': user.last_name or 'N/A',
                'gsmNumber': user.phone or '+900000000000',
                'email': user.email,
                'identityNumber': '11111111111', # Placeholder
                'lastLoginDate': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'registrationDate': user.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                'registrationAddress': user.address or 'N/A',
                'ip': request.remote_addr,
                'city': user.city or 'Istanbul',
                'country': 'Turkey',
                'zipCode': user.zip_code or '34000'
            }
            request_data['buyer'] = buyer

            # Address info
            address = {
                'contactName': f"{user.first_name} {user.last_name}",
                'city': user.city or 'Istanbul',
                'country': 'Turkey',
                'address': user.address or 'N/A',
                'zipCode': user.zip_code or '34000'
            }
            request_data['shippingAddress'] = address
            request_data['billingAddress'] = address

            # Basket items
            basket_items = [
                {
                    'id': f"P-{payment.plan}",
                    'name': f"Vidos Subscription - {payment.plan.capitalize()}",
                    'category1': 'Software',
                    'itemType': iyzipay.BasketItemType.VIRTUAL.value,
                    'price': str(payment.amount)
                }
            ]
            request_data['basketItems'] = basket_items

            checkout_form_initialize = iyzipay.CheckoutFormInitialize().create(request_data, options)
            result = checkout_form_initialize.read().decode('utf-8')
            import json
            result_json = json.loads(result)

            if result_json.get('status') == 'success':
                return {
                    'success': True,
                    'checkout_content': result_json.get('checkoutFormContent'),
                    'token': result_json.get('token'),
                    'payment_page_url': result_json.get('paymentPageUrl')
                }
            else:
                logging.error(f"Iyzico Error: {result_json.get('errorMessage')}")
                return {
                    'success': False,
                    'message': f"Ödeme başlatılamadı: {result_json.get('errorMessage')}"
                }

        except Exception as e:
            import logging
            logging.exception("Failed to initiate Iyzico payment")
            return {'success': False, 'message': f"Teknik bir hata oluştu: {str(e)}"}

    def verify_callback(self, token: str) -> Dict[str, Any]:
        """Retrieve Iyzico payment result via token."""
        try:
            import iyzipay
            import json
            
            options = {
                'api_key': self.api_key,
                'secret_key': self.secret_key,
                'base_url': self.base_url
            }

            request_data = {
                'locale': 'tr',
                'conversationId': f"CB-{token}",
                'token': token
            }

            checkout_form = iyzipay.CheckoutForm().retrieve(request_data, options)
            result = checkout_form.read().decode('utf-8')
            result_json = json.loads(result)

            if result_json.get('status') == 'success' and result_json.get('paymentStatus') == 'SUCCESS':
                return {
                    'success': True,
                    'payment_id': result_json.get('paymentId'),
                    'basket_id': result_json.get('basketId'),
                    'amount': result_json.get('paidPrice'),
                    'reference': result_json.get('conversationId')
                }
            
            return {'success': False, 'message': result_json.get('errorMessage', 'Ödeme doğrulanamadı')}

        except Exception as e:
            return {'success': False, 'message': str(e)}


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


def get_payment_gateway(gateway_name: str = 'iyzico') -> PaymentGateway:
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

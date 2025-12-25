import hmac
import hashlib
import base64
import random
from app.models.payment import Payment
from app.models.settings import Setting  # Admin panelinden ayarları çekmek için
from flask import url_for

# Sabit Plan Tanımları (Veritabanında tutulmuyor, kodda sabit)
SUBSCRIPTION_PLANS = {
    'basic': {
        'name': 'Başlangıç',
        'price': 499.00,
        'currency': 'TRY',
        'features': ['Ayda 1000 Ürün', 'Pazarama & İdefix', 'Temel İstatistikler']
    },
    'pro': {
        'name': 'Profesyonel',
        'price': 999.00,
        'currency': 'TRY',
        'features': ['Ayda 5000 Ürün', 'Tüm Pazaryerleri', 'Gelişmiş Raporlar', '7/24 Destek']
    },
    'enterprise': {
        'name': 'Kurumsal',
        'price': 1999.00,
        'currency': 'TRY',
        'features': ['Sınırsız Ürün', 'Özel Entegrasyon', 'Dedicated Sunucu', 'Özel Hesap Yöneticisi']
    }
}

def get_plan_details(plan_name):
    return SUBSCRIPTION_PLANS.get(plan_name)

def generate_transaction_id():
    """Benzersiz bir işlem ID'si oluşturur."""
    return str(random.randint(100000000, 999999999))

class ShopierAdapter:
    def __init__(self):
        # API bilgilerini veritabanından (Admin Ayarları) çekiyoruz
        self.api_key = Setting.get_value('SHOPIER_API_KEY')
        self.api_secret = Setting.get_value('SHOPIER_API_SECRET')
        self.base_url = "https://www.shopier.com/ShowProduct/api/pay4post"

    def initiate_payment(self, payment: Payment, callback_url: str = None) -> dict:
        """
        Shopier ödeme formunu hazırlar.
        Backend-First yaklaşımı: Parametreleri ve imzayı burada oluşturur.
        """
        if not self.api_key or not self.api_secret:
            return {
                'success': False,
                'message': 'Shopier API anahtarları eksik. Lütfen yönetici ile iletişime geçin.'
            }

        user = payment.user
        plan_name = SUBSCRIPTION_PLANS.get(payment.plan, {}).get('name', 'Abonelik')
        
        # Fiyat kontrolü (Güvenlik için her zaman payment kaydındaki fiyatı kullan)
        price = float(payment.amount)
        
        # Shopier'in istediği zorunlu parametreler
        args = {
            'API_key': self.api_key,
            'website_index': 1,
            'platform_order_id': payment.payment_reference,
            'product_name': f"Vidos - {plan_name} Paketi",
            'product_type': 1, # 1: Fiziksel/Dijital Ürün, 0: Hizmet
            'buyer_name': user.first_name if user.first_name else 'Misafir',
            'buyer_surname': user.last_name if user.last_name else 'Kullanici',
            'buyer_email': user.email,
            'buyer_account_age': 0,
            'buyer_id_nr': 0,
            'buyer_phone': user.phone if user.phone else '05555555555', # Zorunlu alan
            'billing_address': "Turkiye", # Zorunlu
            'city': "Istanbul", # Zorunlu
            'country': "Turkiye", # Zorunlu
            'zip_code': "34000", # Zorunlu
            'shipping_address': "Turkiye",
            'shipping_city': "Istanbul",
            'shipping_country': "Turkiye",
            'shipping_zip_code': "34000",
            'currency': 0, # 0: TL, 1: USD, 2: EUR
            'modul_version': '1.0.4',
            'random_nr': generate_transaction_id()
        }

        # İmza oluşturma (Shopier Dokümantasyonuna göre sıralı birleştirme)
        data_to_sign = [
            args['API_key'],
            args['website_index'],
            args['platform_order_id'],
            args['product_name'],
            args['product_type'],
            args['buyer_name'],
            args['buyer_surname'],
            args['buyer_email'],
            args['buyer_account_age'],
            args['buyer_id_nr'],
            args['buyer_phone'],
            args['billing_address'],
            args['city'],
            args['country'],
            args['zip_code'],
            args['shipping_address'],
            args['shipping_city'],
            args['shipping_country'],
            args['shipping_zip_code'],
            args['currency'],
            args['modul_version'],
            args['random_nr']
        ]
        
        # Veriyi birleştir ve imzala
        # Not: Shopier bazı durumlarda price'ı imza dizisine dahil edip etmeme konusunda farklılık gösterebilir.
        # Standart entegrasyonda price imzaya DAHİL EDİLMEZ, posta ayrıca eklenir.
        
        signature_data = "".join([str(x) for x in data_to_sign])
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            signature_data.encode('utf-8'),
            hashlib.sha256
        ).digest()
        
        args['signature'] = base64.b64encode(signature).decode()
        
        # Fiyat parametresini sonradan ekliyoruz (İmzanın bozulmaması için sırası önemlidir ama POST içinde sıra fark etmez)
        args['price'] = str(price)

        # Başarılı dönüş: Post URL ve Parametreleri ver
        return {
            'success': True,
            'post_url': self.base_url,
            'params': args
        }

    def verify_callback(self, data: dict) -> bool:
        """
        Shopier'den gelen callback verisinin imzasını doğrular.
        """
        if 'signature' not in data:
            return False

        incoming_signature = data['signature']
        random_nr = data.get('random_nr')
        platform_order_id = data.get('platform_order_id')
        status = data.get('status')
        
        if not random_nr or not platform_order_id or not status:
            return False

        expected_data = [
            random_nr,
            platform_order_id,
            status
        ]
        
        signature_str = "".join([str(x) for x in expected_data])
        calculated_signature = hmac.new(
            self.api_secret.encode('utf-8'),
            signature_str.encode('utf-8'),
            hashlib.sha256
        ).digest()
        
        calculated_signature_b64 = base64.b64encode(calculated_signature).decode()
        
        return incoming_signature == calculated_signature_b64

# Basit Facade Functions
def get_payment_gateway(gateway_name='shopier'):
    return ShopierAdapter()

def create_payment(user_id, plan, billing_cycle, ip_address, user_agent):
    plan_info = get_plan_details(plan)
    if not plan_info:
        return None
        
    amount = plan_info['price']
    if billing_cycle == 'yearly':
        amount = amount * 12 * 0.8  # %20 indirim
        
    payment = Payment(
        user_id=user_id,
        plan=plan,
        billing_cycle=billing_cycle,
        amount=amount,
        currency='TRY',
        status='pending',
        payment_reference=generate_transaction_id(),
        ip_address=ip_address,
        user_agent=user_agent
    )
    
    from app import db
    db.session.add(payment)
    db.session.commit()
    return payment

def complete_payment(payment_id, transaction_id, gateway):
    from app import db
    payment = Payment.query.get(payment_id)
    if payment:
        payment.status = 'completed'
        payment.transaction_id = transaction_id
        payment.provider = gateway
        db.session.commit()
        return True
    return False

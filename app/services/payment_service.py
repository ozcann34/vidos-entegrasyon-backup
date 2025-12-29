import hmac
import hashlib
import base64
import random
from datetime import datetime
from app.models.payment import Payment
from app.models.settings import Setting  # Updated for correctness (settings.py)
from flask import url_for

# Sabit Plan Tanımları (Veritabanında tutulmuyor, kodda sabit)
SUBSCRIPTION_PLANS = {
    'basic': {
        'name': 'Başlangıç',
        'price': 1.00,
        'currency': 'TRY',
        'features': ['Ayda 1000 Ürün', 'Pazarama & İdefix', 'Temel İstatistikler']
    },
    'pro': {
        'name': 'Profesyonel',
        'price': 1.00,
        'currency': 'TRY',
        'features': ['Ayda 5000 Ürün', 'Tüm Pazaryerleri', 'Gelişmiş Raporlar', '7/24 Destek']
    },
    'enterprise': {
        'name': 'Kurumsal',
        'price': 1.00,
        'currency': 'TRY',
        'features': ['Sınırsız Ürün', 'Özel Entegrasyon', 'Dedicated Sunucu', 'Özel Hesap Yöneticisi']
    },
    'bug-z-bayilik': {
        'name': 'BUG-Z Bayilik Paketi',
        'price': 0.00,  # Ücretsiz (başvuru sonrası aktif)
        'currency': 'TRY',
        'features': [
            'bug-z.com XML Bayiliği',
            'Tüm Pazaryerleri (XML/Excel)',
            'Sipariş Takibi',
            'Temel Raporlar'
        ],
        'is_application_based': True,  # Başvuru gerektiriyor
        'application_url': 'https://bug-z.com/bayilik-basvuru',
        'max_xml_sources': 1,  # Sadece Admin ekleyebilir
        'max_products': 10000,
        'max_marketplaces': 5,
        'restricted_features': ['instagram_panel', 'add_xml_source', 'add_excel_source', 'vidos_assistant', 'batch_logs', 'auto_sync']
    }
}

def get_plan_details(plan_name):
    return SUBSCRIPTION_PLANS.get(plan_name)

def generate_transaction_id():
    """Benzersiz bir işlem ID'si oluşturur."""
    return str(random.randint(100000000, 999999999))

def clean_text_strict(text):
    import re
    if not text: return ""
    # Turkce karakter donusumu
    text = str(text).replace('ı', 'i').replace('ğ', 'g').replace('ü', 'u').replace('ş', 's').replace('ö', 'o').replace('ç', 'c').replace('İ', 'I').replace('Ğ', 'G').replace('Ü', 'U').replace('Ş', 'S').replace('Ö', 'O').replace('Ç', 'C')
    # Sadece Alfanumerik ve Bosluk birak
    text = re.sub(r'[^a-zA-Z0-9 ]', '', text)
    return text.strip()

class ShopierAdapter:
    def __init__(self):
        # API bilgilerini veritabanından (Admin Ayarları) çekiyoruz
        # KULLANICI ISTEGI UZERINE HARDCODED OLARAK EKLENDI
        self.api_key = "93d990d318d7429b720d52d394681ac3"
        self.api_secret = "3980f311a4dd2438ecccaf4237a9ae73"
        self.base_url = "https://www.shopier.com/ShowProduct/api_pay4.php"

    def initiate_payment(self, payment: Payment, callback_url: str = None) -> dict:
        """
        Shopier ödeme formunu hazırlar (api_pay4.php - SDK STANDART).
        HATA 500 sorununu çözmek için HMAC-SHA256 imza algoritması uygulanmıştır.
        """
        if not self.api_key or not self.api_secret:
            return {
                'success': False,
                'message': 'Shopier API anahtarları eksik.'
            }

        user = payment.user
        website_index = str(Setting.get_value('SHOPIER_WEBSITE_INDEX', '1')).strip()
        
        # Ürün Adı (Maks 100 karakter önerilir)
        product_name = f"Vidos Abonelik - {payment.plan}"
        if payment.billing_cycle == 'yearly':
            product_name += " (Yıllık)"
            
        # Fiyat Formatı: X.XX (SDK total_order_value bekler)
        price_str = "{:.2f}".format(float(payment.amount))

        # Telefon Temizleme: Tam 10 hane (5XXXXXXXXX)
        phone = ''.join(filter(str.isdigit, user.phone or ""))
        if phone.startswith('0'): phone = phone[1:]
        if phone.startswith('90'): phone = phone[2:]
        phone = phone[:10]

        # SDK Standart: random_nr (100000-999999)
        import random
        random_nr_val = random.randint(100000, 999999)

        # Shopier SDK (api_pay4.php) için GEREKLİ parametreler
        args = {
            'API_key': self.api_key,
            'website_index': website_index,
            'platform_order_id': str(payment.payment_reference),
            'product_name': product_name.strip(),
            'product_type': 1, # 1: DOWNLOADABLE_VIRTUAL
            'buyer_name': (user.first_name or "Müşteri").strip(),
            'buyer_surname': (user.last_name or "Soyadı").strip(),
            'buyer_email': user.email.strip(),
            'buyer_account_age': 0,
            'buyer_id_nr': user.id,
            'buyer_phone': phone,
            'billing_address': "Turkiye", 
            'billing_city': "Istanbul", 
            'billing_country': "Turkey",
            'billing_postcode': "34000", 
            'shipping_address': "Turkiye",
            'shipping_city': "Istanbul",
            'shipping_country': "Turkey",
            'shipping_postcode': "34000",
            'total_order_value': price_str,
            'currency': 0, # 0: TL
            'platform': 0, 
            'is_in_frame': 0,
            'current_language': 0, # 0: TR
            'modul_version': '1.0.4',
            'random_nr': random_nr_val
        }

        # İMZA ALGORİTMASI (SDK'dan teyit edildi):
        # random_nr . platform_order_id . total_order_value . currency
        hash_str = f"{args['random_nr']}{args['platform_order_id']}{args['total_order_value']}{args['currency']}"
        
        import hmac
        import hashlib
        import base64
        from datetime import datetime
        
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            hash_str.encode('utf-8'),
            hashlib.sha256
        ).digest()
        
        args['signature'] = base64.b64encode(signature).decode('utf-8')

        # Loglama
        try:
            with open('shopier_debug.log', 'a', encoding='utf-8') as f:
                f.write(f"\n--- Shopier V1 (api_pay4.php) SDK Attempt [{datetime.now()}] ---\n")
                f.write(f"Ref: {args['platform_order_id']}, Hash Str: {hash_str}\n")
                f.write(f"Signature: {args['signature']}\n")
        except: pass

        return {
            'success': True,
            'post_url': self.base_url,
            'params': args
        }

    def initiate_payment_v2(self, payment: Payment, callback_url: str = None) -> dict:
        """
        Shopier Ödeme Formu (Base64 JSON - 'Golden Form' Yaklaşımı)
        Bazı Shopier hesapları bu yeni formatı bekler.
        """
        import json
        import base64
        
        user = payment.user
        plan_name = SUBSCRIPTION_PLANS.get(payment.plan, {}).get('name', 'Abonelik')
        
        # Fiyat ve Callback
        price = float(payment.amount)
        # KULLANICI ISTEGI UZERINE CALLBACK SABITLENDI
        callback_url = "https://vidosentegrasyon.com.tr/payment/callback"

        # Telefon No Temizleme (Shopier 10 hane bekler: 5XX... şeklinde)
        phone = "".join(filter(str.isdigit, str(user.phone or '5555555555')))
        if phone.startswith('90'): phone = phone[2:]
        if phone.startswith('0'): phone = phone[1:]
        phone = phone[:10]
        
        # JSON Verisi (Golden Form v2)
        user_data = {
            "buyer_name": clean_text_strict(user.first_name if user.first_name else 'Misafir'),
            "buyer_surname": clean_text_strict(user.last_name if user.last_name else 'Kullanici'),
            "buyer_email": user.email,
            "buyer_phone": phone,
            "buyer_address": "Istanbul, Turkiye",
            "total_order_value": f"{price:.2f}",
            "currency": "TRY",
            "platform_order_id": f"VID_{payment.payment_reference}",
            "callback_url": callback_url,
            "product_name": clean_text_strict(f"Vidos {plan_name}")[:20],
            "website_index": 1, # Integer olarak deneyelim
            "product_type": 1,
            "buyer_id_nr": 0,
            "platform": 0,
            "is_test": 0
        }
        
        # Veriyi Base64'e çevir
        data_string = json.dumps(user_data, separators=(',', ':')) # Compact JSON
        encoded_data = base64.b64encode(data_string.encode()).decode()
        
        # İmza (V2 genellikle data + random_nr kullanır ancak sıralama önemlidir)
        random_nr = generate_transaction_id()
        # BAZI SHOPIER HESAPLARINDA IMZA ICIN SADECE DATA + RANDOM_NR YETERLIDIR
        # ANCAK GARANTI OLMASI ICIN DATA VE RANDOM_NR'YI BIRLESTIRIYORUZ
        signature_data = encoded_data + random_nr
        
        signature = hmac.new(
            self.api_secret.encode('utf-8'),
            signature_data.encode('utf-8'),
            hashlib.sha256
        ).digest()
        
        args = {
            'API_key': self.api_key,
            'data': encoded_data,
            'random_nr': random_nr,
            'signature': base64.b64encode(signature).decode()
        }
        
        # DEBUG: Log results
        try:
            with open('shopier_debug.log', 'a') as f:
                f.write(f"\n[{datetime.now()}] --- V2 PAYMENT ATTEMPT ---\n")
                f.write(f"Payload: {data_string}\n")
                f.write(f"Random Nr: {random_nr}\n")
                f.write(f"Signature: {args['signature']}\n")
        except: pass

        return {
            'success': True,
            'post_url': self.base_url, # Genellikle aynı veya v1/pay
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

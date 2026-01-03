import os
import re
from sqlalchemy import create_engine, text

def get_db_url():
    db_url = None
    if os.path.exists('.env'):
        with open('.env', 'r') as f:
            content = f.read()
            match = re.search(r'DATABASE_URL=["\']?(.+?)["\']?\s*$', content, re.MULTILINE)
            if match:
                db_url = match.group(1)
    return db_url

def run_repair():
    db_url = get_db_url()
    if not db_url:
        print("HATA: .env içinde DATABASE_URL bulunamadı!")
        return

    print(f"PostgreSQL'e bağlanılıyor: {db_url[:15]}...")
    engine = create_engine(db_url)

    # Tablo ve Kolon Tanımları
    schema_updates = [
        # USERS Tablosu
        ('users', 'is_email_verified', 'BOOLEAN DEFAULT FALSE'),
        ('users', 'permissions_json', 'TEXT'),
        ('users', 'email_otp', 'VARCHAR(6)'),
        ('users', 'otp_expiry', 'TIMESTAMP'),
        ('users', 'reset_token', 'VARCHAR(100)'),
        ('users', 'reset_token_expiry', 'TIMESTAMP'),
        ('users', 'company_title', 'VARCHAR(200)'),
        ('users', 'first_name', 'VARCHAR(100)'),
        ('users', 'last_name', 'VARCHAR(100)'),
        ('users', 'tc_no', 'VARCHAR(11)'),
        ('users', 'tax_no', 'VARCHAR(20)'),
        ('users', 'tax_office', 'VARCHAR(100)'),
        ('users', 'phone', 'VARCHAR(20)'),
        ('users', 'country', "VARCHAR(100) DEFAULT 'Türkiye'"),
        ('users', 'city', 'VARCHAR(100)'),
        ('users', 'district', 'VARCHAR(100)'),
        ('users', 'address', 'TEXT'),
        
        # SUBSCRIPTIONS Tablosu (Kritik: Hata buradaydı)
        ('subscriptions', 'max_products', 'INTEGER DEFAULT 100'),
        ('subscriptions', 'max_marketplaces', 'INTEGER DEFAULT 1'),
        ('subscriptions', 'max_xml_sources', 'INTEGER DEFAULT 1'),
        ('subscriptions', 'payment_reference', 'VARCHAR(100)'),
        ('subscriptions', 'price_paid', 'FLOAT DEFAULT 0.0'),
        ('subscriptions', 'currency', "VARCHAR(10) DEFAULT 'TRY'"),

        # ORDERS Tablosu
        ('orders', 'commission_amount', 'FLOAT DEFAULT 0.0'),
        ('orders', 'shipping_fee', 'FLOAT DEFAULT 0.0'),
        ('orders', 'service_fee', 'FLOAT DEFAULT 0.0'),
        ('orders', 'tax_amount', 'FLOAT DEFAULT 0.0'),
        ('orders', 'total_deductions', 'FLOAT DEFAULT 0.0'),
        ('orders', 'net_profit', 'FLOAT DEFAULT 0.0'),
        ('orders', 'items_json', 'TEXT'),
        ('orders', 'cargo_code', 'VARCHAR(50)'),
        ('orders', 'shipment_package_id', 'BIGINT'),

        # PRODUCTS Tablosu
        ('products', 'cost_price', 'FLOAT DEFAULT 0.0'),
        ('products', 'cost_currency', "VARCHAR(3) DEFAULT 'TRY'"),
        ('products', 'brand', 'VARCHAR(255)'),
        ('products', 'desi', 'FLOAT DEFAULT 1.0'),
        ('products', 'attributes_json', 'TEXT'),
        ('products', 'images_json', 'TEXT'),
        ('products', 'top_category', 'VARCHAR(255)'),

        # PAYMENTS Tablosu (YENİ)
        ('payments', 'user_id', 'INTEGER'),
        ('payments', 'subscription_id', 'INTEGER'),
        ('payments', 'amount', 'FLOAT'),
        ('payments', 'currency', "VARCHAR(10) DEFAULT 'TRY'"),
        ('payments', 'plan', 'VARCHAR(50)'),
        ('payments', 'billing_cycle', "VARCHAR(20) DEFAULT 'monthly'"),
        ('payments', 'gateway', 'VARCHAR(50)'),
        ('payments', 'transaction_id', 'VARCHAR(200)'),
        ('payments', 'payment_reference', 'VARCHAR(200)'),
        ('payments', 'status', "VARCHAR(20) DEFAULT 'pending'"),
        ('payments', 'payment_method', 'VARCHAR(50)'),
        ('payments', 'ip_address', 'VARCHAR(50)'),
        ('payments', 'user_agent', 'TEXT'),
        ('payments', 'payment_metadata', 'TEXT'),
        ('payments', 'created_at', 'TIMESTAMP'),
        ('payments', 'completed_at', 'TIMESTAMP'),
        ('payments', 'updated_at', 'TIMESTAMP'),

        # SUPPLIER_XMLS Tablosu (Son Eklenenler)
        ('supplier_xmls', 'use_random_barcode', 'BOOLEAN DEFAULT FALSE'),
        ('supplier_xmls', 'last_cached_at', 'TIMESTAMP'),

        # SYNC_LOGS Tablosu
        ('sync_logs', 'user_id', 'INTEGER')
    ]

    for table, col, dtype in schema_updates:
        # Her kolon için ayrı bir transaction/bağlantı açalım ki biri hata verirse diğeri devam etsin
        with engine.connect() as conn:
            try:
                # PostgreSQL için IF NOT EXISTS desteği (kolon eklemede doğrudan yok, manuel kontrol)
                check_sql = text(f"SELECT column_name FROM information_schema.columns WHERE table_name='{table}' AND column_name='{col}'")
                result = conn.execute(check_sql).fetchone()
                
                if not result:
                    print(f"Ekleniyor: {table}.{col}...")
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}"))
                    conn.commit()
                    print(f"BAŞARILI: {col} eklendi.")
                else:
                    print(f"ATLANDI: {table}.{col} zaten mevcut.")
            except Exception as e:
                print(f"HATA ({table}.{col}): {str(e)[:100]}")

    print("\n[!] Tüm işlemler tamamlandı. Servisleri yeniden başlatabilirsiniz.")

if __name__ == "__main__":
    run_repair()

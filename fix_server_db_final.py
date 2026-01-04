import os
import sys

# Proje dizinini path'e ekle
sys.path.append(os.getcwd())

# .env dosyasını yükle (Sunucu üzerindeki DATABASE_URL vb. için)
try:
    from dotenv import load_dotenv
    load_dotenv()
    print(".env dosyası yüklendi.")
except ImportError:
    print("python-dotenv bulunamadı, sistem environment değişkenleri kullanılacak.")

from app import create_app, db
from sqlalchemy import text, inspect
from app.models import MarketplaceProduct, CachedXmlProduct, PersistentJob

app = create_app('production') # Sunucuda production config kullanılıyor

with app.app_context():
    try:
        print(f"Bağlanılan Veritabanı: {app.config['SQLALCHEMY_DATABASE_URI']}")
        print("--- VERITABANI GÜNCELLEME İŞLEMİ BAŞLATILDI ---")
        
        # 1. Yeni Tabloları Oluştur (db.create_all sadece olmayanları oluşturur)
        print("Yeni tablolar kontrol ediliyor...")
        db.create_all()
        print("Tablo kontrolü tamamlandı.")

        # 2. MarketplaceProduct Tablosuna Eksik Sütunları Ekle
        print("marketplace_products tablosu güncelleniyor...")
        
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        
        def add_column_if_missing(table_name, column_name, column_type, default_val=None):
            columns = [c['name'] for c in inspector.get_columns(table_name)]
            if column_name not in columns:
                print(f"-> {column_name} sütunu ekleniyor...")
                alter_sql = f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                if default_val is not None:
                    alter_sql += f" DEFAULT {default_val}"
                db.session.execute(text(alter_sql))
                db.session.commit()
                print(f"-> {column_name} başarıyla eklendi.")
            else:
                print(f"-> {column_name} zaten mevcut.")

        add_column_if_missing('marketplace_products', 'price', 'DOUBLE PRECISION', '0.0')
        add_column_if_missing('marketplace_products', 'xml_source_id', 'INTEGER')
        
        # Indeks kontrolü ve oluşturma
        print("İndeksler kontrol ediliyor...")
        db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_marketplace_products_xml_source_id ON marketplace_products (xml_source_id);"))
        db.session.commit()
        
        print("--- BAŞARIYLA TAMAMLANDI ---")
        print("Lütfen gunicorn servisini yeniden başlatın: sudo systemctl restart vidos")

    except Exception as e:
        print(f"!!! HATA OLUŞTU !!!: {e}")
        db.session.rollback()
        sys.exit(1)

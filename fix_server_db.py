import os
import sys

# Proje dizinini path'e ekle
sys.path.append(os.getcwd())

from app import create_app, db
from sqlalchemy import text

app = create_app('production') # Veya 'default'

with app.app_context():
    try:
        print("Veritabanı şeması güncelleniyor...")
        
        # PostgreSQL/SQLite için sütun ekleme
        # Sütun var mı kontrolü (Hata almamak için)
        sql_check = "SELECT column_name FROM information_schema.columns WHERE table_name='marketplace_products' AND column_name='xml_source_id';"
        result = db.session.execute(text(sql_check)).fetchone()
        
        if not result:
            print("xml_source_id sütunu ekleniyor...")
            db.session.execute(text("ALTER TABLE marketplace_products ADD COLUMN xml_source_id INTEGER;"))
            db.session.execute(text("CREATE INDEX IF NOT EXISTS ix_marketplace_products_xml_source_id ON marketplace_products (xml_source_id);"))
            db.session.commit()
            print("Başarıyla güncellendi.")
        else:
            print("Sütun zaten mevcut.")
            
    except Exception as e:
        print(f"Hata oluştu: {e}")
        db.session.rollback()

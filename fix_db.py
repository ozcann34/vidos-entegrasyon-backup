from app import create_app, db
from sqlalchemy import text

app = create_app()
with app.app_context():
    print("--- Veritabanı Onarım İşlemi Başladı ---")
    
    # Doğrudan engine üzerinden bağlantı kurarak DDL çalıştırmak daha sağlıklıdır (Özellikle PostgreSQL için)
    with db.engine.connect() as conn:
        try:
            # Sütun var mı kontrol et
            print("Sütun kontrol ediliyor...")
            conn.execute(text('SELECT is_support FROM users LIMIT 1'))
            print("[BİLGİ] is_support sütunu zaten mevcut.")
        except Exception:
            # Sütun yoksa ekle
            print("Sütun eksik. Ekleniyor...")
            try:
                # PostgreSQL/SQLite uyumlu ALTER komutu
                conn.execute(text('ALTER TABLE users ADD COLUMN is_support BOOLEAN DEFAULT FALSE'))
                # Sütunu güncelle (mevcut kayıtlar için)
                conn.execute(text('UPDATE users SET is_support = FALSE WHERE is_support IS NULL'))
                print("[BAŞARILI] is_support sütunu users tablosuna eklendi.")
            except Exception as e:
                print(f"[HATA] Sütun eklenirken bir sorun oluştu: {e}")
    
    print("--- Onarım İşlemi Tamamlandı ---")

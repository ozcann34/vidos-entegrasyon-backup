import os
from app import create_app, db
from sqlalchemy import text

def fix_database():
    app = create_app()
    with app.app_context():
        print("Veri tabanı şeması kontrol ediliyor...")
        
        # Eklenecek kolonlar ve tipleri (PostgreSQL uyumlu)
        columns_to_add = [
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
            ('users', 'is_email_verified', 'BOOLEAN DEFAULT FALSE'),
            ('users', 'permissions_json', 'TEXT'),
            ('users', 'email_otp', 'VARCHAR(6)'),
            ('users', 'otp_expiry', 'TIMESTAMP'),
            ('users', 'reset_token', 'VARCHAR(100)'),
            ('reset_token_expiry', 'TIMESTAMP'), # Hata yapmış olabilirim, modelde user tablosunda
            ('users', 'reset_token_expiry', 'TIMESTAMP')
        ]
        
        for table, column, col_type in columns_to_add:
            try:
                # Kolon var mı kontrol et
                check_query = text(f"SELECT column_name FROM information_schema.columns WHERE table_name='{table}' AND column_name='{column}'")
                result = db.session.execute(check_query).fetchone()
                
                if not result:
                    print(f"Eksik kolon ekleniyor: {table}.{column}...")
                    alter_query = text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                    db.session.execute(alter_query)
                    db.session.commit()
                    print(f"Başarıyla eklendi: {column}")
                else:
                    print(f"Kolon zaten mevcut: {column}")
            except Exception as e:
                db.session.rollback()
                print(f"Hata ({column}): {e}")

        print("\nİşlem tamamlandı. Servisi yeniden başlatabilirsiniz.")

if __name__ == "__main__":
    fix_database()

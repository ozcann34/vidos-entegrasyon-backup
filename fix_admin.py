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

def fix_admin():
    db_url = get_db_url()
    if not db_url:
        print("HATA: .env içinde DATABASE_URL bulunamadı!")
        return

    email = 'bugraerkaradeniz34@gmail.com'
    engine = create_engine(db_url)

    with engine.connect() as conn:
        try:
            # Önce kullanıcıyı kontrol et
            check_sql = text("SELECT id, email, is_admin FROM users WHERE email = :email")
            user = conn.execute(check_sql, {"email": email}).fetchone()
            
            if user:
                print(f"Kullanıcı bulundu: {user.email} (ID: {user.id}, Admin: {user.is_admin})")
                if not user.is_admin:
                    print(f"Admin yetkisi veriliyor...")
                    update_sql = text("UPDATE users SET is_admin = TRUE WHERE id = :id")
                    conn.execute(update_sql, {"id": user.id})
                    conn.commit()
                    print("BAŞARILI: Admin yetkisi verildi.")
                else:
                    print("Kullanıcı zaten admin yetkisine sahip.")
            else:
                print(f"HATA: {email} adresli kullanıcı bulunamadı!")
        except Exception as e:
            print(f"HATA: {str(e)}")

if __name__ == "__main__":
    fix_admin()

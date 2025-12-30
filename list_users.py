
import os
from app import create_app, db
from app.models import User

app = create_app()

def list_users():
    with app.app_context():
        users = User.query.all()
        print("-" * 50)
        print(f"{'ID':<5} | {'Email':<30} | {'Admin':<5}")
        print("-" * 50)
        for user in users:
            print(f"{user.id:<5} | {user.email:<30} | {str(user.is_admin):<5}")
        print("-" * 50)
        print(f"Toplam {len(users)} kullanıcı bulundu.")

if __name__ == "__main__":
    list_users()

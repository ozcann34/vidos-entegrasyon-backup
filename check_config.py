
from app import create_app
import os

app = create_app()

def check():
    with app.app_context():
        print(f"Runtime DB URI: {app.config['SQLALCHEMY_DATABASE_URI']}")
        print(f"Current Working Directory: {os.getcwd()}")
        db_path = app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
        if os.path.exists(db_path):
            print(f"DB File exists: {db_path} (Size: {os.path.getsize(db_path)} bytes)")
        else:
            print(f"DB File MISSING: {db_path}")

if __name__ == "__main__":
    check()

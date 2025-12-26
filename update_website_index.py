import sys
from app import create_app, db
from app.models.settings import Setting

def set_website_index(index):
    app = create_app()
    with app.app_context():
        Setting.set('SHOPIER_WEBSITE_INDEX', index)
        print(f"SHOPIER_WEBSITE_INDEX set to {index}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        set_website_index(sys.argv[1])
    else:
        print("Usage: python update_website_index.py <index>")

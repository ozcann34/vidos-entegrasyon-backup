
from app import create_app
from app.models import Setting
import logging

app = create_app()

def dump_settings():
    with app.app_context():
        print(f"\nALL Settings in DB:")
        settings = Setting.query.all()
        for s in settings:
            val = s.value
            if s.key and ("SECRET" in s.key or "KEY" in s.key or "PASSWORD" in s.key or "TOKEN" in s.key):
                val = val[:4] + "****" if val else ""
            print(f"  UID:{s.user_id} | {s.key}: {val}")

if __name__ == "__main__":
    dump_settings()

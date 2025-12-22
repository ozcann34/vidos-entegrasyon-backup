from app import create_app, db
from app.models.blacklist import Blacklist
from sqlalchemy import text

app = create_app()

with app.app_context():
    try:
        # Check if table exists
        with db.engine.connect() as conn:
            result = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='blacklist'"))
            if result.fetchone():
                print("Table 'blacklist' already exists.")
            else:
                db.create_all()
                print("Table 'blacklist' created successfully.")
    except Exception as e:
        print(f"Error creating table: {e}")

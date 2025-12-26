import os
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from app import create_app, db
from sqlalchemy import text, inspect

app = create_app()

with app.app_context():
    print("--- Production Database Migration Start ---")
    print(f"Database URI: {app.config['SQLALCHEMY_DATABASE_URI']}")
    
    engine = db.engine
    dialect = engine.url.drivername
    print(f"Detected dialect: {dialect}")

    try:
        # Check if column exists using inspector
        inspector = inspect(engine)
        columns = [c['name'] for c in inspector.get_columns('subscriptions')]
        
        if 'is_approved' not in columns:
            print("Adding 'is_approved' column to 'subscriptions' table...")
            if 'postgresql' in dialect:
                db.session.execute(text("ALTER TABLE subscriptions ADD COLUMN is_approved BOOLEAN DEFAULT FALSE"))
            else:
                # SQLite syntax (no IF NOT EXISTS, and standard BOOLEAN)
                db.session.execute(text("ALTER TABLE subscriptions ADD COLUMN is_approved BOOLEAN DEFAULT 0"))
            db.session.commit()
            print("✅ Column added successfully.")
        else:
            print("ℹ️ Column 'is_approved' already exists.")

        # 2. Update existing subscriptions to be approved
        print("Setting existing subscriptions to 'approved'...")
        db.session.execute(text("UPDATE subscriptions SET is_approved = TRUE WHERE is_approved IS FALSE OR is_approved IS NULL"))
        db.session.commit()
        print("✅ Existing records updated.")

    except Exception as e:
        db.session.rollback()
        print(f"❌ Migration Error: {e}")
        import traceback
        traceback.print_exc()

    print("--- Migration Finish ---")

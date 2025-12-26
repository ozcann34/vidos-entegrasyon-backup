from app import create_app, db
from sqlalchemy import text

app = create_app()

with app.app_context():
    print("--- Produciton Database Migration Start ---")
    try:
        # 1. Add column if not exists (PostgreSQL syntax)
        print("Adding 'is_approved' column to 'subscriptions' table...")
        db.session.execute(text("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS is_approved BOOLEAN DEFAULT FALSE"))
        db.session.commit()
        print("✅ Column added successfully (or already existed).")

        # 2. Update existing subscriptions to be approved
        print("Setting existing subscriptions to 'approved'...")
        db.session.execute(text("UPDATE subscriptions SET is_approved = TRUE WHERE is_approved IS FALSE"))
        db.session.commit()
        print("✅ Existing records updated.")

        # 3. Double check column exists
        result = db.session.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='subscriptions' AND column_name='is_approved'"))
        if result.fetchone():
            print("🎉 Migration verified: 'is_approved' column is present.")
        else:
            print("❌ Migration failed: column still not found.")

    except Exception as e:
        db.session.rollback()
        print(f"❌ Migration Error: {e}")
        import traceback
        traceback.print_exc()

    print("--- Migration Finish ---")

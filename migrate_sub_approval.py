import sqlite3
import os

db_path = 'panel.db'
if not os.path.exists(db_path):
    print(f"Error: {db_path} not found")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

try:
    # Add the column
    print("Adding is_approved column to subscriptions table...")
    cursor.execute("ALTER TABLE subscriptions ADD COLUMN is_approved BOOLEAN DEFAULT 0")
    
    # Set existing subscriptions to approved by default (to avoid locking out current users)
    print("Setting existing subscriptions to APPROVED...")
    cursor.execute("UPDATE subscriptions SET is_approved = 1")
    
    conn.commit()
    print("SUCCESS: Database migrated successfully.")
except Exception as e:
    conn.rollback()
    print(f"Error during migration: {e}")
finally:
    conn.close()

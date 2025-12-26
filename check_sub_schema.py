import sqlite3
import os

db_path = 'panel.db'
if not os.path.exists(db_path):
    print(f"Error: {db_path} not found")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

try:
    cursor.execute("PRAGMA table_info(subscriptions)")
    columns = [row[1] for row in cursor.fetchall()]
    print(f"Columns in subscriptions table: {columns}")
    
    if 'is_approved' not in columns:
        print("MISSING: is_approved column is missing from subscriptions table!")
    else:
        print("SUCCESS: is_approved column exists.")
        
except Exception as e:
    print(f"Error: {e}")
finally:
    conn.close()

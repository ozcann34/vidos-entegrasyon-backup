import sqlite3
import os

db_path = 'panel.db'
if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM settings WHERE key LIKE 'SHOPIER_%'")
    results = cursor.fetchall()
    for row in results:
        print(f"{row[0]}: {row[1]}")
    conn.close()
else:
    print("Database not found")

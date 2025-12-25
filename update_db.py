import sqlite3
import os

db_path = 'panel.db'
if not os.path.exists(db_path):
    print(f"Database {db_path} not found.")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

cols_to_add = [
    ('success_count', 'INTEGER DEFAULT 0'),
    ('fail_count', 'INTEGER DEFAULT 0'),
    ('job_type', 'TEXT')
]

for col_name, col_type in cols_to_add:
    try:
        cursor.execute(f"ALTER TABLE batch_logs ADD COLUMN {col_name} {col_type}")
        print(f"Added column {col_name} to batch_logs")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e).lower():
            print(f"Column {col_name} already exists.")
        else:
            print(f"Error adding column {col_name}: {e}")

conn.commit()
conn.close()
print("Database schema update finished.")

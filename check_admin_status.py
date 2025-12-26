import sqlite3
import os

db_path = 'panel.db'
if not os.path.exists(db_path):
    print(f"Error: {db_path} not found")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

try:
    print("--- Users Admin Status ---")
    cursor.execute("SELECT id, email, is_admin FROM users")
    users = cursor.fetchall()
    for user in users:
        print(f"ID: {user[0]}, Email: {user[1]}, Is Admin: {user[2]}")
            
    print("\n--- Subscriptions Check ---")
    cursor.execute("SELECT id, user_id, plan, is_approved FROM subscriptions")
    subs = cursor.fetchall()
    for sub in subs:
        print(f"Sub ID: {sub[0]}, User ID: {sub[1]}, Plan: {sub[2]}, Approved: {sub[3]}")

except Exception as e:
    print(f"Error: {e}")
finally:
    conn.close()

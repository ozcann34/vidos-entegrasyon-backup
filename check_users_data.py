import sqlite3
import os

db_path = 'panel.db'
if not os.path.exists(db_path):
    print(f"Error: {db_path} not found")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

try:
    print("--- Subscriptions Check ---")
    cursor.execute("SELECT id, user_id, plan, status, is_approved FROM subscriptions")
    subs = cursor.fetchall()
    for sub in subs:
        print(f"Sub ID: {sub[0]}, User ID: {sub[1]}, Plan: {sub[2]}, Status: {sub[3]}, Approved: {sub[4]}")
        if sub[2] is None:
            print(f"  WARNING: Plan is NULL for Sub ID {sub[0]}")
            
    print("\n--- Users Check ---")
    cursor.execute("SELECT id, email, created_at, last_login FROM users LIMIT 10")
    users = cursor.fetchall()
    for user in users:
        print(f"User ID: {user[0]}, Email: {user[1]}, Created: {user[2]}, Last Login: {user[3]}")

except Exception as e:
    print(f"Error: {e}")
finally:
    conn.close()

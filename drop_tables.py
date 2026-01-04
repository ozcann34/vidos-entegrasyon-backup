import sqlite3
import os

db_path = 'panel.db'
if os.path.exists(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("DROP TABLE IF EXISTS cached_xml_products;")
    cursor.execute("DROP TABLE IF EXISTS persistent_jobs;")
    conn.commit()
    print("Tables dropped")
    conn.close()
else:
    print("DB not found")

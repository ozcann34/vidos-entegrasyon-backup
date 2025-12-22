import sqlite3
import os

DB_PATH = 'panel.db'

def add_columns():
    if not os.path.exists(DB_PATH):
        print(f"Error: Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    columns_to_add = [
        ('products', 'brand', 'TEXT'),
        ('products', 'desi', 'REAL DEFAULT 1.0'),
        ('products', 'attributes_json', 'TEXT') # Renk, beden vb. için JSON
    ]
    
    for table, col, dtype in columns_to_add:
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col} {dtype}")
            print(f"Added column {col} to {table}")
        except sqlite3.OperationalError as e:
            if 'duplicate column name' in str(e):
                print(f"Column {col} already exists in {table}")
            else:
                print(f"Error adding {col} to {table}: {e}")
                
    conn.commit()
    conn.close()
    print("Schema update completed.")

if __name__ == '__main__':
    add_columns()

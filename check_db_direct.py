import sqlite3
import json

def check_db():
    try:
        conn = sqlite3.connect('panel.db')
        cursor = conn.cursor()
        
        # Check table columns
        cursor.execute("PRAGMA table_info(products)")
        columns = [col[1] for col in cursor.fetchall()]
        print(f"Columns in products table: {columns}")
        
        # Count all products
        cursor.execute("SELECT count(*) FROM products")
        total = cursor.fetchone()[0]
        print(f"Total products: {total}")
        
        # Count manual products
        cursor.execute("SELECT count(*) FROM products WHERE xml_source_id IS NULL")
        manual_null = cursor.fetchone()[0]
        print(f"Manual products (xml_source_id IS NULL): {manual_null}")
        
        # Count manual products if they were saved as 0 or empty string by mistake
        cursor.execute("SELECT count(*) FROM products WHERE xml_source_id = 0")
        manual_zero = cursor.fetchone()[0]
        print(f"Manual products (xml_source_id = 0): {manual_zero}")
        
        # Show first 5 manual products
        cursor.execute("SELECT id, user_id, barcode, title, xml_source_id FROM products WHERE xml_source_id IS NULL LIMIT 5")
        prods = cursor.fetchall()
        print("\nFirst 5 manual products:")
        for p in prods:
            print(f"ID: {p[0]}, UserID: {p[1]}, Barcode: {p[2]}, Title: {p[3]}, SourceID: {p[4]}")
            
        conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_db()

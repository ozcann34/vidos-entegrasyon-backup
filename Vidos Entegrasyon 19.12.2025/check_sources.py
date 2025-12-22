import sqlite3

def check_sources():
    try:
        conn = sqlite3.connect('panel.db')
        cursor = conn.cursor()
        
        # Check SupplierXML
        cursor.execute("SELECT id, user_id, name FROM supplier_xmls")
        sources = cursor.fetchall()
        print("--- Supplier XMLs ---")
        for s in sources:
            print(f"ID: {s[0]}, UserID: {s[1]}, Name: {s[2]}")
            
        # Check if there are products with user_id != 1
        cursor.execute("SELECT user_id, count(*) FROM products GROUP BY user_id")
        user_counts = cursor.fetchall()
        print("\n--- Product Counts per User ---")
        for uc in user_counts:
            print(f"UserID: {uc[0]}, Count: {uc[1]}")
            
        conn.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_sources()

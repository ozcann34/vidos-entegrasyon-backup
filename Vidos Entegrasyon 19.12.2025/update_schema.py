
import sqlite3
import os

# Database file path
DB_PATH = 'panel.db'

def add_column_if_not_exists(cursor, table, column, definition):
    try:
        cursor.execute(f"SELECT {column} FROM {table} LIMIT 1")
    except sqlite3.OperationalError:
        print(f"Adding column {column} to {table}...")
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            print(f"Successfully added {column}.")
        except Exception as e:
            print(f"Failed to add {column}: {e}")
    except Exception as e:
        print(f"Error checking {column}: {e}")

def update_schema():
    if not os.path.exists(DB_PATH):
        print(f"Database not found at {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Update Orders table
    add_column_if_not_exists(cursor, 'orders', 'commission_amount', 'FLOAT DEFAULT 0')
    add_column_if_not_exists(cursor, 'orders', 'shipping_fee', 'FLOAT DEFAULT 0')
    add_column_if_not_exists(cursor, 'orders', 'service_fee', 'FLOAT DEFAULT 0')
    add_column_if_not_exists(cursor, 'orders', 'tax_amount', 'FLOAT DEFAULT 0')
    add_column_if_not_exists(cursor, 'orders', 'total_deductions', 'FLOAT DEFAULT 0')
    add_column_if_not_exists(cursor, 'orders', 'net_profit', 'FLOAT DEFAULT 0')
    add_column_if_not_exists(cursor, 'orders', 'items_json', 'TEXT')

    # Update Products table
    add_column_if_not_exists(cursor, 'products', 'cost_price', 'FLOAT DEFAULT 0')
    add_column_if_not_exists(cursor, 'products', 'cost_currency', "VARCHAR(3) DEFAULT 'TRY'")

    conn.commit()
    conn.close()
    print("Schema update completed.")

if __name__ == '__main__':
    update_schema()

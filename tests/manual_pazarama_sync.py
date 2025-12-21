
import os
import sys
import logging

# Add project root to sys.path
sys.path.append(os.getcwd())

from app import create_app, db
from app.services.pazarama_service import sync_pazarama_products

def run_manual_sync():
    app = create_app()
    with app.app_context():
        user_id = 1
        print(f"--- Manual Pazarama Sync (User ID: {user_id}) ---")
        
        result = sync_pazarama_products(user_id=user_id)
        print(f"Sync Result: {result}")

if __name__ == "__main__":
    run_manual_sync()

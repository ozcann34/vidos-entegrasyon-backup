
import os
import sys
import logging

# Add project root to sys.path
sys.path.append(os.getcwd())

from app import create_app, db
from app.models import Setting, MarketplaceProduct
from app.services.pazarama_service import get_pazarama_client

def test_pazarama_count():
    app = create_app()
    with app.app_context():
        # Get the first user or a specific user_id if known
        # In a typical dev environment, it's often user_id=1
        user_id = 1 
        
        print(f"--- Pazarama Count Test (User ID: {user_id}) ---")
        
        # 1. Check DB count
        db_count = db.session.query(MarketplaceProduct).filter_by(user_id=user_id, marketplace='pazarama').count()
        print(f"Local DB Count (MarketplaceProduct): {db_count}")
        
        # 2. Check API Fallback
        try:
            client = get_pazarama_client(user_id=user_id)
            print("Pazarama Client created successfully.")
            
            # Direct API call
            url = "https://isortagimapi.pazarama.com/product/products"
            params = {"Page": 1, "Size": 10}
            
            print(f"Requesting: {url} with params {params}")
            resp = client._request("GET", url, params=params)
            print(f"Response Status: {resp.status_code}")
            print(f"Response Headers: {resp.headers}")
            
            data = resp.json()
            import json
            # print(f"Response JSON: {json.dumps(data, indent=2)}")
            print(f"Response Keys: {list(data.keys())}")
            
            if "data" in data and isinstance(data["data"], list):
                print(f"Data length: {len(data['data'])}")
                if len(data["data"]) > 0:
                    print(f"First item keys: {list(data['data'][0].keys())}")
            
            # Check for ANY key containing 'count' or 'total'
            def find_keys(d, prefix=""):
                if isinstance(d, dict):
                    for k, v in d.items():
                        if "count" in k.lower() or "total" in k.lower():
                            print(f"Found match: {prefix}{k} = {v}")
                        find_keys(v, prefix + k + ".")
                elif isinstance(d, list) and len(d) > 0:
                     # Only check first element for keys
                     if isinstance(d[0], dict):
                         find_keys(d[0], prefix + "0.")

            print("Searching for 'count' or 'total' in JSON...")
            find_keys(data)
            
        except Exception as e:
            print(f"Error during Pazarama API test: {e}")

if __name__ == "__main__":
    test_pazarama_count()

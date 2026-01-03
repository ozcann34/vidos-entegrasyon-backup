
import logging
import sys
import os

# Add project root to path
sys.path.append(os.getcwd())

from app import create_app
from app.services.pazarama_service import get_pazarama_client
from app.services.idefix_service import get_idefix_client

app = create_app()

def debug_db():
    print("--- DB CHECK ---")
    with app.app_context():
        from app.models import MarketplaceProduct
        UID = 2
        tp = MarketplaceProduct.query.filter_by(user_id=UID, marketplace='trendyol').count()
        pz = MarketplaceProduct.query.filter_by(user_id=UID, marketplace='pazarama').count()
        idx = MarketplaceProduct.query.filter_by(user_id=UID, marketplace='idefix').count()
        n11 = MarketplaceProduct.query.filter_by(user_id=UID, marketplace='n11').count()
        print(f"DB Counts for UID {UID}: Trendyol={tp}, Pazarama={pz}, Idefix={idx}, N11={n11}")

def debug_pazarama():
    print("--- PAZARAMA DEBUG ---")
    with app.app_context():
        try:
            UID = 2
            client = get_pazarama_client(user_id=UID)
            # Try with larger size
            url = f"https://isortagimapi.pazarama.com/product/products"
            params = {"Page": 1, "Size": 1}
            resp = client._request("GET", url, params=params)
            data = resp.json()
            import json
            print("Full Data Structure:")
            print(json.dumps({k: (v if not isinstance(v, list) else f"List len {len(v)}") for k,v in data.items()}, indent=2))
        except Exception as e:
            print(f"Pazarama Error: {e}")

def debug_idefix():
    print("\n--- IDEFIX DEBUG ---")
    with app.app_context():
        try:
            UID = 2
            client = get_idefix_client(user_id=UID)
            print(f"Checking vendor_id: {client.vendor_id}")
            # Try listing products
            res = client.list_products(page=0, limit=1)
            print(f"Idefix totalElements from list_products (no filter): {res.get('totalElements')}")
            
            # Check states again
            POOL_STATES = ["APPROVED", "WAITING_APPROVAL", "REJECTED", "WAITING_CONTENT", "DELETED", "IN_REVISION", "ARCHIVED"]
            for state in POOL_STATES:
                try:
                    r = client.list_products(page=0, limit=1, pool_state=state)
                    print(f"  State {state}: {r.get('totalElements')}")
                except:
                    pass
            
        except Exception as e:
            print(f"Idefix Error: {e}")

if __name__ == "__main__":
    debug_db()
    debug_pazarama()
    debug_idefix()

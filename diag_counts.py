
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
        tp = MarketplaceProduct.query.filter_by(user_id=1, marketplace='trendyol').count()
        pz = MarketplaceProduct.query.filter_by(user_id=1, marketplace='pazarama').count()
        idx = MarketplaceProduct.query.filter_by(user_id=1, marketplace='idefix').count()
        n11 = MarketplaceProduct.query.filter_by(user_id=1, marketplace='n11').count()
        print(f"DB Counts: Trendyol={tp}, Pazarama={pz}, Idefix={idx}, N11={n11}")

def debug_pazarama():
    print("--- PAZARAMA DEBUG ---")
    with app.app_context():
        try:
            client = get_pazarama_client(user_id=1)
            # Try with larger size
            url = f"https://isortagimapi.pazarama.com/product/products"
            params = {"Page": 1, "Size": 10}
            resp = client._request("GET", url, params=params)
            data = resp.json()
            print(f"Structure with Size 10: {list(data.keys())}")
            if 'paging' in data: print("Found PAGING!")
            if 'totalCount' in data: print(f"Found totalCount: {data['totalCount']}")
            
            # Try another endpoint?
            url2 = "https://isortagimapi.pazarama.com/product/getInventory" # Just a guess
            try:
                resp2 = client._request("GET", url2, params={"Page": 1, "Size": 1})
                print(f"getInventory success. Keys: {list(resp2.json().keys())}")
            except:
                print("getInventory failed.")
        except Exception as e:
            print(f"Pazarama Error: {e}")

def debug_idefix():
    print("\n--- IDEFIX DEBUG ---")
    with app.app_context():
        try:
            client = get_idefix_client(user_id=1)
            # Try count endpoint
            url_count = f"{client.BASE_URL}/pim/pool/{client.vendor_id}/count"
            try:
                resp_c = client.session.get(url_count, headers=client._get_headers())
                print(f"Count endpoint status: {resp_c.status_code}")
                if resp_c.status_code == 200:
                    print(f"Count endpoint data: {resp_c.json()}")
            except:
                 print("Count endpoint failed.")
                 
            # Try summary endpoint?
            url_sum = f"{client.BASE_URL}/pim/pool/{client.vendor_id}/summary"
            try:
                resp_s = client.session.get(url_sum, headers=client._get_headers())
                print(f"Summary endpoint status: {resp_s.status_code}")
                if resp_s.status_code == 200:
                    print(f"Summary endpoint data: {resp_s.json()}")
            except:
                 print("Summary endpoint failed.")
            
        except Exception as e:
            print(f"Idefix Error: {e}")

if __name__ == "__main__":
    debug_db()
    debug_pazarama()
    debug_idefix()

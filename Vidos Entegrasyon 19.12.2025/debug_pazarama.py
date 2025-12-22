from app import create_app
from app.services.pazarama_service import get_pazarama_client
from app.services.pazarama_client import BASE_URL
import json

app = create_app()
BASE_URL = "https://isortagimapi.pazarama.com"

with app.app_context():
    print("Initializing Pazarama Client...")
    try:
        client = get_pazarama_client()
        if not client:
            print("Client init failed.")
            exit(1)
            
        print("Fetching product count...")
        count = client.get_product_count()
        print(f"Count returned: {count}")
        
        # Manually inspect the raw response for the same call
        url = f"{BASE_URL}/product/products"
        params = {"Page": 1, "Size": 1}
        print(f"Requesting {url} with params {params}")
        resp = client._request("GET", url, params=params)
        data = resp.json()
        print("Raw Response Keys:", list(data.keys()))
        if 'data' in data:
            print("Data keys:", list(data['data'].keys()) if isinstance(data['data'], dict) else "Data is list or other")
            if isinstance(data['data'], dict):
                 print("Data content sample:", str(data['data'])[:500])
        
        print("Full raw response (first 1000 chars):")
        print(json.dumps(data, indent=2)[:1000])
        
        print("\nResponse Headers:")
        for k, v in resp.headers.items():
            print(f"{k}: {v}")
            
        print("\nTrying Size=0...")
        resp2 = client._request("GET", url, params={"Page": 1, "Size": 0})
        print(f"Size=0 Response: {resp2.text[:200]}")

    except Exception as e:
        print(f"Error: {e}")

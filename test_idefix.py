
import base64
import requests
import json
from app import create_app
from app.services.idefix_service import get_idefix_client

app = create_app()
with app.app_context():
    # Try to get client for user 1 (or any available user)
    client = get_idefix_client(user_id=1)
    if not client or not client.api_key:
        print("API keys not found for user 1")
    else:
        print(f"Testing Idefix Category API with key: {client.api_key[:5]}...")
        try:
            url = f"{client.BASE_URL}/pim/product-category"
            headers = client._get_headers()
            print(f"URL: {url}")
            # print(f"Headers: {headers}")
            
            response = requests.get(url, headers=headers, timeout=60)
            print(f"Status: {response.status_code}")
            if response.status_code == 200:
                data = response.json()
                print(f"Type: {type(data)}")
                if isinstance(data, list):
                    print(f"Count (Top Level): {len(data)}")
                    # Sample first item
                    if data:
                        print(f"First item: {data[0].get('name')} (ID: {data[0].get('id')})")
                        print(f"Subs count: {len(data[0].get('subs', []))}")
                elif isinstance(data, dict):
                    print(f"Keys: {list(data.keys())}")
            else:
                print(f"Error Body: {response.text[:200]}")
        except Exception as e:
            print(f"Exception: {e}")

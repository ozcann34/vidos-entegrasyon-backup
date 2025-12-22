from app import create_app
from app.services.n11_client import get_n11_client
import json

app = create_app()

with app.app_context():
    client = get_n11_client()
    if not client:
        print("Client not created (missing keys?)")
        exit(1)
    
    print("Fetching categories...")
    try:
        cats = client.get_categories()
        print(f"Type: {type(cats)}")
        if isinstance(cats, dict):
            print(f"Keys: {list(cats.keys())}")
            import json
            # Print first level keys and types
            for k, v in cats.items():
                print(f"Key: {k}, Type: {type(v)}")
                if isinstance(v, dict):
                    print(f"  SubKeys of {k}: {list(v.keys())}")
                elif isinstance(v, list):
                     print(f"  List Length of {k}: {len(v)}")
                     if len(v) > 0:
                         print(f"  First item keys: {v[0].keys() if isinstance(v[0], dict) else v[0]}")
        else:
            print("Response is not a dict.")
            print(str(cats)[:500])
            
    except Exception as e:
        print(f"Error: {e}")

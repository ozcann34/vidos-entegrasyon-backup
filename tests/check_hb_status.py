import sys
import os

# Add the project root to sys.path
sys.path.append(os.getcwd())

from app import create_app
from app.services.hepsiburada_service import get_hepsiburada_client

def check_status(tracking_id):
    app = create_app()
    with app.app_context():
        try:
            client = get_hepsiburada_client()
            print(f"Checking status for: {tracking_id}")
            result = client.get_catalog_import_status(tracking_id)
            import json
            print(json.dumps(result, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"Error checking status: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python tests/check_hb_status.py <tracking_id>")
    else:
        check_status(sys.argv[1])

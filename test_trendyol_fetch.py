from app import create_app
from app.services.trendyol_service import get_trendyol_client

app = create_app()
with app.app_context():
    client = get_trendyol_client()
    print("Fetching first page...")
    resp = client.list_products(page=0, size=50)
    print(f"Total items: {resp.get('totalElements', 'Unknown')}")
    items = resp.get('content', [])
    print(f"Fetched {len(items)} items.")
    if items:
        print(f"Sample item: {items[0].get('barcode')} - {items[0].get('title')}")

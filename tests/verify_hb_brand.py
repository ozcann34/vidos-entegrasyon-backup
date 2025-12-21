import sys
import os

# Add the project root to sys.path
sys.path.append(os.getcwd())

from app import create_app
from app.services.hepsiburada_service import resolve_hepsiburada_brand, get_hepsiburada_client

def verify_brand(brand_name):
    app = create_app()
    with app.app_context():
        try:
            client = get_hepsiburada_client()
            print(f"Resolving brand: {brand_name}")
            resolved = resolve_hepsiburada_brand(brand_name, client)
            print(f"Resolved to: {resolved}")
            
            # Check database
            from app.models.mapping import BrandMapping
            mapping = BrandMapping.query.filter_by(source_brand=brand_name, marketplace='hepsiburada').first()
            if mapping:
                print(f"Database Mapping Found: {mapping.source_brand} -> {mapping.target_brand_name}")
            else:
                print("No database mapping found!")
                
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python tests/verify_hb_brand.py <brand_name>")
    else:
        verify_brand(sys.argv[1])

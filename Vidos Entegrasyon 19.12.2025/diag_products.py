from app import create_app, db
from app.models import Product, User
import json

app = create_app('default')
with app.app_context():
    # List all users to see who has products
    users = User.query.all()
    print(f"--- Users ---")
    for u in users:
        print(f"ID: {u.id}, Email: {u.email}")
    
    print(f"\n--- Manual Products (xml_source_id IS None) ---")
    manual_prods = Product.query.filter(Product.xml_source_id == None).all()
    print(f"Found {len(manual_prods)} manual products in total.")
    
    for p in manual_prods:
        print(f"ID: {p.id}, UserID: {p.user_id}, Barcode: {p.barcode}, Title: {p.title}")
    
    # Check if there are products with xml_source_id=0 or other values that might look like manual
    other_prods = Product.query.filter(Product.xml_source_id != None).limit(5).all()
    if other_prods:
        print(f"\n--- Some XML Products (for comparison) ---")
        for p in other_prods:
            print(f"ID: {p.id}, SourceID: {p.xml_source_id}, Barcode: {p.barcode}")

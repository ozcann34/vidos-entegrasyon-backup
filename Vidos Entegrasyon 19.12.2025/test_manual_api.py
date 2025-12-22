from app import create_app
from app.models import Product
import json

app = create_app('default')
with app.app_context():
    # Find manual products for the first user (usually the admin/user)
    # Check all users if needed
    products = Product.query.filter_by(xml_source_id=None).all()
    print(f"Total manual products found: {len(products)}")
    for p in products:
        print(f"Barcode: {p.barcode}, Title: {p.title}, Brand: {p.brand}")

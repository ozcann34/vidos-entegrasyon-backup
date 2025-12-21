
import os
import sys
sys.path.append(os.getcwd())

from app import create_app, db
from app.models import MarketplaceProduct

app = create_app()
with app.app_context():
    results = db.session.query(MarketplaceProduct.user_id, db.func.count(MarketplaceProduct.id))\
        .filter_by(marketplace='pazarama')\
        .group_by(MarketplaceProduct.user_id).all()
    
    print("--- Pazarama User ID Distribution ---")
    for user_id, count in results:
        print(f"User ID: {user_id}, Count: {count}")
    
    if not results:
        print("No Pazarama products found in DB.")

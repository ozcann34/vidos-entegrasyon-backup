
import os
import sys
sys.path.append(os.getcwd())

from app import create_app, db
from app.models import Setting, MarketplaceProduct, User

app = create_app()
with app.app_context():
    print("--- User IDs in System ---")
    users = User.query.all()
    for u in users:
        print(f"User ID: {u.id}, Email: {u.email}")
        
    print("\n--- Pazarama Settings (User ID 1) ---")
    print(f"LAST_DASHBOARD_SYNC: {Setting.get('LAST_DASHBOARD_SYNC', user_id=1)}")
    
    print("\n--- MarketplaceProduct Distributions (All Users) ---")
    results = db.session.query(MarketplaceProduct.user_id, MarketplaceProduct.marketplace, db.func.count(MarketplaceProduct.id))\
        .group_by(MarketplaceProduct.user_id, MarketplaceProduct.marketplace).all()
    for uid, mp, c in results:
        print(f"User ID: {uid}, Marketplace: {mp}, Count: {c}")


from app import create_app, db
from app.models import MarketplaceProduct, User
import logging

app = create_app()

def debug():
    with app.app_context():
        # 1. Sample products for User 2
        products = MarketplaceProduct.query.filter_by(user_id=2).limit(5).all()
        print(f"Sample Products for User 2:")
        for p in products:
            print(f"  ID:{p.id} | MP:{p.marketplace} | Barcode:{p.barcode} | Title:{p.title}")
            
        # 2. Check if any product exists for ANY user other than 2
        other_products = MarketplaceProduct.query.filter(MarketplaceProduct.user_id != 2).count()
        print(f"\nProducts for users other than UID 2: {other_products}")
        
        # 3. Double check User 2 existence
        u2 = User.query.get(2)
        if u2:
            print(f"\nUser 2: {u2.email} (Active: {u2.is_active})")
        else:
            print(f"\nUser 2 NOT FOUND in User table (orphaned data?)")

if __name__ == "__main__":
    debug()

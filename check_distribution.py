
from app import create_app, db
from app.models import MarketplaceProduct
from sqlalchemy import text, func
import logging

app = create_app()

def check():
    with app.app_context():
        # Group by marketplace and user_id
        res = db.session.query(
            MarketplaceProduct.marketplace, 
            MarketplaceProduct.user_id, 
            func.count(MarketplaceProduct.id)
        ).group_by(
            MarketplaceProduct.marketplace, 
            MarketplaceProduct.user_id
        ).all()
        
        print(f"Marketplace Product Distribution:")
        for mp, uid, count in res:
            print(f"  MP: {mp} | UID: {uid} | Count: {count}")

if __name__ == "__main__":
    check()

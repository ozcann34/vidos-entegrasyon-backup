
from app import create_app
from app.models import User, MarketplaceProduct
import logging

app = create_app()

def check():
    with app.app_context():
        users = User.query.all()
        print(f"Total Users: {len(users)}")
        for u in users:
            print(f"\nUser: {u.id} - {u.email}")
            for mp in ['trendyol', 'pazarama', 'hepsiburada', 'idefix', 'n11']:
                count = MarketplaceProduct.query.filter_by(user_id=u.id, marketplace=mp).count()
                print(f"  {mp}: {count} in DB")

if __name__ == "__main__":
    check()

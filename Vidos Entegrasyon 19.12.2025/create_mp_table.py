from app import create_app, db
from app.models import MarketplaceProduct

app = create_app()

with app.app_context():
    print("Creating MarketplaceProduct table...")
    # This will only create tables that don't exist
    db.create_all()
    print("Done.")

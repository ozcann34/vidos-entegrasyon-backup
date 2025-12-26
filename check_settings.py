from app import create_app, db
from app.models.settings import Setting

app = create_app()
with app.app_context():
    s = Setting.query.filter_by(key='SHOPIER_WEBSITE_INDEX').first()
    if s:
        print(f"SHOPIER_WEBSITE_INDEX: {s.value}")
    else:
        print("SHOPIER_WEBSITE_INDEX not found in DB")
        
    # Also check other Shopier settings
    keys = ['SHOPIER_API_KEY', 'SHOPIER_API_SECRET']
    for k in keys:
        s = Setting.query.filter_by(key=k).first()
        if s:
            print(f"{k}: {s.value}")
        else:
            print(f"{k} not found in DB")

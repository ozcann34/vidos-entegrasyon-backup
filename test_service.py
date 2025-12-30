
import requests
from app import create_app
from flask import url_for

app = create_app()
with app.app_context():
    # We can't easily mock login here without a full session, 
    # but we can try to call the service function directly first to see if it even works.
    from app.services.idefix_service import fetch_and_cache_categories
    print("Calling fetch_and_cache_categories(user_id=1) directly...")
    try:
        # Use user 1 keys if they exist in DB
        res = fetch_and_cache_categories(user_id=1)
        print(f"Result: {res}")
    except Exception as e:
        import traceback
        print(f"Direct Call Failed: {e}")
        traceback.print_exc()

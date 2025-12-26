from app import create_app, db
from flask import url_for

app = create_app()

with app.app_context():
    try:
        from app.routes.auth import login
        from flask import request
        
        with app.test_request_context('/login'):
            print("Testing auth.login route...")
            res = login()
            print("SUCCESS: Login page rendered!")
            
    except Exception as e:
        import traceback
        print("\n!!! LOGIN ERROR DETECTED !!!")
        traceback.print_exc()

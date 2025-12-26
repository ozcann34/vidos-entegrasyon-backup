from app import create_app, db
from flask import render_template, request

app = create_app()

with app.app_context():
    try:
        from flask_login import current_user
        
        with app.test_request_context('/login'):
            # current_user will be anonymous here
            print(f"Testing anonymous user: Authenticated={current_user.is_authenticated}")
            
            print("Rendering _base.html...")
            # This will render _base.html as it's the base
            res = render_template('_base.html')
            print("SUCCESS: _base.html rendered!")
            
    except Exception as e:
        import traceback
        print("\n!!! TEMPLATE ERROR DETECTED !!!")
        traceback.print_exc()

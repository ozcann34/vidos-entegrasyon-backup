from app import create_app, db
from app.models import User, Subscription
from flask import render_template, request

app = create_app()

with app.app_context():
    try:
        # Get an admin user
        admin = User.query.filter_by(email='bugraerkaradeniz34@gmail.com').first()
        if not admin:
            print("Admin user not found in DB!")
            exit(1)

        # Get some users for the pagination object simulation
        from app.routes.admin import users
        
        with app.test_request_context('/admin-secret-panel/users'):
            from flask_login import login_user
            # Use the real login_user inside test_request_context
            login_user(admin)
            
            print("Calling users() function...")
            # This will trigger the template rendering
            res = users()
            print("SUCCESS: Function returned normally.")
            # print(res[:500]) # Print first 500 chars of HTML
            
    except Exception as e:
        import traceback
        print("\n!!! ERROR DETECTED !!!")
        traceback.print_exc()

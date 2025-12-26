from app import create_app, db
from app.models import User, Subscription
from datetime import datetime

app = create_app()

with app.app_context():
    try:
        users = User.query.all()
        print(f"Checking {len(users)} users...")
        
        for user in users:
            print(f"Checking User ID: {user.id} ({user.email})")
            
            # 1. Check basic attributes
            _ = user.full_name
            _ = user.email
            _ = user.is_banned
            _ = user.is_admin
            _ = user.is_active
            
            if user.created_at:
                try:
                    user.created_at.strftime('%d.%m.%Y')
                except Exception as e:
                    print(f"  ERROR: created_at strftime failed for User {user.id}: {e}")
            
            if user.last_login:
                try:
                    user.last_login.strftime('%d.%m.%Y %H:%M')
                except Exception as e:
                    print(f"  ERROR: last_login strftime failed for User {user.id}: {e}")
            
            # 2. Check subscription
            if user.subscription:
                _ = user.subscription.plan
                _ = user.subscription.is_approved
                try:
                    _ = user.subscription.plan_display_name
                except Exception as e:
                    print(f"  ERROR: plan_display_name failed for User {user.id}: {e}")
            
            # 3. Check for specific properties used in templates
            try:
                _ = user.is_super_admin
            except Exception as e:
                print(f"  ERROR: is_super_admin failed for User {user.id}: {e}")

        print("--- All data checks passed at code level ---")
        
        # 4. Try rendering the specific template with these users
        from flask import render_template
        with app.test_request_context('/admin-secret-panel/users'):
            from flask_login import login_user
            admin = User.query.filter_by(is_admin=True).first()
            login_user(admin)
            
            # Mock pagination object since users.html expects users.items
            class MockPagination:
                def __init__(self, items):
                    self.items = items
                    self.pages = 1
                    self.has_prev = False
                    self.has_next = False
                    self.page = 1
                def iter_pages(self, **kwargs):
                    return [1]
            
            mock_users = MockPagination(users)
            print("Trying to render_template('admin/users.html')...")
            render_template('admin/users.html', users=mock_users, search='')
            print("RENDER SUCCESS!")

    except Exception as e:
        import traceback
        print("\n!!! CRITICAL ERROR !!!")
        traceback.print_exc()

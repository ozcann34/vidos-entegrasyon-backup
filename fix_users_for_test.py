from app import create_app, db
from app.models.user import User
from app.models.subscription import Subscription
import json

app = create_app()
with app.app_context():
    # 1. Reset bugra password
    admin = User.query.filter_by(email='bugraerkaradeniz34@gmail.com').first()
    if admin:
        admin.set_password('admin1')
        admin.is_admin = True
        print(f"Password reset for {admin.email}")
    
    # 2. Grant permissions to test@test.com
    test_user = User.query.filter_by(email='test@test.com').first()
    if test_user:
        test_user.is_admin = True # Make it admin for easier testing
        
        # Ensure it has an enterprise subscription
        sub = Subscription.query.filter_by(user_id=test_user.id).first()
        if not sub:
            sub = Subscription(user_id=test_user.id)
            db.session.add(sub)
            
        sub.plan = 'enterprise'
        sub.status = 'active'
        sub.allowed_marketplaces = json.dumps(['trendyol', 'pazarama', 'n11', 'hepsiburada', 'idefix'])
        
        print(f"Granted full permissions to {test_user.email}")
        
    db.session.commit()

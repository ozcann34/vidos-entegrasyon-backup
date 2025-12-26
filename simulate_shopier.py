from app import create_app, db
from app.models.payment import Payment
from app.models.user import User
from app.services.payment_service import ShopierAdapter
import json

def simulate_payment():
    app = create_app()
    with app.app_context():
        # Create a mock user if none exists
        user = User.query.first()
        if not user:
            user = User(
                email="test@vidos.com",
                first_name="Test",
                last_name="User",
                phone="5555555555"
            )
            db.session.add(user)
            db.session.commit()
        
        payment = Payment(
            user_id=user.id,
            plan='pro',
            billing_cycle='monthly',
            amount=999.00,
            currency='TRY',
            status='pending',
            payment_reference='SIM_12345'
        )
        payment.user = user # Ensure relation is loaded
        
        adapter = ShopierAdapter()
        result = adapter.initiate_payment_v2(payment)
        
        if result['success']:
            print(f"Post URL: {result['post_url']}")
            print("Params:")
            for k, v in result['params'].items():
                print(f"  {k}: {v}")
        else:
            print(f"Error: {result['message']}")

if __name__ == "__main__":
    simulate_payment()

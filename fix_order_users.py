
from app import create_app, db
from app.models import Order, User

app = create_app()
with app.app_context():
    try:
        u = User.query.first()
        if u:
            print(f"Assigning orphan orders to User {u.id} ({u.email})...")
            # Filter where user_id is None
            orders = Order.query.filter(Order.user_id == None).all()
            count = len(orders)
            print(f"Found {count} orphan orders.")
            if count > 0:
                for o in orders:
                    o.user_id = u.id
                db.session.commit()
                print("Successfully updated orphan orders.")
            else:
                print("No orphan orders found.")
        else:
            print("No user found in database.")
    except Exception as e:
        print(f"Error: {e}")

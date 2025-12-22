
from app import create_app, db
from app.models import Order, User

app = create_app()

with app.app_context():
    # Find first user
    user = User.query.first()
    if not user:
        print("No user found!")
    else:
        print(f"Assigning Order 77 to User {user.email} (ID: {user.id})")
        order = Order.query.get(77)
        if order:
            order.user_id = user.id
            db.session.commit()
            print("Order 77 updated successfully.")
        else:
            print("Order 77 not found.")

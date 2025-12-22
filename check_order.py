
from app import create_app, db
from app.models import Order, User

app = create_app()

with app.app_context():
    order = Order.query.get(77)
    if order:
        print(f"Order found: ID={order.id}, UserID={order.user_id}")
        user = User.query.get(order.user_id)
        if user:
            print(f"Owner: {user.username} (ID: {user.id})")
        else:
            print("Owner user not found")
    else:
        print("Order 77 not found in DB")
        # List last 5 orders
        print("Last 5 orders:")
        last = Order.query.order_by(Order.id.desc()).limit(5).all()
        for o in last:
            print(f"ID={o.id}, UserID={o.user_id}")

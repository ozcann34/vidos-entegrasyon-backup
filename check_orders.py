
from app import create_app,db
from app.models import Order, User

app = create_app()
with app.app_context():
    orders = Order.query.all()
    print(f"Total Orders: {len(orders)}")
    for o in orders:
        print(f"Order {o.order_number}: user_id={o.user_id}, status={o.status}")
        
    # Check users
    users = User.query.all()
    print("Users:", [(u.id, u.email) for u in users])

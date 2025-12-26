from app import create_app, db
from app.models import User

app = create_app()

with app.app_context():
    users = User.query.all()
    for user in users:
        print(f"User {user.id}:")
        print(f"  created_at type: {type(user.created_at)} value: {user.created_at}")
        print(f"  last_login type: {type(user.last_login)} value: {user.last_login}")

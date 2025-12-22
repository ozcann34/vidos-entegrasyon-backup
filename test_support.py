from app import create_app, db
from app.services.support_service import create_ticket
from app.models.user import User

app = create_app()
with app.app_context():
    user = User.query.filter_by(email='test@test.com').first()
    if not user:
        print("No test user found.")
    else:
        print(f"Testing ticket creation for user: {user.email}")
        ticket = create_ticket(user.id, "Test Subject", "Test Message")
        if ticket:
            print(f"Success! Ticket ID: {ticket.id}")
        else:
            print("Failed to create ticket.")

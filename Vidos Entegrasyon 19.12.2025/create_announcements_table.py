from app import create_app, db
from app.models.announcement import Announcement

app = create_app()

with app.app_context():
    print("Creating announcements table...")
    # Create table directly using SQLAlchemy engine
    Announcement.__table__.create(db.engine)
    print("Table created successfully.")

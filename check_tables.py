
from app import create_app, db
from sqlalchemy import inspect, text
import logging

app = create_app()

def check():
    with app.app_context():
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()
        print(f"Tables: {tables}")
        for t in tables:
            try:
                # Get row count
                res = db.session.execute(text(f"SELECT COUNT(*) FROM {t}"))
                count = res.scalar()
                print(f"  {t}: {count} rows")
            except Exception as e:
                print(f"  {t}: Error {e}")

if __name__ == "__main__":
    check()

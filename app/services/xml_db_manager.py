from app import db
from app.models import CachedXmlProduct
import logging

class XmlDbManager:
    """
    Manages XML product caching in the main PostgreSQL database.
    Replaces the old SQLite-per-source approach for high performance.
    """
    def __init__(self):
        pass

    def get_session(self):
        """Returns the main DB session."""
        return db.session

    def get_products(self, xml_source_id: int):
        """Helper to get cached products for a specific source."""
        return CachedXmlProduct.query.filter_by(xml_source_id=xml_source_id).all()

    def clear_cache(self, xml_source_id: int):
        """Clears cache for a specific source."""
        try:
            CachedXmlProduct.query.filter_by(xml_source_id=xml_source_id).delete()
            db.session.commit()
            return True
        except Exception as e:
            logging.error(f"Error clearing XML cache: {e}")
            db.session.rollback()
            return False

# Singleton instance
xml_db_manager = XmlDbManager()

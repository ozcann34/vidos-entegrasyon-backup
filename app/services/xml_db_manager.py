import os
from sqlalchemy import create_engine, Column, Integer, String, Float, Text, DateTime, Index
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
from typing import Optional

Base = declarative_base()

class CachedXmlProduct(Base):
    __tablename__ = 'cached_products'
    
    id = Column(Integer, primary_key=True)
    stock_code = Column(String(200), index=True, nullable=False)
    barcode = Column(String(200), index=True)
    title = Column(String(500))
    price = Column(Float, default=0.0)
    quantity = Column(Integer, default=0)
    brand = Column(String(200))
    category = Column(String(500))
    images_json = Column(Text)
    raw_data = Column(Text) # Kaynak verinin tamamı (JSON)
    last_updated = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # Composite index for faster lookups if needed
    __table_args__ = (
        Index('idx_stock_code', 'stock_code'),
    )

class XmlDbManager:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.cache_dir = os.path.join(base_dir, 'instance', 'xml_cache')
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir, exist_ok=True)

    def get_db_path(self, xml_source_id: int) -> str:
        return os.path.join(self.cache_dir, f"source_{xml_source_id}.db")

    def get_session(self, xml_source_id: int):
        db_path = self.get_db_path(xml_source_id)
        engine = create_engine(f"sqlite:///{db_path}")
        
        # Ensure tables exist
        Base.metadata.create_all(engine)
        
        Session = sessionmaker(bind=engine)
        return Session()

# Singleton instance
from config import Config
xml_db_manager = XmlDbManager(Config.BASE_DIR)

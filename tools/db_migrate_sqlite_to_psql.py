import os
from sqlalchemy import create_all, create_engine, MetaData
from sqlalchemy.orm import sessionmaker
from app import create_app, db

# Setup engines
sqlite_uri = "sqlite:///panel.db"
# Change this to your target PostgreSQL URI
postgres_uri = os.environ.get('DATABASE_URL')

if not postgres_uri or not postgres_uri.startswith('postgresql'):
    print("HATA: DATABASE_URL (PostgreSQL) ortam değişkeni ayarlanmamış veya hatalı!")
    exit(1)

app = create_app()

def migrate():
    with app.app_context():
        # Create engines
        src_engine = create_engine(sqlite_uri)
        dst_engine = create_engine(postgres_uri)
        
        print(f"Başlatılıyor: {sqlite_uri} -> {postgres_uri}")
        
        # Create tables in destination
        db.create_all(bind=dst_engine)
        print("Hedef veritabanında tablolar oluşturuldu.")
        
        metadata = MetaData()
        metadata.reflect(bind=src_engine)
        
        # Copy data for each table
        for table in metadata.sorted_tables:
            print(f"Tablo kopyalanıyor: {table.name}...", end="", flush=True)
            
            # Read from source
            with src_engine.connect() as src_conn:
                rows = src_conn.execute(table.select()).fetchall()
                
            if rows:
                # Insert into destination
                with dst_engine.connect() as dst_conn:
                    # Clean the table first if needed (Optional)
                    # dst_conn.execute(table.delete())
                    
                    # Insert in batches
                    dst_conn.execute(table.insert(), [dict(row._mapping) for row in rows])
                    dst_conn.commit()
                print(f" OK ({len(rows)} satır)")
            else:
                print(" Boş")

        print("\nTEBRİKLER: Veri taşıma işlemi başarıyla tamamlandı!")

if __name__ == "__main__":
    migrate()

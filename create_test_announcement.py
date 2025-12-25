from app import create_app, db
from app.models.announcement import Announcement
from datetime import datetime, timedelta

app = create_app()
with app.app_context():
    # Delete existing test ones if any
    Announcement.query.filter_by(title="Test Duyurusu").delete()
    
    ann = Announcement(
        title="Yeni Özellik Yayında!",
        content="Saatlik otomatik senkronizasyon ve multi-user desteği aktif edildi. Tüm pazaryerleri artık her saat başı güncelleniyor!",
        is_active=True,
        priority='high'
    )
    db.session.add(ann)
    db.session.commit()
    print("Test duyurusu oluşturuldu.")

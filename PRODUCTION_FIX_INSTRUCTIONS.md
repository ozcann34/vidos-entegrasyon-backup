# Production Fix Instructions

## Kritik Hatalar ve Çözümler

### 1. Database Migration: sync_logs.user_id Eksik

**Hata:**
```
column sync_logs.user_id does not exist
```

**Çözüm:**
```bash
# SSH ile sunucuya bağlan
ssh ubuntu@your-server

# PostgreSQL'e bağlan
sudo -u postgres psql vidos_db

# Migration SQL'i çalıştır
\i /var/www/vidos/migration_sync_logs_user_id.sql

# Veya manuel olarak:
ALTER TABLE sync_logs ADD COLUMN IF NOT EXISTS user_id INTEGER;
ALTER TABLE sync_logs ADD CONSTRAINT fk_sync_logs_user_id FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS ix_sync_logs_user_id ON sync_logs(user_id);

# Çık
\q
```

### 2. Worker Timeout: Excel Upload

**Hata:**
```
WORKER TIMEOUT (pid:127954)
/api/sync-exceptions/upload
```

**Çözüm:**
Kod güncellemeleri yapıldı:
- 5000 satır limiti eklendi
- Batch commit (100'lük gruplar) eklendi
- Performans optimizasyonları

**Gunicorn Timeout Ayarı (Opsiyonel):**
```bash
# /etc/systemd/system/vidos.service dosyasında
# --timeout 120 parametresi ekle

ExecStart=/var/www/vidos/venv/bin/gunicorn \
    --workers 4 \
    --timeout 120 \
    --bind unix:/var/www/vidos/vidos.sock \
    --access-logfile /var/log/vidos/access.log \
    --error-logfile /var/log/vidos/error.log \
    run:app
```

### 3. Deployment

```bash
# Sunucuda güncel kodu çek
cd /var/www/vidos
git pull origin main

# Migration'ı çalıştır (yukarıdaki adım 1)

# Gunicorn'u yeniden başlat
sudo systemctl restart vidos

# Logları kontrol et
sudo journalctl -u vidos -f
```

### 4. Doğrulama

```bash
# Sync logs API'sini test et
curl https://your-domain.com/api/auto_sync/logs

# Excel upload'ı test et (küçük dosya ile)
# Tarayıcıdan /sync-exceptions sayfasına git ve test et
```

## Notlar

- Migration otomatik değil, manuel çalıştırılmalı
- Excel upload artık max 5000 satır işliyor
- Büyük dosyalar için batch processing aktif

# Pazarama Sipariş API Sorunu ve Çözümler

## Sorun
Pazarama sipariş çekme işlemi 404 hatası veriyor:
```
404 Client Error: Not Found for url: https://isortagimapi.pazarama.com/order/getOrders
```

## Denenen Çözümler

### 1. Endpoint Değişikliği ✅ (Denendi)
**Eski:** `POST /order/getOrders`
**Yeni:** `GET /order/orders`

Pazarama API'sinde genelde REST standartlarına uygun endpoint'ler kullanılır.

### 2. Alternatif Endpoint'ler (Eğer 1. çalışmazsa)

Pazarama API'sinde sipariş listesi için kullanılabilecek alternatif endpoint'ler:

```python
# Alternatif 1: Shipment packages (kargo paketleri)
GET /shipment/packages

# Alternatif 2: Order list (farklı path)
GET /orders

# Alternatif 3: Merchant orders
GET /merchant/orders
```

## Test Etme

1. Sunucuyu yeniden başlatın: `run_server.bat`
2. Siparişler sayfasına gidin
3. "Pazarama Siparişleri Çek" butonuna tıklayın
4. Konsol loglarını kontrol edin

## Eğer Hala Çalışmazsa

### Çözüm A: API Dokümantasyonu
Pazarama iş ortağı panelinizde (https://isortagim.pazarama.com/) API dokümantasyonunu kontrol edin:
- Sol menüden "Entegrasyon" veya "API" bölümüne gidin
- Sipariş listeleme endpoint'ini bulun
- Doğru endpoint'i bana bildirin

### Çözüm B: Manuel Test
Postman veya benzeri bir araçla test edin:
1. Token alın: `POST https://isortagimgiris.pazarama.com/connect/token`
2. Farklı endpoint'leri deneyin:
   - `GET /order/orders`
   - `GET /orders`
   - `GET /shipment/packages`

### Çözüm C: Geçici Çözüm
Şimdilik Pazarama sipariş senkronizasyonunu devre dışı bırakıp sadece Trendyol kullanabilirsiniz.

## Güncel Durum

✅ Trendyol siparişleri çalışıyor (sayfalama ile tüm siparişler çekiliyor)
⏳ Pazarama endpoint'i güncellendi, test bekleniyor

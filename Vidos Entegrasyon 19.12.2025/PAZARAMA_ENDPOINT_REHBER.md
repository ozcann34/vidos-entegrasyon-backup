# Pazarama Sipariş API Endpoint Bulma Rehberi

## Sorun
Pazarama sipariş API'si 404 hatası veriyor. Doğru endpoint'i bulmamız gerekiyor.

## Adım 1: Pazarama İş Ortağı Paneline Giriş
1. https://isortagim.pazarama.com/ adresine gidin
2. Hesabınızla giriş yapın

## Adım 2: API Dokümantasyonunu Bulun
Sol menüden şunlardan birini arayın:
- "Entegrasyon"
- "API Dokümantasyonu"
- "Geliştirici Araçları"
- "API Bilgileri"

## Adım 3: Sipariş API Endpoint'ini Bulun
API dokümantasyonunda şunları arayın:
- "Sipariş Listeleme" / "Order Listing"
- "Siparişleri Getir" / "Get Orders"
- "Order List API"

**Endpoint şu şekilde olabilir:**
```
GET /order/list
GET /orders
GET /order/getOrderList
POST /order/list
GET /marketplace/orders
```

## Adım 4: Endpoint Bilgilerini Not Edin

Bulduğunuz endpoint için şunları not edin:
1. **Method:** GET mi POST mu?
2. **URL Path:** Tam endpoint yolu (örn: /order/list)
3. **Parameters:** Hangi parametreleri kabul ediyor? (page, size, status, vb.)
4. **Request Type:** Query parameters mı, JSON body mi?

## Adım 5: Bana Bildirin

Bulduğunuz bilgileri bana şu formatta bildirin:

```
Method: GET/POST
Endpoint: /order/xxxxx
Parameters:
  - page: number (query/body)
  - size: number (query/body)
  - status: string (optional)
  - startDate: string (optional)
  - endDate: string (optional)
```

## Alternatif: Pazarama Destek

Eğer dokümantasyonu bulamazsanız:
- **Email:** destek@pazarama.com
- **Konu:** "Sipariş Listeleme API Endpoint Bilgisi"
- **Mesaj:** "Merhaba, entegrasyon için sipariş listeleme API endpoint'inin tam path'ini ve kullanım detaylarını öğrenmek istiyorum."

## Geçici Çözüm

Şu anda:
- ✅ **Trendyol siparişleri çalışıyor** (tüm siparişler çekiliyor)
- ⏸️ **Pazarama siparişleri geçici olarak devre dışı** (endpoint bulunana kadar)

Trendyol siparişlerini kullanmaya devam edebilirsiniz.

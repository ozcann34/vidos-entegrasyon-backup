import difflib

# Knowledge Base Dictionary
KNOWLEDGE_BASE = {
    # Trendyol
    "trendyol ürün gönderimi": "Trendyol ürünlerini göndermek için 'Trendyol Ürünleri' sayfasına gidin, ürünleri seçin ve 'Seçili Ürünleri Gönder' butonuna tıklayın. Eksik bilgi varsa hata alabilirsiniz.",
    "trendyol stok güncelleme": "Stoklar otomatik olarak senkronize edilir. Manuel tetiklemek için 'Otomatik Senkronizasyon' sayfasına bakabilirsiniz.",
    "trendyol api hatası": "API hatası alıyorsanız Ayarlar > Trendyol API bilgilerinizin doğru olduğundan ve süresinin dolmadığından emin olun.",
    "trendyol barkod hatası": "Trendyol'da barkodlar benzersiz olmalıdır. Ürün düzenleme sayfasından barkodu kontrol edin.",
    "trendyol kategori eşleştirme": "Kategorileri eşleştirmek için ürün listesinde 'Kategori Eşleştir' butonunu kullanın.",

    # Hepsiburada
    "hepsiburada ürün gönderimi": "Hepsiburada'ya ürün göndermek için merchant ID'nizin doğru girildiğinden emin olun. Ürünler Hepsiburada Listing API üzerinden iletilir.",
    "hepsiburada 403 hatası": "403 hatası genellikle yetki sorunudur. API anahtarlarınızı kontrol edin.",

    # Pazarama & Idefix
    "pazarama ürün gönderimi": "Pazarama'ya ürün göndermek için Ayarlar > Pazarama API bilgilerinizin (E-posta, Şifre) doğru olduğunu kontrol edin.",
    "idefix ürün gönderimi": "Idefix entegrasyonu için API bilgilerini girdikten sonra XML ürünlerini seçerek gönderim yapabilirsiniz.",

    # XML / Excel
    "xml yükleme": "XML linki eklemek için 'XML Ürünleri' sayfasına gidin ve 'Yeni XML Ekle' butonunu kullanın.",
    "excel ürün yükleme": "Excel ile ürün yüklemek için şablonu indirin, doldurun ve 'Excel Ürünleri' sayfasından yükleyin.",
    "stok 0": "Stok 0 olan ürünleri satışa kapatmak veya 1 olarak göndermek için Ayarlar sayfasını kontrol edebilirsiniz.",

    # New Features
    "yasaklı liste": "Yasaklı Liste (Blacklist), belirli marka, kategori veya kelimeleri içeren ürünlerin pazar yerlerine gönderilmesini engeller. Ayarlar > Yasaklı Liste menüsünden yönetebilirsiniz.",
    "kara liste": "Kara Liste özelliği ile istemediğiniz markaları veya kelimeleri engelleyerek satış listenizi kontrol altında tutabilirsiniz.",
    "kdv oranı": "Ürün ekleme veya düzenleme sayfasında KDV oranını manuel seçebilirsiniz. Ayrıca XML'den gelen verilerde varsayılan KDV oranı kullanılır.",
    "fiyat toggle": "Landing sayfasındaki fiyatlar aylık veya yıllık (indirimli) olarak görüntülenebilir. Yıllık alımlarda %20 indirim uygulanır.",

    # Orders / Invoice
    "fatura kesme": "Sipariş detay sayfasında 'Fatura Oluştur' butonunu kullanabilirsiniz. Henüz entegrasyon tamamlanmadıysa manuel yükleyebilirsiniz.",
    "kargo etiketi": "Kargo etiketi oluşturmak için siparişi 'Hazırlanıyor' statüsüne getirin.",
    "sipariş iptali": "Siparişi iptal etmek için Trendyol panelinizden işlem yapmanız gerekebilir, buradan sadece statü güncelleyebilirsiniz.",

    # General
    "şifremi unuttum": "Giriş sayfasındaki 'Şifremi Unuttum' linkine tıklayarak sıfırlama maili alabilirsiniz.",
    "kullanıcı ekleme": "Sadece Admin yetkisi olanlar kullanıcı ekleyebilir.",
    "destek talebi": "Destek talebi oluşturmak için 'İşlemler > Destek Taleplerim' menüsünü kullanın.",
    "iletişim": "Bizimle destek talebi üzerinden iletişime geçebilirsiniz."
}

def get_chatbot_response(user_input):
    """
    Get response using fuzzy matching from knowledge base.
    """
    if not user_input:
        return "Lütfen bir soru sorun."

    user_input = user_input.lower()
    
    # 1. Direct Keyword Check (Priority)
    if "merhaba" in user_input or "selam" in user_input:
        return "Merhaba! Size nasıl yardımcı olabilirim?"
    
    # 2. Fuzzy Matching with Keys
    keys = list(KNOWLEDGE_BASE.keys())
    # find best match with cutoff 0.4 (loose match)
    matches = difflib.get_close_matches(user_input, keys, n=1, cutoff=0.4)
    
    if matches:
        best_match = matches[0]
        return KNOWLEDGE_BASE[best_match]
    
    # 3. Check if any key is substantially contained in input
    for key in keys:
        if key in user_input:
             return KNOWLEDGE_BASE[key]

    # Fallback
    return "Bu konuda tam bir bilgim yok. Dilerseniz 'Destek Talebi Oluştur' butonunu kullanarak uzman ekibimize sorabilirsiniz."

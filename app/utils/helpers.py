from typing import Iterable, List, Any, Optional
import requests
from app.models import Setting

def get_marketplace_multiplier(marketplace: str, user_id: Optional[int] = None):
    """Get price multiplier for a marketplace. Uses current_user if user_id not provided."""
    # Deprecated: Use calculate_price instead for full logic
    mult_key_map = {
        'trendyol': 'PRICE_MULTIPLIER',
        'hepsiburada': 'HB_PRICE_MULTIPLIER',
        'pazarama': 'PAZARAMA_PRICE_MULTIPLIER',
        'idefix': 'IDEFIX_PRICE_MULTIPLIER',
        'n11': 'N11_PRICE_MULTIPLIER',
    }
    skey = mult_key_map.get(marketplace, 'PRICE_MULTIPLIER')
    
    if user_id is None:
        try:
            from flask_login import current_user
            if current_user and current_user.is_authenticated:
                user_id = current_user.id
        except Exception:
            pass
    
    try:
        mp_multiplier = float(Setting.get(skey, '1.0', user_id=user_id) or '1.0')
        if mp_multiplier <= 0:
            mp_multiplier = 1.0
    except Exception:
        mp_multiplier = 1.0
    return mp_multiplier

def calculate_price(base_price: float, marketplace: str, user_id: Optional[int] = None, multiplier_override: Optional[float] = None, return_details: bool = False) -> Any:
    """
    Calculate final price based on GLOBAL tiered price rules.
    Looks up rules in GLOBAL_PRICE_RULES setting (JSON) which applies to ALL marketplaces.
    If no rules found or no matching range, returns the base price unchanged.
    
    If return_details is True, returns (price, rule_description)
    """
    if base_price <= 0:
        return (0.0, "Geçersiz fiyat") if return_details else 0.0

    # Ensure user_id
    if user_id is None:
        try:
            from flask_login import current_user
            if current_user and current_user.is_authenticated:
                user_id = current_user.id
        except Exception:
            pass

    import json
    
    # 1. Try GLOBAL Price Rules (applies to ALL marketplaces)
    rules_json = Setting.get("GLOBAL_PRICE_RULES", "", user_id=user_id)
    if rules_json:
        try:
            rules = json.loads(rules_json)
            # Find the rule that matches the base_price range
            for rule in rules:
                rmin = float(rule.get('min', 0))
                rmax = float(rule.get('max', 99999999))
                if rmin <= base_price < rmax:
                    percent = float(rule.get('percent', 0))
                    fixed_on_top = float(rule.get('fixed', 0))
                    price = base_price * (1 + (percent / 100.0)) + fixed_on_top
                    
                    desc = ""
                    if percent > 0: desc += f"%{percent}"
                    if fixed_on_top > 0: desc += f"{' + ' if desc else ''}{fixed_on_top} TL"
                    if not desc: desc = "Kural eşleşti (değişim yok)"
                    desc += f" ({rmin}-{rmax} TL arası)"
                    
                    final_p = round(price, 2)
                    return (final_p, desc) if return_details else final_p
        except Exception as e:
            import logging
            logging.error(f"Error calculating price with GLOBAL rules: {e}")

    # 2. FALLBACK: No rules
    final_p = round(base_price, 2)
    return (final_p, "Fiyat kuralı yok") if return_details else final_p


def chunked(iterable: Iterable[Any], size: int) -> Iterable[List[Any]]:
    chunk: List[Any] = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk

def to_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        try:
            return int(float(str(value).replace(',', '.')))
        except Exception:
            return default

def to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except Exception:
        try:
            return float(str(value).replace(',', '.'))
        except Exception:
            return default

def fetch_xml_from_url(url: str):
    """Verilen URL'den XML içeriğini çeker."""
    try:
        if url.startswith('local:'):
            import os
            # local:file.xml -> reads from xml_uploads/file.xml
            filename = url.split('local:', 1)[1]
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            file_path = os.path.join(base_dir, 'xml_uploads', filename)
            
            if not os.path.exists(file_path):
                raise FileNotFoundError(f"Yerel XML dosyası bulunamadı: {filename}")
                
            with open(file_path, 'rb') as f:
                return f.read()

        headers = {'User-Agent': 'SOPYO-Integration-Client/1.0'}
        response = requests.get(url, headers=headers, timeout=120)
        
        # HTTP Hata kontrolü
        response.raise_for_status()
        
        # İçerik türü kontrolü (Genişletildi)
        content_type = response.headers.get('Content-Type', '').lower()
        if not any(t in content_type for t in ['xml', 'text', 'octet-stream', 'rss']):
             # Bazı sunucular yanlış content-type dönebilir, yine de uyaralım ama engellemeyelim
             pass

        return response.content
        
    except FileNotFoundError as e:
        raise Exception(str(e))
    except requests.exceptions.RequestException as e:
        raise Exception(f"URL'den XML çekilirken ağ hatası oluştu: {e}")
    except ValueError as e:
        raise Exception(f"XML içeriği doğrulama hatası: {e}")
    except Exception as e:
        raise Exception(f"Beklenmedik bir hata oluştu: {e}")


def clean_forbidden_words(text: str, user_id: Optional[int] = None) -> str:
    """
    Remove forbidden words from text based on FORBIDDEN_KEYWORDS setting.
    
    Args:
        text: The text to clean (product title, description, etc.)
        user_id: User ID to get user-specific settings
        
    Returns:
        Cleaned text with forbidden words removed
    """
    if not text:
        return text
    
    # Get forbidden keywords from settings
    if user_id is None:
        try:
            from flask_login import current_user
            if current_user and current_user.is_authenticated:
                user_id = current_user.id
        except Exception:
            pass
    
    forbidden_str = Setting.get("FORBIDDEN_KEYWORDS", "", user_id=user_id) or ""
    if not forbidden_str.strip():
        return text
    
    # Split by comma and clean each keyword
    forbidden_words = [w.strip().lower() for w in forbidden_str.split(",") if w.strip()]
    
    if not forbidden_words:
        return text
    
    import re
    result = text
    for word in forbidden_words:
        if word:
            # Case-insensitive replacement
            pattern = re.compile(re.escape(word), re.IGNORECASE)
            result = pattern.sub("", result)
    
    # Clean up multiple spaces
    result = re.sub(r'\s+', ' ', result).strip()
    
    return result

def is_product_forbidden(user_id: int, title: str = "", brand: str = "", category: str = "") -> Optional[str]:
    """
    Check if a product is in the forbidden list.
    
    Returns:
        The reason (value/type) if forbidden, None otherwise.
    """
    from app.models import Blacklist
    
    # Get all blacklist items for this user
    items = Blacklist.query.filter_by(user_id=user_id).all()
    if not items:
        return None
    
    title_low = (title or "").lower()
    brand_low = (brand or "").lower()
    category_low = (category or "").lower()
    
    for item in items:
        val_low = item.value.lower()
        if item.type == 'brand':
            if val_low == brand_low:
                return f"Yasaklı Marka: {item.value}"
        elif item.type == 'category':
            if val_low in category_low: # Category usually contains breadcrumbs
                return f"Yasaklı Kategori: {item.value}"
        elif item.type == 'word':
            if val_low in title_low or val_low in brand_low or val_low in category_low:
                return f"Yasaklı Kelime: {item.value}"
                
    return None

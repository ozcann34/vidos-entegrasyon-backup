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

def calculate_price(base_price: float, marketplace: str, user_id: Optional[int] = None, multiplier_override: Optional[float] = None) -> float:
    """
    Calculate final price based on tiered price rules (ranges).
    New logic: Looks up rules in [MARKETPLACE]_PRICE_RULES setting (JSON).
    If no rules found, falls back to legacy Multiplier/Percentage system.
    """
    if base_price <= 0:
        return 0.0

    # Ensure user_id
    if user_id is None:
        try:
            from flask_login import current_user
            if current_user and current_user.is_authenticated:
                user_id = current_user.id
        except Exception:
            pass

    import json
    # 1. Try NEW Tiered Price Rules
    rules_key_map = {
        'trendyol': 'TRENDYOL_PRICE_RULES',
        'hepsiburada': 'HB_PRICE_RULES',
        'pazarama': 'PAZARAMA_PRICE_RULES',
        'idefix': 'IDEFIX_PRICE_RULES',
        'n11': 'N11_PRICE_RULES',
    }
    
    rules_key = rules_key_map.get(marketplace)
    if rules_key:
        rules_json = Setting.get(rules_key, "", user_id=user_id)
        if rules_json:
            try:
                rules = json.loads(rules_json)
                # Rules format: [{"min": 0, "max": 100, "percent": 30}, ...]
                applied_rule = None
                for rule in rules:
                    rmin = float(rule.get('min', 0))
                    rmax = float(rule.get('max', 99999999))
                    if rmin <= base_price < rmax:
                        applied_rule = rule
                        break
                
                if applied_rule:
                    percent = float(applied_rule.get('percent', 0))
                    price = base_price * (1 + (percent / 100.0))
                    # Fixed amount can still be added on top if defined in rule
                    fixed_on_top = float(applied_rule.get('fixed', 0))
                    return round(price + fixed_on_top, 2)
            except Exception as e:
                import logging
                logging.error(f"Error calculating price with rules for {marketplace}: {e}")

    # 2. FALLBACK to Legacy Multiplier/Percentage system
    # (Existing logic maintained for backward compatibility or if no rules defined)
    
    # Start with Multiplier
    if multiplier_override is not None and multiplier_override > 0:
        multiplier = multiplier_override
    else:
        multiplier = get_marketplace_multiplier(marketplace, user_id)
    
    price = base_price * multiplier

    # Percentage
    perc_key_map = {
        'trendyol': 'PRICE_PERCENTAGE',
        'hepsiburada': 'HB_PRICE_PERCENTAGE',
        'pazarama': 'PAZARAMA_PRICE_PERCENTAGE',
        'idefix': 'IDEFIX_PRICE_PERCENTAGE',
        'n11': 'N11_PRICE_PERCENTAGE',
    }
    # Fixed Amount
    fixed_key_map = {
        'trendyol': 'PRICE_FIXED',
        'hepsiburada': 'HB_PRICE_FIXED',
        'pazarama': 'PAZARAMA_PRICE_FIXED',
        'idefix': 'IDEFIX_PRICE_FIXED',
        'n11': 'N11_PRICE_FIXED',
    }

    try:
        pkey = perc_key_map.get(marketplace)
        if pkey:
            perc_val = float(Setting.get(pkey, '0', user_id=user_id) or '0')
            if perc_val != 0:
                price = price * (1 + (perc_val / 100.0))
    except Exception: pass

    try:
        fkey = fixed_key_map.get(marketplace)
        if fkey:
            fixed_val = float(Setting.get(fkey, '0', user_id=user_id) or '0')
            if fixed_val != 0:
                price = price + fixed_val
    except Exception: pass
    
    return round(price, 2)


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

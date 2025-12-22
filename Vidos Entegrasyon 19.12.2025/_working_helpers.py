from typing import Iterable, List, Any, Optional
import requests
from app.models import Setting

def get_marketplace_multiplier(marketplace: str, user_id: Optional[int] = None):
    """Get price multiplier for a marketplace. Uses current_user if user_id not provided."""
    mult_key_map = {
        'trendyol': 'PRICE_MULTIPLIER',
        'hepsiburada': 'HB_PRICE_MULTIPLIER',
        'pazarama': 'PAZARAMA_PRICE_MULTIPLIER',
        'idefix': 'IDEFIX_PRICE_MULTIPLIER',
    }
    skey = mult_key_map.get(marketplace, 'PRICE_MULTIPLIER')
    
    # If no user_id provided, try to get from current_user
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
        headers = {'User-Agent': 'SOPYO-Integration-Client/1.0'}
        response = requests.get(url, headers=headers, timeout=60)
        
        # HTTP Hata kontrolü
        response.raise_for_status()
        
        # İçerik türü kontrolü (Genişletildi)
        content_type = response.headers.get('Content-Type', '').lower()
        if not any(t in content_type for t in ['xml', 'text', 'octet-stream']):
             raise ValueError(f"URL geçerli bir XML içeriği sağlamıyor. Content-Type: {content_type}")

        return response.content
        
    except requests.exceptions.RequestException as e:
        raise Exception(f"URL'den XML çekilirken ağ hatası oluştu: {e}")
    except ValueError as e:
        raise Exception(f"XML içeriği doğrulama hatası: {e}")
    except Exception as e:
        raise Exception(f"Beklenmedik bir hata oluştu: {e}")

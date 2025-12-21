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
        'n11': 'N11_PRICE_MULTIPLIER',
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
        response = requests.get(url, headers=headers, timeout=60)
        
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

def sync_product_to_local(user_id: int, barcode: str, product_data: dict, xml_source_id: Any = None):
    """
    Creates or updates a local Product record from XML or Marketplace data.
    Centralized to ensure consistent behavior across all services (Trendyol, Pazarama, etc).
    """
    from app import db
    from app.models import Product
    import json
    
    if not user_id or not barcode:
        return None
        
    try:
        local_p = Product.query.filter_by(user_id=user_id, barcode=barcode).first()
        
        # Extract fields with safe defaults
        title = product_data.get('title') or ""
        stock = to_int(product_data.get('quantity') or product_data.get('stock'), 0)
        # We store the base price (original XML price) in listPrice
        price = to_float(product_data.get('price'), 0.0)
        cost = to_float(product_data.get('cost'), 0.0)
        images = product_data.get('images', [])
        stock_code = product_data.get('stock_code') or product_data.get('stockCode') or barcode
        
        # Convert images to JSON if they are a list
        images_json = None
        if images:
            if isinstance(images, list):
                # Normalize image list if it contains dicts from marketplace format
                normalized_images = []
                for img in images:
                    if isinstance(img, dict):
                        normalized_images.append(img.get('url') or img.get('imageurl'))
                    else:
                        normalized_images.append(str(img))
                images_json = json.dumps(normalized_images)
            elif isinstance(images, str):
                images_json = images

        # Handle XML Source ID
        valid_xml_id = None
        if xml_source_id:
            try:
                valid_xml_id = int(xml_source_id)
            except:
                pass

        if local_p:
            # Update existing
            if title: local_p.title = title
            local_p.listPrice = price
            local_p.quantity = stock
            local_p.stockCode = stock_code
            if cost > 0: local_p.cost_price = cost
            if valid_xml_id: local_p.xml_source_id = valid_xml_id
            if images_json and not local_p.images_json: # Only update if empty to avoid overwriting user changes?
                local_p.images_json = images_json
        else:
            # Create new
            local_p = Product(
                user_id=user_id,
                barcode=barcode,
                title=title,
                listPrice=price,
                quantity=stock,
                stockCode=stock_code,
                cost_price=cost,
                images_json=images_json,
                xml_source_id=valid_xml_id
            )
            db.session.add(local_p)
            
        return local_p
    except Exception as e:
        import logging
        logging.error(f"Error in sync_product_to_local for {barcode}: {e}")
        return None

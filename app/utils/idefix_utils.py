"""
Idefix integration utilities.

This module provides helper functions for working with the Idefix API.
"""
import logging
from typing import Dict, List, Any, Optional, Callable

from app.services.idefix_service import IdefixClient
from app.models import Setting

logger = logging.getLogger(__name__)

def get_idefix_client(user_id: Optional[int] = None) -> Optional[IdefixClient]:
    """
    Veritabanındaki ayarları kullanarak Idefix istemci örneği oluşturur.
    
    Returns:
        Yapılandırılmışsa IdefixClient örneği, aksi takdirde None
    """
    from flask_login import current_user
    
    # Priority: passed user_id > current_user
    actual_user_id = user_id
    if actual_user_id is None:
        try:
            if current_user and current_user.is_authenticated:
                actual_user_id = current_user.id
        except Exception:
            pass
            
    if actual_user_id is None:
        logger.warning("Idefix istemcisi oluşturulamıyor: Kullanıcı kimliği belirsiz")
        return None

    try:
        # Veritabanından ayarları al
        api_key = Setting.get('IDEFIX_API_KEY', '').strip()
        vendor_id = Setting.get('IDEFIX_VENDOR_ID', '').strip()
        api_secret = Setting.get('IDEFIX_API_SECRET', '').strip()
        
        # Eğer Setting.get user_id parametresini destekliyorsa onu kullanmalıyız.
        # Setting modelini kontrol ettim, destekliyor.
        api_key = Setting.get('IDEFIX_API_KEY', '', user_id=actual_user_id).strip()
        vendor_id = Setting.get('IDEFIX_VENDOR_ID', '', user_id=actual_user_id).strip()
        api_secret = Setting.get('IDEFIX_API_SECRET', '', user_id=actual_user_id).strip()
        is_test = Setting.get('IDEFIX_IS_TEST', '0', user_id=actual_user_id) == '1'
        
        # Gerekli ayarların kontrolü
        if not api_key or not vendor_id:
            logger.warning(f"Idefix API anahtarı veya Satıcı ID eksik (Kullanıcı: {actual_user_id})")
            return None
            
        logger.info(f"Idefix istemcisi oluşturuluyor - Kullanıcı: {actual_user_id}, Satıcı ID: {vendor_id}, Test Modu: {is_test}")
        
        # İstemciyi oluştur
        client = IdefixClient(
            api_key=api_key,
            api_secret=api_secret, # Note: IdefixClient in idefix_service takes api_secret too
            vendor_id=vendor_id
        )
        # Note: IdefixClient in idefix_service doesn't seem to take is_test in __init__ currently
        # If it's used in utils, we might need to adjust or ignore for now.
        
        return client
        
    except Exception as e:
        logger.error(f"Idefix istemcisi oluşturulurken hata: {str(e)}", exc_info=True)
        return None


def update_product_inventory(
    barcode: str,
    price: float,
    stock: int,
    compare_price: Optional[float] = None,
    delivery_duration: int = 1,
    delivery_type: str = "regular"
) -> Dict[str, Any]:
    """
    Update inventory and price for a single product.
    
    Args:
        barcode: Product barcode
        price: Price in TL
        stock: Available stock quantity
        compare_price: Original price (for showing discount)
        delivery_duration: Delivery duration in days
        delivery_type: "regular" or "fast"
        
    Returns:
        API response as dict
    """
    client = get_idefix_client()
    if not client:
        return {"status": "error", "message": "Idefix client not configured"}
        
    item = {
        "barcode": str(barcode),
        "price": price,
        "inventoryQuantity": int(stock),
        "deliveryDuration": delivery_duration,
        "deliveryType": delivery_type
    }
    
    if compare_price is not None:
        item["comparePrice"] = compare_price
    
    return client.update_inventory_and_price([item])


def batch_update_inventory(
    items: List[Dict[str, Any]],
    batch_callback: Optional[Callable[[str, Dict], None]] = None
) -> Dict[str, Any]:
    """
    Update inventory and prices for multiple products in a batch.
    
    Args:
        items: List of product items with inventory and price data
        batch_callback: Optional callback function to handle batch response
        
    Returns:
        API response as dict
        
    Example items format:
    [
        {
            "barcode": "1234567890123",
            "price": 100.0,  # Price in TL
            "stock": 10,     # Available quantity
            "compare_price": 120.0  # Optional original price
        }
    ]
    """
    client = get_idefix_client()
    if not client:
        return {"status": "error", "message": "Idefix client not configured"}
    
    # Convert to Idefix API format
    formatted_items = []
    for item in items:
        formatted_item = {
            "barcode": str(item.get('barcode', '')),
            "price": float(item.get('price', 0)),
            "inventoryQuantity": int(item.get('stock', 0)),
            "deliveryDuration": int(item.get('delivery_duration', 1)),
            "deliveryType": str(item.get('delivery_type', 'regular'))
        }
        
        if 'compare_price' in item and item['compare_price'] is not None:
            formatted_item["comparePrice"] = float(item['compare_price'])
            
        formatted_items.append(formatted_item)
    
    return client.update_inventory_and_price(formatted_items, batch_callback)


def get_inventory_status(batch_request_id: str) -> Dict[str, Any]:
    """
    Get the status of an inventory update batch.
    
    Args:
        batch_request_id: The batch ID from update_inventory_and_price response
        
    Returns:
        Dict containing the batch status and item results
    """
    client = get_idefix_client()
    if not client:
        return {"status": "error", "message": "Idefix client not configured"}
        
    return client.get_inventory_status(batch_request_id)


def get_products(page: int = 0, size: int = 50) -> Dict[str, Any]:
    """
    Get list of products from Idefix.
    
    Args:
        page: Page number (0-based)
        size: Number of items per page (max 100)
        
    Returns:
        Dict containing product list and pagination info
    """
    client = get_idefix_client()
    if not client:
        return {"status": "error", "message": "Idefix client not configured", "items": [], "total": 0}
        
    return client.get_products(page=page, size=size)


def is_configured() -> bool:
    """
    Idefix entegrasyonunun doğru yapılandırılıp yapılandırılmadığını kontrol eder.
    
    Returns:
        bool: Tüm gerekli ayarlar mevcutsa True, aksi halde False
    """
    try:
        from flask_login import current_user
        actual_user_id = None
        if current_user and current_user.is_authenticated:
            actual_user_id = current_user.id
            
        api_key = Setting.get('IDEFIX_API_KEY', '', user_id=actual_user_id).strip()
        vendor_id = Setting.get('IDEFIX_VENDOR_ID', '', user_id=actual_user_id).strip()
        
        # Hem boş olmamalı hem de sadece boşluk karakterlerinden oluşmamalı
        is_valid = bool(api_key and vendor_id and 
                       not api_key.isspace() and 
                       not vendor_id.isspace())
        
        if not is_valid:
            logger.warning(f"Idefix için gerekli ayarlar eksik veya geçersiz (Kullanıcı: {actual_user_id})")
            
        return is_valid
        
    except Exception as e:
        logger.error(f"Idefix yapılandırma kontrolü sırasında hata: {str(e)}")
        return False


import logging
import time
from typing import List, Dict, Any, Optional

from app.models import Setting, SupplierXML
from app.services.hepsiburada_client import HepsiburadaClient
from app.services.xml_service import load_xml_source_index
from app.services.job_queue import append_mp_job_log, update_mp_job, get_mp_job
from app.utils.helpers import get_marketplace_multiplier, to_float, to_int, is_product_forbidden

def get_hepsiburada_client(user_id: int = None) -> HepsiburadaClient:
    """Factory to get authenticated client."""
    if user_id is None:
        from flask_login import current_user
        if current_user and current_user.is_authenticated:
            user_id = current_user.id
            
    merchant_id = Setting.get("HB_MERCHANT_ID", "", user_id=user_id)
    service_key = Setting.get("HB_SERVICE_KEY", "", user_id=user_id)
    
    if not merchant_id or not service_key:
        raise ValueError("Hepsiburada Merchant ID veya Servis Anahtarı eksik. Ayarlar sayfasından giriniz.")
        
    return HepsiburadaClient(merchant_id.strip(), service_key.strip())

def perform_hepsiburada_send_products(job_id: str, barcodes: List[str], xml_source_id: Any, **kwargs) -> Dict[str, Any]:
    """
    Send selected products from XML to Hepsiburada.
    """
    append_mp_job_log(job_id, "Hepsiburada gönderim işlemi başlatılıyor...")
    
    # Resolve User ID from XML Source
    user_id = None
    if xml_source_id:
        try:
            s_id = str(xml_source_id)
            if s_id.isdigit():
                src = SupplierXML.query.get(int(s_id))
                if src: user_id = src.user_id
        except Exception as e:
            logging.warning(f"Failed to resolve user_id: {e}")
    
    try:
        client = get_hepsiburada_client()
    except Exception as e:
        return {'success': False, 'message': str(e)}

    # Load Source
    xml_index = load_xml_source_index(xml_source_id)
    mp_map = xml_index.get('by_barcode') or {}
    
    # Options from kwargs or defaults
    multiplier = to_float(kwargs.get('price_multiplier', get_marketplace_multiplier('hepsiburada')))
    default_price = to_float(kwargs.get('default_price', 0))
    title_prefix = kwargs.get('title_prefix', '')
    skip_no_image = kwargs.get('skip_no_image', False)
    skip_no_barcode = kwargs.get('skip_no_barcode', False)
    zero_stock_as_one = kwargs.get('zero_stock_as_one', False)
    
    products_to_send = []
    skipped = []
    
    processed_count = 0
    total_count = len(barcodes)
    
    for barcode in barcodes:
        processed_count += 1
        
        # Check Cancel
        job = get_mp_job(job_id)
        if job and job.get('cancel_requested'):
            append_mp_job_log(job_id, "İşlem iptal edildi.", level='warning')
            break
            
        product = mp_map.get(barcode)
        if not product:
            skipped.append({'barcode': barcode, 'reason': 'XML verisi bulunamadı'})
            continue
            
        # Blacklist check
        forbidden_reason = is_product_forbidden(user_id, title=product.get('title'), brand=product.get('brand'), category=product.get('category'))
        if forbidden_reason:
            skipped.append({'barcode': barcode, 'reason': f"Yasaklı Liste: {forbidden_reason}"})
            continue

        # Basic Mapping
        # Hepsiburada requires: MerchantSku, ProductName, Price, Stock, etc.
        # Actually for 'Catalog' integration it is complex, but for 'Listing' (Inventory) 
        # it typically matches via Barcode or MerchantSku.
        # Required fields for Inventory Upload often:
        # - merchantSku (we use barcode or stock code)
        # - price
        # - availableStock
        # - dispatchTime (handling time)
        # - cargoCompany
        
        # Assuming we are using the "Listing API" which connects to existing catalog products via barcode?
        # OR if we are creating new products (Catalog integration)? 
        # User said "xmlden ürün gönderilebilir olsun" -> implies listing or full creation.
        # Usually full creation requires a lot of attributes. Listing is safer first step.
        # Let's try to map what we can for a "Listing" (Match & Publish) payload.
        # Hepsiburada Listing API payload format (list of object):
        # {
        #   "merchantSku": "...",
        #   "productName": "...", (Optional if matching)
        #   "price": { "amount": 100.0, "currency": "TRY" },
        #   "availableStock": 10,
        #   "dispatchTime": 3,
        #   "cargoCompany1": "Aras Kargo",
        #   ...
        # }
        
        title = (product.get('title') or '')
        if title_prefix:
            title = f"{title_prefix}{title}"

        start_price = to_float(product.get('price')) or 0
        if start_price <= 0 and default_price > 0:
            start_price = default_price
            append_mp_job_log(job_id, f"Varsayılan fiyat uygulandı: {barcode}")

        final_price = round(start_price * multiplier, 2)
        stock = to_int(product.get('quantity'))

        if stock <= 0 and zero_stock_as_one:
            stock = 1
            append_mp_job_log(job_id, f"Stok 0→1 uygulandı: {barcode}")
        
        if final_price <= 0:
            skipped.append({'barcode': barcode, 'reason': 'Fiyat 0'})
            continue
            
        # Simplified Catalog Import Schema (based on common practices for Hepsiburada)
        # Hepsiburada usually matches via "merchantSku" or "ean".
        # This payload attempts to Create/Update listing.
        
        if final_price <= 0:
            skipped.append({'barcode': barcode, 'reason': 'Fiyat 0'})
            continue
            
        # Switch back to Listing API (Inventory Uploads) as Import API gives 403
        # Payload for Inventory Uploads (Listing API)
        # reference: https://developers.hepsiburada.com/hepsiburada/reference/inventory-uploads
        
        item = {
            "merchantSku": barcode,
            "productName": title[:200], # Added title if needed
            "price": {
                "amount": final_price,
                "currency": "TRY"
            },
            "availableStock": stock,
            "dispatchTime": 3,
            "cargoCompany1": "Yurtiçi Kargo"
        }
        
        products_to_send.append(item)
        
    if not products_to_send:
        return {'success': False, 'message': 'Gönderilecek geçerli ürün yok.', 'skipped': skipped}
        
    append_mp_job_log(job_id, f"{len(products_to_send)} ürün hazırlanarak Hepsiburada'ya gönderiliyor (Listing API)...")
    
    # Send
    try:
        # Submit via Listing API using the fixed Auth method in client
        result = client.upload_products(products_to_send)
        track_id = result.get('id') or result.get('trackingId')
        
        append_mp_job_log(job_id, f"Gönderim başarılı. Takip ID: {track_id}")
        
        return {
            'success': True,
            'count': len(products_to_send),
            'tracking_id': track_id,
            'skipped': skipped,
            'message': f"{len(products_to_send)} ürün Hepsiburada'ya iletildi. (Takip ID: {track_id})"
        }
        
    except Exception as e:
        msg = f"Hepsiburada API hatası: {str(e)}"
        append_mp_job_log(job_id, msg, level='error')
        return {'success': False, 'message': msg}

def perform_hepsiburada_send_all(job_id: str, xml_source_id: Any, **kwargs) -> Dict[str, Any]:
    """Send ALL products from XML source to Hepsiburada"""
    append_mp_job_log(job_id, "Tüm ürünler hazırlanıyor...")
    
    xml_index = load_xml_source_index(xml_source_id)
    mp_map = xml_index.get('by_barcode') or {}
    all_barcodes = list(mp_map.keys())
    
    if not all_barcodes:
        return {'success': False, 'message': 'XML kaynağında ürün bulunamadı.', 'count': 0}
    
    append_mp_job_log(job_id, f"Toplam {len(all_barcodes)} ürün bulundu. Gönderim başlıyor...")
    
    return perform_hepsiburada_send_products(job_id, all_barcodes, xml_source_id, **kwargs)

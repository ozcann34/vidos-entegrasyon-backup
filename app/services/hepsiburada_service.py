
import logging
import time
from typing import List, Dict, Any, Optional

from app.models import Setting, SupplierXML, BrandMapping
from app.services.hepsiburada_client import HepsiburadaClient
from app.services.xml_service import load_xml_source_index
from app.services.job_queue import append_mp_job_log, update_mp_job, get_mp_job
from app.utils.helpers import get_marketplace_multiplier, to_float, to_int
def resolve_hepsiburada_brand(brand_name: str, client: HepsiburadaClient = None, category_id: int = 0, user_id: int = None) -> str:
    # 1. Check database mapping - USER ISOLATED
    from flask_login import current_user
    u_id = user_id or (current_user.id if current_user and current_user.is_authenticated else None)
    
    mapping = BrandMapping.query.filter_by(
        user_id=u_id,
        source_brand=brand_name, 
        marketplace='hepsiburada'
    ).first()
    if mapping and mapping.target_brand_name:
        logging.info(f"HB Brand found in mapping: '{brand_name}' -> '{mapping.target_brand_name}'")
        return mapping.target_brand_name

    # 2. Try API search if client is provided
    if client:
        try:
            # First try global search
            results = client.search_brands(brand_name)
            items = results.get('data', []) if isinstance(results, dict) else []
            
            # If nothing found globally and we have a category, try category brands
            if not items and category_id:
                cat_results = client.get_brands_by_category(category_id)
                items = cat_results.get('data', []) if isinstance(cat_results, dict) else []
            
            if items:
                # Find exact match or closest match
                from difflib import get_close_matches
                names = [i.get('name') for i in items if i.get('name')]
                
                match_name = None
                # Exact case-insensitive match
                for n in names:
                    if n.lower() == brand_name.lower():
                        match_name = n
                        break
                
                # Fuzzy match if no exact match
                if not match_name:
                    matches = get_close_matches(brand_name, names, n=1, cutoff=0.6)
                    if matches:
                        match_name = matches[0]
                
                # If still no match but we have results, use first as last resort if it looks similar
                if not match_name and items:
                    match_name = items[0].get('name')

                if match_name:
                    logging.info(f"HB Brand resolved: '{brand_name}' -> '{match_name}'")
                    # Auto-save mapping for future use
                    from app import db
                    try:
                        new_mapping = BrandMapping(
                            user_id=u_id,
                            source_brand=brand_name,
                            marketplace='hepsiburada',
                            target_brand_id=0,
                            target_brand_name=match_name
                        )
                        db.session.add(new_mapping)
                        db.session.commit()
                        return match_name
                    except Exception:
                        db.session.rollback()
                    
                    return match_name
        except Exception as e:
            logging.warning(f"HB Brand resolve error for {brand_name}: {e}")

    return brand_name

def get_hepsiburada_client(user_id: int = None) -> HepsiburadaClient:
    """Factory to get authenticated client."""
    if user_id is None:
        from flask_login import current_user
        if current_user and current_user.is_authenticated:
            user_id = current_user.id
            
    merchant_id = Setting.get("HB_MERCHANT_ID", "", user_id=user_id)
    service_key = Setting.get("HB_SERVICE_KEY", "", user_id=user_id)
    api_username = Setting.get("HB_API_USERNAME", "", user_id=user_id)
    test_mode = Setting.get("HB_TEST_MODE", "off", user_id=user_id) == 'on'
    
    if not merchant_id or not service_key:
        raise ValueError("Hepsiburada Merchant ID veya Servis Anahtarı eksik. Ayarlar sayfasından giriniz.")
        
    return HepsiburadaClient(merchant_id.strip(), service_key.strip(), api_username=api_username.strip(), test_mode=test_mode)

def perform_hepsiburada_send_products(job_id: str, barcodes: List[str], xml_source_id: Any = None, title_prefix: str = None, auto_match: bool = False, match_by: str = 'barcode', user_id: int = None, is_manual: bool = False) -> Dict[str, Any]:
    """
    Send selected products from XML or Manual DB to Hepsiburada.
    """
    append_mp_job_log(job_id, "Hepsiburada gönderim işlemi başlatılıyor...")
    
    # Resolve user_id if not provided
    if user_id is None:
        try:
            from app.services.job_queue import get_mp_job
            job_data = get_mp_job(job_id)
            user_id = job_data.get('params', {}).get('_user_id')
        except Exception:
            pass
    
    try:
        client = get_hepsiburada_client(user_id=user_id)
    except Exception as e:
        return {'success': False, 'message': str(e)}

    # Load Source
    mp_map = {}
    if is_manual:
        append_mp_job_log(job_id, "Manuel ürün gönderimi aktif, veritabanından okunuyor...")
        from app.models.product import Product
        prods = Product.query.filter(Product.barcode.in_(barcodes), Product.user_id == user_id).all()
        for p in prods:
            mp_map[p.barcode] = {
                'barcode': p.barcode,
                'title': p.title,
                'description': p.description,
                'price': p.listPrice,
                'quantity': p.quantity,
                'stockCode': p.stockCode,
                'brand': p.brand,
                'categoryId': p.marketplace_category_id,
                'images': p.get_images,
                'marketplace_attributes_json': p.marketplace_attributes_json,
                'is_manual': True
            }
    else:
        xml_index = load_xml_source_index(xml_source_id)
        mp_map = xml_index.get('by_barcode') or {}
    
    # Options
    multiplier = get_marketplace_multiplier('hepsiburada', user_id=user_id)
    
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
            
        # Prepare Item
        start_price = to_float(product.get('price')) or 0
        final_price = round(start_price * multiplier, 2)
        stock = to_int(product.get('quantity')) or 0
        
        raw_title = product.get('title', '')
        if title_prefix:
            raw_title = f"{title_prefix} {raw_title}"

        if final_price <= 0:
            skipped.append({'barcode': barcode, 'reason': 'Fiyat 0'})
            continue

        # Catalog Import (v1 mPOP Schema)
        images = product.get('images', [])
        img_urls = [ (i.get('url') if isinstance(i, dict) else i) for i in images ]
        
        # Build Attributes Object based on standard V1 requirements
        # Note: fields.00000MU is the most common internal code for 'Brand' in HB mPOP
        # Brand Override Logic
        default_hb_brand = Setting.get('HB_BRAND_NAME', user_id=user_id)
        if default_hb_brand:
            brand_val = default_hb_brand
            logging.info(f"HB Brand Override applied: {brand_val}")
        else:
            raw_brand = product.get('brand', 'Markasız')
            cat_id = to_int(product.get('categoryId', 0))
            brand_val = resolve_hepsiburada_brand(raw_brand, client, category_id=cat_id)
        
        attrs = {
            "merchantSku": barcode if match_by == 'barcode' else product.get('stockCode', barcode),
            "Barcode": barcode,
            "UrunAdi": raw_title,
            "Marka": brand_val,
            "Brand": brand_val,
            "fields.00000MU": brand_val,
            "price": str(final_price).replace('.', ','),
            "stock": str(stock),
            "tax_vat_rate": str(to_int(product.get('vatRate', 20))),
            "kg": str(to_int(product.get('desi', 1))),
            "GarantiSuresi": "2", # Standard 2 years for electronics, can be customized later
            "UrunAciklamasi": product.get('description', raw_title),
        }

        # Add manual attributes if present
        if product.get('is_manual') and product.get('marketplace_attributes_json'):
            try:
                import json
                mp_attrs = json.loads(product.get('marketplace_attributes_json'))
                for a in mp_attrs:
                    # HB attributes are just key-value pairs in mPOP v1
                    attrs[a.get('name')] = a.get('value')
            except: pass
        
        # Add images as Image1, Image2...
        for idx, url in enumerate(img_urls[:5]):
            attrs[f"Image{idx+1}"] = url

        # mPOP v1 Item Structure
        item = {
            "categoryId": to_int(product.get('categoryId', 0)),
            "merchant": client.merchant_id, # Merchant UUID
            "attributes": attrs
        }

        products_to_send.append(item)
        
    if not products_to_send:
        return {'success': False, 'message': 'Gönderilecek geçerli ürün yok.', 'skipped': skipped}
        
    append_mp_job_log(job_id, f"{len(products_to_send)} ürün Hepsiburada Katalog Import (JSON) olarak hazırlanıyor...")
    
    # Send
    try:
        import json
        json_payload = json.dumps(products_to_send, ensure_ascii=False)
        
        # Submit via Catalog Import API
        result = client.import_products_file(json_payload, file_name=f"vidos_import_{int(time.time())}.json")
        
        # Hepsiburada v1 response usually nests trackingId inside 'data'
        track_id = result.get('data', {}).get('trackingId') or result.get('trackingId') or result.get('id')
        
        if not track_id:
            append_mp_job_log(job_id, f"Uyarı: API başarılı döndü ancak Takip ID bulunamadı. Tam yanıt: {result}", level='warning')
        else:
            append_mp_job_log(job_id, f"Katalog gönderimi başarılı. Takip ID: {track_id}")
        
        return {
            'success': True,
            'count': len(products_to_send),
            'tracking_id': track_id,
            'skipped': skipped,
            'message': f"{len(products_to_send)} ürün Hepsiburada Katalog İçe Aktarım kuyruğuna girdi. (Takip ID: {track_id})"
        }
        
    except Exception as e:
        msg = f"Hepsiburada API hatası: {str(e)}"
        append_mp_job_log(job_id, msg, level='error')
        return {'success': False, 'message': msg}

def perform_hepsiburada_bulk_inventory_update(job_id: str, barcodes: List[str], user_id: int) -> Dict[str, Any]:
    """
    Update existing products on Hepsiburada (Price/Stock) in bulk via job.
    This typically uses the Listing API (Inventory Uploads).
    """
    append_mp_job_log(job_id, "Hepsiburada toplu güncelleme işlemi başlatılıyor...")
    
    try:
        client = get_hepsiburada_client(user_id=user_id)
    except Exception as e:
        return {'success': False, 'message': str(e)}

    # Similar multiplier logic
    multiplier = get_marketplace_multiplier('hepsiburada', user_id=user_id)
    
    updates = []
    from app.models import Product
    
    for barcode in barcodes:
        p = Product.query.filter_by(barcode=barcode, user_id=user_id).first()
        if not p: continue
        
        final_price = round((p.sale_price or 0) * multiplier, 2)
        if final_price <= 0: continue
        
        updates.append({
            "merchantSku": p.barcode,
            "price": {"amount": final_price, "currency": "TRY"},
            "availableStock": p.stock or 0
        })

    if not updates:
        return {'success': False, 'message': 'Güncellenecek ürün bulunamadı.'}

    try:
        result = client.upload_products(updates) # Inventory Uploads is used for price/stock updates
        track_id = result.get('id') or result.get('trackingId')
        append_mp_job_log(job_id, f"Güncelleme gönderildi. Takip ID: {track_id}")
        return {'success': True, 'count': len(updates), 'tracking_id': track_id}
    except Exception as e:
        return {'success': False, 'message': f"API Hatası: {e}"}

def perform_hepsiburada_product_update(barcode: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Update a single product on Hepsiburada (Title, Description, Price, Stock).
    Uses Ticket API for metadata and Listing API for Price/Stock.
    """
    try:
        from flask_login import current_user
        client = get_hepsiburada_client(user_id=current_user.id)
        multiplier = get_marketplace_multiplier('hepsiburada', user_id=current_user.id)
        
        # 1. Update Inventory (Price/Stock)
        price = to_float(data.get('sale_price'))
        stock = to_int(data.get('stock'))
        
        inventory_payload = [{
            "merchantSku": barcode,
            "price": {"amount": round(price * multiplier, 2), "currency": "TRY"},
            "availableStock": stock
        }]
        
        client.upload_products(inventory_payload)
        
        # 2. Update Metadata (Title/Description) via Ticket
        meta_payload = [{
            "merchantSku": barcode,
            "productName": data.get('title'),
            "description": data.get('description', data.get('title'))
        }]
        
        # Optional: Add image if provided in data
        if data.get('image_url'):
            meta_payload[0]["Image1"] = data.get('image_url')
            
        import json
        ticket_res = client.create_update_ticket(json.dumps(meta_payload, ensure_ascii=False))
        
        return {
            'success': True, 
            'message': 'Ürün güncellendi. Fiyat/Stok yansıdı, bilgiler onay bekliyor.',
            'tracking_id': ticket_res.get('id') or ticket_res.get('trackingId')
        }
    except Exception as e:
        logging.error(f"Hepsiburada single product update error: {e}")
        return {'success': False, 'message': str(e)}

def bulk_update_hepsiburada_stock_price(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Update multiple products (price/stock) on Hepsiburada.
    """
    try:
        from flask_login import current_user
        client = get_hepsiburada_client(user_id=current_user.id)
        multiplier = get_marketplace_multiplier('hepsiburada', user_id=current_user.id)
        
        payload = []
        for it in items:
            barcode = it.get('barcode')
            price = to_float(it.get('sale_price'))
            stock = to_int(it.get('stock'))
            
            payload.append({
                "merchantSku": barcode,
                "price": {"amount": round(price * multiplier, 2), "currency": "TRY"},
                "availableStock": stock
            })
            
        client.upload_products(payload)
        return {'success': True, 'message': f'{len(items)} ürün güncellendi.'}
    except Exception as e:
        logging.error(f"HB Bulk Update Error: {e}")
        return {'success': False, 'message': str(e)}

def perform_hepsiburada_send_all(job_id: str, xml_source_id: Any, title_prefix: str = None, match_by: str = 'barcode', user_id: int = None) -> Dict[str, Any]:
    """
    Send all products from an XML source to Hepsiburada.
    """
    # Resolve user_id if not provided (for background jobs)
    if user_id is None:
        try:
            from flask_login import current_user
            if current_user and current_user.is_authenticated:
                user_id = current_user.id
        except Exception:
            pass
    
    try:
        from app.services.xml_service import load_xml_source_index
        xml_index = load_xml_source_index(xml_source_id)
        barcodes = list(xml_index.get('by_barcode', {}).keys())
        
        if not barcodes:
            return {'success': False, 'message': 'XML kaynağında ürün bulunamadı.'}
            
        return perform_hepsiburada_send_products(
            job_id, 
            barcodes, 
            xml_source_id, 
            title_prefix=title_prefix, 
            match_by=match_by,
            user_id=user_id
        )
    except Exception as e:
        msg = f"Hepsiburada send_all error: {str(e)}"
        from app.services.job_queue import append_mp_job_log
        append_mp_job_log(job_id, msg, level='error')
        return {'success': False, 'message': msg}

def perform_hepsiburada_metadata_update(job_id: str, barcodes: List[str], user_id: int) -> Dict[str, Any]:
    """
    Update product metadata (Title, Description, Images) via Hepsiburada Ticket API.
    """
    from app.services.job_queue import append_mp_job_log
    append_mp_job_log(job_id, "Hepsiburada meta veri güncelleme (Ticket) başlatılıyor...")
    
    try:
        client = get_hepsiburada_client(user_id=user_id)
    except Exception as e:
        return {'success': False, 'message': str(e)}

    from app.models import Product
    updates = []
    
    for barcode in barcodes:
        p = Product.query.filter_by(barcode=barcode, user_id=user_id).first()
        if not p: continue
        
        # Build item with ONLY fields to update
        item = {
            "merchantSku": p.barcode,
            "productName": p.title,
            "description": p.description or p.title,
        }
        
        if p.image_url:
            item["Image1"] = p.image_url
            
        updates.append(item)

    if not updates:
        return {'success': False, 'message': 'Güncellenecek ürün meta verisi bulunamadı.'}

    try:
        import json
        json_payload = json.dumps(updates, ensure_ascii=False)
        result = client.create_update_ticket(json_payload)
        
        track_id = result.get('id') or result.get('trackingId')
        append_mp_job_log(job_id, f"Meta veri güncelleme talebi (Ticket) gönderildi. Takip ID: {track_id}")
        
        return {
            'success': True, 
            'count': len(updates), 
            'tracking_id': track_id,
            'message': 'Ürün bilgileri Hepsiburada onayına gönderildi.'
        }
    except Exception as e:
        return {'success': False, 'message': f"Ticket API Hatası: {e}"}

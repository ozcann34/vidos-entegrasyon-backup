
import logging
import time
from typing import List, Dict, Any, Optional

from app import db
from app.models import Setting, SupplierXML, MarketplaceProduct
from app.services.hepsiburada_client import HepsiburadaClient
from app.services.xml_service import load_xml_source_index
from app.services.job_queue import append_mp_job_log, update_mp_job, get_mp_job
from app.utils.helpers import get_marketplace_multiplier, to_float, to_int, is_product_forbidden, calculate_price

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

def perform_hepsiburada_send_products(job_id: str, barcodes: List[str], xml_source_id: Any, user_id: int = None, **kwargs) -> Dict[str, Any]:
    """
    Send selected products from XML to Hepsiburada.
    """
    append_mp_job_log(job_id, "Hepsiburada gönderim işlemi başlatılıyor...")
    
    try:
        if not user_id and xml_source_id:
            try:
                s_id = str(xml_source_id)
                if s_id.isdigit():
                    src = SupplierXML.query.get(int(s_id))
                    if src: user_id = src.user_id
            except Exception as e:
                logging.warning(f"Failed to resolve user_id: {e}")

        client = get_hepsiburada_client(user_id=user_id)
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

        # final_price = round(start_price * multiplier, 2)
        final_price = calculate_price(start_price, 'hepsiburada', user_id=user_id, multiplier_override=multiplier)
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
        
        item = {
            "merchantSku": barcode,
            "productName": title[:200], # Added title if needed
            "VaryantGroupID": product.get('parent_barcode') or product.get('modelCode') or product.get('productCode') or barcode, 
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
        msg = 'Gönderilecek geçerli ürün yok.'
        if skipped:
             msg += f" (Atlanan: {len(skipped)}). İlk sebep: {skipped[0]['reason']}"
        append_mp_job_log(job_id, msg, level='warning')
        return {'success': False, 'message': msg, 'skipped': skipped}
        
    append_mp_job_log(job_id, f"{len(products_to_send)} ürün hazırlanarak Hepsiburada'ya gönderiliyor (Listing API)...")
    
    # Send
    try:
        # Submit via Listing API using the fixed Auth method in client
        result = client.upload_products(products_to_send)
        track_id = result.get('id') or result.get('trackingId')
        
        append_mp_job_log(job_id, f"Gönderim başarılı. Takip ID: {track_id}")

        # --- STATUS CHECK LOOP ---
        if track_id:
            append_mp_job_log(job_id, f"Takip ID {track_id} durumu kontrol ediliyor (maks 30sn)...", level='info')
            imported_cnt = 0
            failed_cnt = 0
            failures_list = []
            
            for _chk in range(10): # 10 * 3s = 30s
                time.sleep(3)
                try:
                    status_res = client.check_upload_status(track_id)
                    # Expected response based on listing api:
                    # { "id": "...", "status": "COMPLETED", "totalCount": 1, "successCount": 1, "failedCount": 0, "result": [...] }
                    status_enum = status_res.get('status')
                    
                    if status_enum in ('COMPLETED', 'DONE', 'FINISHED'):
                        s_cnt = status_res.get('successCount', 0)
                        f_cnt = status_res.get('failedCount', 0)
                        imported_cnt = s_cnt
                        failed_cnt = f_cnt
                        
                        append_mp_job_log(job_id, f"İşlem Tamamlandı. Başarılı: {s_cnt}, Hatalı: {f_cnt}")
                        
                        # Log failures
                        if f_cnt > 0:
                            results_list = status_res.get('result', []) or status_res.get('results', [])
                            for res_item in results_list:
                                if res_item.get('status') == 'FAILED':
                                    merchant_sku = res_item.get('merchantSku')
                                    err_msg = res_item.get('explanation') or res_item.get('message') or "Bilinmeyen hata"
                                    failures_list.append({'barcode': merchant_sku, 'reason': err_msg})
                                    if len(failures_list) <= 10:
                                        append_mp_job_log(job_id, f"   ❌ {merchant_sku}: {err_msg}", level='error')
                        break
                    elif status_enum == 'FAILED':
                        append_mp_job_log(job_id, f"İşlem tamamen BAŞARISIZ oldu: {status_res.get('message')}", level='error')
                        break
                    # If QUEUED or PROCESSING, continue
                except Exception as poll_err:
                     append_mp_job_log(job_id, f"Durum kontrol hatası: {poll_err}", level='warning')
                     break
        # -------------------------
        
        return {
            'success': True,
            'success_count': imported_cnt if 'imported_cnt' in locals() and imported_cnt > 0 else len(products_to_send),
            'fail_count': (failed_cnt if 'failed_cnt' in locals() else 0) + len(skipped),
            'tracking_id': track_id,
            'skipped': skipped,
            'message': f"{len(products_to_send)} ürün Hepsiburada'ya iletildi. (Takip ID: {track_id})",
            'summary': {
                'success_count': imported_cnt if 'imported_cnt' in locals() and imported_cnt > 0 else len(products_to_send),
                'fail_count': (failed_cnt if 'failed_cnt' in locals() else 0) + len(skipped),
                'failures': failures_list if 'failures_list' in locals() else []
            }
        }
        
    except Exception as e:
        msg = f"Hepsiburada API hatası: {str(e)}"
        append_mp_job_log(job_id, msg, level='error')
        return {'success': False, 'message': msg}

def perform_hepsiburada_send_all(job_id: str, xml_source_id: Any, user_id: int = None, **kwargs) -> Dict[str, Any]:
    """Send ALL products from XML source to Hepsiburada"""
    append_mp_job_log(job_id, "Tüm ürünler hazırlanıyor...")
    
    xml_index = load_xml_source_index(xml_source_id)
    mp_map = xml_index.get('by_barcode') or {}
    all_barcodes = list(mp_map.keys())
    
    if not all_barcodes:
        return {'success': False, 'message': 'XML kaynağında ürün bulunamadı.', 'count': 0}
    
    append_mp_job_log(job_id, f"Toplam {len(all_barcodes)} ürün bulundu. Gönderim başlıyor...")
    
    return perform_hepsiburada_send_products(job_id, all_barcodes, xml_source_id, user_id=user_id, **kwargs)

def perform_hepsiburada_batch_update(job_id: str, items: List[Dict[str, Any]], user_id: int = None) -> Dict[str, Any]:
    """
    Batch update Hepsiburada stock/price from Excel items.
    items: [{'barcode': '...', 'stock': 10, 'price': 100.0}, ...]
    """
    try:
        client = get_hepsiburada_client(user_id=user_id)
        append_mp_job_log(job_id, f"Hepsiburada toplu güncelleme başlatıldı. {len(items)} ürün.")
        
        payload = []
        for item in items:
            barcode = item['barcode']
            
            # Form standard HB listing payload
            # We need at least price or stock. 
            # If one is missing, we might have issues if we don't have current values.
            # But normally Listing API allows partial if we send the same structure.
            
            hb_item = {
                "merchantSku": barcode,
                "dispatchTime": 3, # Default
                "cargoCompany1": "Yurtiçi Kargo" # Default
            }
            
            if 'stock' in item:
                hb_item["availableStock"] = int(item['stock'])
            
            if 'price' in item:
                hb_item["price"] = {
                    "amount": float(item['price']),
                    "currency": "TRY"
                }
            
            payload.append(hb_item)
            
        if not payload:
            return {'success': False, 'message': 'Güncellenecek veri bulunamadı.'}
            
        # Send in chunks of 50 (HB limit is 1000 but small chunks safer for logs)
        total_sent = 0
        from app.utils.helpers import chunked
        for chunk in chunked(payload, 50):
            client.upload_products(chunk)
            total_sent += len(chunk)
            append_mp_job_log(job_id, f"{total_sent}/{len(payload)} ürün gönderildi.")
            time.sleep(1)
            
        append_mp_job_log(job_id, "Hepsiburada güncelleme işlemi tamamlandı.")
        return {'success': True, 'count': total_sent}
        
    except Exception as e:
        msg = f"Hepsiburada batch update hatası: {str(e)}"
        append_mp_job_log(job_id, msg, level='error')
        return {'success': False, 'message': msg}

def sync_hepsiburada_products(user_id: int) -> Dict[str, Any]:
    """Hepsiburada ürünlerini çek ve MarketplaceProduct tablosuna kaydet/güncelle."""
    try:
        client = get_hepsiburada_client(user_id=user_id)
        
        offset = 0
        limit = 100
        total_synced = 0
        
        while True:
            res = client.get_products(offset=offset, limit=limit)
            items = res.get('items', [])
            if not items:
                break
            
            for item in items:
                barcode = item.get('merchantSku')
                if not barcode:
                    continue
                
                mp_product = MarketplaceProduct.query.filter_by(
                    user_id=user_id,
                    marketplace='hepsiburada',
                    barcode=barcode
                ).first()
                
                if not mp_product:
                    mp_product = MarketplaceProduct(
                        user_id=user_id,
                        marketplace='hepsiburada',
                        barcode=barcode
                    )
                    db.session.add(mp_product)
                
                mp_product.title = item.get('productName')
                mp_product.stock = to_int(item.get('availableStock', 0))
                
                price_data = item.get('price', {})
                mp_product.sale_price = to_float(price_data.get('amount', 0))
                
                # Durum Eşitleme
                # isActive=True/False veya status='ACTIVE' kontrolü
                is_active = item.get('status') == 'ACTIVE' or item.get('isActive') == True
                mp_product.on_sale = is_active
                mp_product.status = 'Aktif' if is_active else 'Pasif'
                
            db.session.commit()
            total_synced += len(items)
            offset += limit
            
            if len(items) < limit:
                break
                
        return {'success': True, 'count': total_synced}
    except Exception as e:
        logging.error(f"Hepsiburada senkronizasyon hatası: {str(e)}")
        return {'success': False, 'message': str(e)}

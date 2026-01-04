
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
    
    # Options from kwargs or defaults (Multiplier kaldırıldı - artık GLOBAL_PRICE_RULES kullanılıyor)
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

        # Artık GLOBAL_PRICE_RULES kullanılıyor (multiplier kaldırıldı)
        final_price = calculate_price(start_price, 'hepsiburada', user_id=user_id)
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
            "MerchantSku": barcode,
            "ProductName": title[:200], # Added title if needed
            "VaryantGroupID": product.get('parent_barcode') or product.get('modelCode') or product.get('productCode') or barcode, 
            "Price": {
                "Amount": final_price,
                "Currency": "TRY"
            },
            "AvailableStock": stock,
            "DispatchTime": 3,
            "CargoCompany": "Yurtiçi Kargo"
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
                hb_item["AvailableStock"] = int(item['stock'])
            
            if 'price' in item:
                hb_item["Price"] = {
                    "Amount": float(item['price']),
                    "Currency": "TRY"
                }
            
            payload.append(hb_item)
            
        if not payload:
            return {'success': False, 'message': 'Güncellenecek veri bulunamadı.'}
            
        # Send in chunks of 50 (HB limit is 1000 but small chunks safer for logs)
        total_sent = 0
        from app.utils.helpers import chunked
        for chunk in chunked(payload, 50):
            try:
                res = client.upload_products(chunk)
                total_sent += len(chunk)
                append_mp_job_log(job_id, f"✅ {len(chunk)} ürün gönderildi. (Toplam: {total_sent}/{len(payload)})")
                
                # Check for individual item status if possible via HB (usually it's async but check Response if available)
                # HB returns status code 202 and a tracking ID.
                # If we have errors at this stage, they are usually authentication or schema errors.
            except Exception as batch_err:
                append_mp_job_log(job_id, f"❌ Chunk gönderim hatası: {str(batch_err)}", level='error')
            
            time.sleep(1)
            
        append_mp_job_log(job_id, f"Hepsiburada güncelleme işlemi tamamlandı. Toplam {total_sent} ürün iletildi.")
        return {'success': True, 'count': total_sent}
        
    except Exception as e:
        msg = f"Hepsiburada batch update hatası: {str(e)}"
        append_mp_job_log(job_id, msg, level='error')
        return {'success': False, 'message': msg}

def sync_hepsiburada_with_xml_diff(job_id: str, xml_source_id: Any, user_id: int = None, **kwargs) -> Dict[str, Any]:
    """Smart Sync for Hepsiburada (Diff Logic)"""
    startTime = time.time()
    append_mp_job_log(job_id, "Hepsiburada Akıllı Senkronizasyon (Diff Sync) başlatıldı.")
    
    client = get_hepsiburada_client(user_id=user_id)
    
    # 1. Fetch Remote Inventory
    remote_items = []
    offset = 0
    limit = 100
    while True:
        try:
            res = client.get_products(offset=offset, limit=limit)
            items = res.get('items', [])
            if not items: break
            remote_items.extend(items)
            if len(items) < limit: break
            offset += limit
            if offset > 50000: break # Safety
        except Exception as e:
            append_mp_job_log(job_id, f"Hepsiburada envanter çekme hatası: {e}", level='error')
            break
            
    append_mp_job_log(job_id, f"Hepsiburada hesabınızda toplam {len(remote_items)} ürün tespit edildi.")
    remote_barcodes = {item.get('merchantSku') for item in remote_items if item.get('merchantSku')}

    # 2. Load XML
    xml_index = load_xml_source_index(xml_source_id)
    xml_map = xml_index.get('by_barcode') or {}
    xml_barcodes = set(xml_map.keys())

    # 3. Find Diff
    to_zero_barcodes = remote_barcodes - xml_barcodes
    append_mp_job_log(job_id, f"XML'de OLMAYAN {len(to_zero_barcodes)} ürün Hepsiburada'da sıfırlanıyor.")

    # 4. Zero out missing
    zeroed_count = 0
    if to_zero_barcodes:
        zero_payload = []
        for bc in to_zero_barcodes:
            zero_payload.append({
                "MerchantSku": bc,
                "AvailableStock": 0,
                "CargoCompany": "Yurtiçi Kargo" # Required by HB Listing API
            })
        
        for chunk in chunked(zero_payload, 100):
            try:
                client.upload_products(chunk)
                zeroed_count += len(chunk)
                append_mp_job_log(job_id, f"✅ {zeroed_count}/{len(to_zero_barcodes)} ürün sıfırlandı.")
                time.sleep(1)
            except Exception as e:
                append_mp_job_log(job_id, f"Sıfırlama hatası: {e}", level='error')

    # 5. Sync Existing/New from XML
    sync_res = perform_hepsiburada_send_all(job_id, xml_source_id, user_id=user_id, **kwargs)
    sync_res['zeroed_count'] = zeroed_count
    
    append_mp_job_log(job_id, f"Hepsiburada senkronizasyon tamamlandı. Süre: {time.time()-startTime:.1f}s")
    return sync_res

def perform_hepsiburada_sync_all(job_id: str, xml_source_id: Any, user_id: int = None, **kwargs) -> Dict[str, Any]:
    return sync_hepsiburada_with_xml_diff(job_id, xml_source_id, user_id=user_id, **kwargs)

def clear_hepsiburada_cache(user_id: int):
    """Placeholder for Hepsiburada cache clear.
    This service currently doesn't use heavy caching but keeping it for UI compatibility.
    """
    pass

def create_hepsiburada_catalog_request(product_data: Dict[str, Any], user_id: int = None):
    """
    [PREPARATION] Basic structure for Hepsiburada Catalog API (Product Creation)
    HB Catalog API is complex and requires specific attribute mapping.
    This function will eventually generate the JSON payload for 'POST /product/create'
    """
    # Placeholder structure for Phase 2
    # To be used for creating brand new products on HB instead of just listings
    pass


def perform_hepsiburada_direct_push_actions(user_id: int, to_update: List[Any], to_create: List[Any], to_zero: List[Any], src: Any, job_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Hepsiburada için Direct Push aksiyonlarını gerçekleştirir.
    Hepsiburada 'Listing API' (inventory-uploads) kullanılarak hem güncelleme hem de (bazı durumlarda) oluşturma yapılır.
    """
    import json
    from datetime import datetime
    from app.services.job_queue import append_mp_job_log, append_mp_job_logs, get_mp_job, update_mp_job, update_job_progress
    from app.utils.helpers import calculate_price, chunked
    from app.models import MarketplaceProduct, Setting, db
    
    client = get_hepsiburada_client(user_id=user_id)
    res = {'updated_count': 0, 'created_count': 0, 'zeroed_count': 0}

    total_ops = len(to_update or []) + len(to_create or []) + len(to_zero or [])
    completed_ops = 0
    if job_id:
        update_job_progress(job_id, 0, total_ops, 'İşlemler başlatılıyor...')
    
    # Varsayılan değerler
    dispatch_time = int(Setting.get("HEPSIBURADA_DISPATCH_TIME", "3", user_id=user_id))
    cargo_company = Setting.get("HEPSIBURADA_CARGO_COMPANY", "Hepsijet", user_id=user_id)

    # --- 1. GÜNCELLEMELER (Update) ---
    if to_update:
        if job_id: update_job_progress(job_id, completed_ops, total_ops, f'Güncellemeler hazırlanıyor ({len(to_update)} ürün)...')
        
        update_payloads = []
        db_mappings = []
        batch_logs = []
        
        for xml_item, local_item in to_update:
            # Periodic cancel check
            if len(db_mappings) % 50 == 0 and job_id:
                js = get_mp_job(job_id)
                if js and js.get('cancel_requested'):
                    append_mp_job_log(job_id, "İşlem kullanıcı tarafından iptal edildi.", level='warning')
                    return res
            
            final_price, rule_desc = calculate_price(xml_item.price, 'hepsiburada', user_id=user_id, return_details=True)
            final_price = round(final_price, 2)
            
            update_payloads.append({
                "merchantSku": local_item.stock_code,
                "Barcode": local_item.barcode,
                "Price": final_price,
                "AvailableStock": xml_item.quantity,
                "DispatchTime": dispatch_time,
                "CargoCompany1": cargo_company
            })
            
            db_mappings.append({
                'id': local_item.id,
                'price': xml_item.price, # Base
                'quantity': xml_item.quantity,
                'sale_price': final_price, # Calculated
                'last_sync_at': datetime.now()
            })
            
            if job_id:
                status_log = f"[{local_item.stock_code}] Fiyat: {local_item.sale_price} -> {final_price} ({rule_desc}), Stok: {local_item.quantity} -> {xml_item.quantity}"
                batch_logs.append(status_log)

        # Batch execute API and DB calls
        try:
            for i, batch in enumerate(chunked(update_payloads, 100)):
                if job_id and i % 5 == 0:
                    js = get_mp_job(job_id)
                    if js and js.get('cancel_requested'):
                        append_mp_job_log(job_id, "İptal edildi (Batch sırasında)", level='warning')
                        return res
                
                client.upload_products(batch)
                res['updated_count'] += len(batch)
                
                # Corresponding DB updates
                batch_mappings = db_mappings[i*100 : (i+1)*100]
                db.session.bulk_update_mappings(MarketplaceProduct, batch_mappings)
                db.session.commit()
                
                # Batch Logs
                if job_id:
                    curr_batch_logs = batch_logs[i*100 : (i+1)*100]
                    append_mp_job_logs(job_id, curr_batch_logs)

                completed_ops += len(batch)
                if job_id:
                    update_job_progress(job_id, completed_ops, total_ops, f"Güncelleniyor ({completed_ops}/{total_ops})...")
            
        except Exception as e:
            db.session.rollback()
            if job_id: append_mp_job_log(job_id, f"Hepsiburada güncelleme hatası: {str(e)}", level='error')

    # --- 2. YENİ ÜRÜNLER (Create) ---
    if to_create:
        if job_id: update_job_progress(job_id, completed_ops, total_ops, f'Yeni ürünler hazırlanıyor ({len(to_create)} ürün)...')
        from app.services.xml_service import generate_random_barcode
        
        valid_creates = []
        for xml_item in to_create:
            if len(valid_creates) % 50 == 0 and job_id:
                js = get_mp_job(job_id)
                if js and js.get('cancel_requested'):
                    append_mp_job_log(job_id, "İşlem kullanıcı tarafından iptal edildi.", level='warning')
                    return res
            
            barcode = xml_item.barcode
            use_random_setting = Setting.get(f'AUTO_SYNC_USE_RANDOM_BARCODE_hepsiburada', user_id=user_id) == 'true'
            use_override_setting = Setting.get(f'AUTO_SYNC_USE_OVERRIDE_BARCODE_hepsiburada', user_id=user_id) == 'true'
            if use_override_setting or (not barcode and (src.use_random_barcode or use_random_setting)):
                barcode = generate_random_barcode()
            
            raw = json.loads(xml_item.raw_data)
            final_price, rule_desc = calculate_price(xml_item.price, 'hepsiburada', user_id=user_id, return_details=True)
            
            item_payload = {
                "MerchantSku": xml_item.stock_code,
                "Barcode": barcode,
                "Price": final_price,
                "AvailableStock": xml_item.quantity,
                "DispatchTime": dispatch_time,
                "CargoCompany": cargo_company,
                "VaryantGroupID": raw.get('modelCode') or raw.get('parent_barcode') or xml_item.stock_code
            }
            valid_creates.append((item_payload, xml_item, rule_desc))

        # API Chunks
        for batch in chunked(valid_creates, 50):
            if job_id:
                js = get_mp_job(job_id)
                if js and js.get('cancel_requested'):
                    append_mp_job_log(job_id, "İptal edildi (Create sırasında)", level='warning')
                    return res
 
            payloads = [x[0] for x in batch]
            try:
                client.upload_products(payloads)
                
                # Bulk DB Create
                new_mps = []
                batch_logs = []
                for item_payload, xml_record, r_desc in batch:
                    # Duplicate check for safety
                    existing = MarketplaceProduct.query.filter_by(user_id=user_id, marketplace='hepsiburada', stock_code=xml_record.stock_code).first()
                    if not existing:
                        new_mps.append(MarketplaceProduct(
                            user_id=user_id, marketplace='hepsiburada', barcode=item_payload['Barcode'],
                            stock_code=xml_record.stock_code, title=xml_record.title,
                            price=xml_record.price, # Base
                            sale_price=item_payload['Price'], # Calculated
                            quantity=xml_record.quantity, status='Pending', on_sale=True,
                            xml_source_id=src.id
                        ))
                        if job_id:
                            batch_logs.append(f"[YENİ] {xml_record.stock_code} yüklendi. Fiyat: {item_payload['Price']} ({r_desc}), Stok: {xml_record.quantity}")
                
                db.session.bulk_save_objects(new_mps)
                db.session.commit()
                
                if job_id:
                    append_mp_job_logs(job_id, batch_logs)

                res['created_count'] += len(batch)
                completed_ops += len(batch)
                if job_id:
                    update_job_progress(job_id, completed_ops, total_ops, f"Yeni Ürünler Ekleniyor ({completed_ops}/{total_ops})...")
            except Exception as e:
                db.session.rollback()
                if job_id: append_mp_job_log(job_id, f"Hepsiburada yükleme hatası: {str(e)}", level='error')

    if to_zero:
        if job_id: update_job_progress(job_id, completed_ops, total_ops, f'Stok sıfırlama hazırlanıyor ({len(to_zero)} ürün)...')
        zero_payloads = []
        zero_mappings = []
        
        for local_item in to_zero:
            # Periodic cancel check
            if len(zero_payloads) % 50 == 0 and job_id:
                js = get_mp_job(job_id)
                if js and js.get('cancel_requested'):
                    append_mp_job_log(job_id, "İşlem kullanıcı tarafından iptal edildi.", level='warning')
                    return res
            
            zero_payloads.append({
                "MerchantSku": local_item.stock_code,
                "Barcode": local_item.barcode,
                "Price": local_item.sale_price,
                "AvailableStock": 0,
                "DispatchTime": dispatch_time,
                "CargoCompany": cargo_company
            })
            zero_mappings.append({'id': local_item.id, 'quantity': 0})

        try:
            for i, batch in enumerate(chunked(zero_payloads, 100)):
                if job_id and i % 5 == 0:
                    js = get_mp_job(job_id)
                    if js and js.get('cancel_requested'):
                        append_mp_job_log(job_id, "İptal edildi (Zero sırasında)", level='warning')
                        return res
                
                client.upload_products(batch)
                res['zeroed_count'] += len(batch)
                
                batch_mappings = zero_mappings[i*100 : (i+1)*100]
                db.session.bulk_update_mappings(MarketplaceProduct, batch_mappings)
                db.session.commit()
                
                completed_ops += len(batch)
                if job_id:
                    update_job_progress(job_id, completed_ops, total_ops, f"Stoklar Sıfırlanıyor ({completed_ops}/{total_ops})...")
        except Exception as e:
            db.session.rollback()
            if job_id: append_mp_job_log(job_id, f"Hepsiburada stok sıfırlama hatası: {str(e)}", level='error')

    return res

import logging
import json
import time
from datetime import datetime
from typing import Dict, Any, List, Optional
from app import db
from app.models import MarketplaceProduct, SupplierXML, SyncLog, Setting, CachedXmlProduct
from app.services.job_queue import append_mp_job_log, update_mp_job, get_mp_job, update_job_progress
from app.services.xml_service import generate_random_barcode
import sqlalchemy

logger = logging.getLogger(__name__)

class DirectSyncService:
    @staticmethod
    def perform_sync(marketplace: str, user_id: int, xml_source_id: int, job_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Gelişmiş "Direct Push" Senkronizasyonu.
        Pazaryeri panelinden çekmek yerine yerel veritabanı (MarketplaceProduct) ile
        XML Önbelleği (CachedXmlProduct) karşılaştırılır.
        """
        start_time = time.time()
        result = {
            'success': False,
            'updated_count': 0,
            'created_count': 0,
            'zeroed_count': 0,
            'errors': []
        }

        # 1. Hazırlık ve Gereksinim Kontrolleri
        src = SupplierXML.query.get(xml_source_id)
        if not src:
            msg = "XML kaynağı bulunamadı."
            if job_id: append_mp_job_log(job_id, msg, level='error')
            return {'success': False, 'message': msg}

        if job_id:
            append_mp_job_log(job_id, f"[{marketplace.upper()}] Direct Push senkronizasyonu başlatıldı.")
            append_mp_job_log(job_id, f"Kaynak: {src.name} | Eşleşme: Stok Kodu")

        try:
            # Check Cancel
            if job_id:
                js = get_mp_job(job_id)
                if js and js.get('cancel_requested'):
                    append_mp_job_log(job_id, "İşlem kullanıcı tarafından iptal edildi.", level='warning')
                    return {'success': False, 'message': 'İptal edildi'}

            # 2. XML Verilerini Al (PostgreSQL'den)
            # Standart kolonları + ekstra veriler için raw_data'yı çekiyoruz
            xml_products = db.session.query(
                CachedXmlProduct.stock_code,
                CachedXmlProduct.price,
                CachedXmlProduct.quantity,
                CachedXmlProduct.barcode,
                CachedXmlProduct.title,
                CachedXmlProduct.raw_data
            ).filter(CachedXmlProduct.xml_source_id == xml_source_id).all()
            
            from types import SimpleNamespace
            import json
            
            xml_map = {}
            for p in xml_products:
                if not p.stock_code: continue
                try:
                    # Ham veriyi SimpleNamespace'e çevirerek dot-notation (p.images vb.) kazandırıyoruz
                    raw_dict = json.loads(p.raw_data) if p.raw_data else {}
                    ns = SimpleNamespace(**raw_dict)
                    
                    # Veritabanındaki standart/temizlenmiş değerleri garanti ediyoruz
                    ns.stock_code = p.stock_code
                    ns.price = p.price
                    ns.quantity = p.quantity
                    ns.barcode = p.barcode
                    ns.title = p.title
                    ns.raw_data = p.raw_data # Servislerin re-parse yapabilmesi için geri ekliyoruz
                    
                    xml_map[p.stock_code] = ns
                except Exception as e:
                    logger.warning(f"XML data parse error for {p.stock_code}: {e}")
            
            # 3. Yerel Pazaryeri Kayıtlarını Al
            local_products = db.session.query(
                MarketplaceProduct.id,
                MarketplaceProduct.stock_code,
                MarketplaceProduct.barcode,
                MarketplaceProduct.quantity,
                MarketplaceProduct.price,
                MarketplaceProduct.sale_price,
                MarketplaceProduct.xml_source_id
            ).filter_by(user_id=user_id, marketplace=marketplace).all()
            
            local_map = {p.stock_code: p for p in local_products if p.stock_code}

            if not xml_map:
                msg = "XML önbelleği boş. Lütfen önce XML'i yenileyin."
                if job_id: append_mp_job_log(job_id, msg, level='warning')
                return {'success': False, 'message': msg}

            # 4. Analiz (Diff)
            to_update = [] # (xml_item, local_item)
            to_create = [] # xml_item
            to_zero = []   # local_item

            for sc, xml_item in xml_map.items():
                if sc in local_map:
                    local_item = local_map[sc]
                    
                    # Kaynak Sahipliği Kontrolü (Atama yapmadan sadece kontrol ediyoruz)
                    current_sid = getattr(local_item, 'xml_source_id', None)
                    ownership_changed = (current_sid != xml_source_id)
                    
                    # Değişiklik kontrolü (Stok veya Fiyat veya Sahiplik)
                    if (xml_item.quantity != local_item.quantity or 
                        xml_item.price != local_item.price or 
                        ownership_changed):
                        to_update.append((xml_item, local_item))
                else:
                    to_create.append(xml_item)

            for sc, local_item in local_map.items():
                if sc not in xml_map and (local_item.quantity or 0) > 0:
                    # Sadece bu kaynağın sahipliğindeki ürünleri sıfırla
                    # xml_source_id NULL ise eski veridir, çakışmayı önlemek için dokunmuyoruz
                    if getattr(local_item, 'xml_source_id', None) == xml_source_id:
                        to_zero.append(local_item)

            total_diff = len(to_update) + len(to_create) + len(to_zero)
            if job_id:
                append_mp_job_log(job_id, f"Analiz tamamlandı: {len(to_update)} güncelleme, {len(to_create)} yeni, {len(to_zero)} sıfırlama.")

            if total_diff == 0:
                msg = "Tüm ürünler zaten güncel."
                if job_id: append_mp_job_log(job_id, msg)
                return {'success': True, 'message': msg}

            # Check Cancel
            if job_id:
                js = get_mp_job(job_id)
                if js and js.get('cancel_requested'):
                    append_mp_job_log(job_id, "İşlem kullanıcı tarafından iptal edildi.", level='warning')
                    return {'success': False, 'message': 'İptal edildi'}

            # 5. Aksiyon (Execution)
            # Burada pazaryeri bazlı servislere yönlendirilecek
            # Örn: perform_trendyol_direct_push_actions(...)
            
            execution_res = DirectSyncService._execute_actions(
                marketplace, user_id, to_update, to_create, to_zero, src, job_id
            )
            
            result.update(execution_res)
            result['success'] = True
            
        except Exception as e:
            logger.exception(f"Direct sync failed: {e}")
            result['errors'].append(str(e))
            if job_id: append_mp_job_log(job_id, f"Senkronizasyon hatası: {str(e)}", level='error')

        return result

    @staticmethod
    def _execute_actions(marketplace, user_id, to_update, to_create, to_zero, src, job_id):
        """Pazaryeri bazlı API çağrılarını yönetir."""
        res = {'updated_count': 0, 'created_count': 0, 'zeroed_count': 0}
        
        # Marketplace Client'ı hazırla
        if marketplace == 'trendyol':
            from app.services.trendyol_service import perform_trendyol_direct_push_actions
            res = perform_trendyol_direct_push_actions(user_id, to_update, to_create, to_zero, src, job_id)
            
        elif marketplace == 'idefix':
            from app.services.idefix_service import perform_idefix_direct_push_actions
            res = perform_idefix_direct_push_actions(user_id, to_update, to_create, to_zero, src, job_id)
            
        elif marketplace == 'hepsiburada':
            from app.services.hepsiburada_service import perform_hepsiburada_direct_push_actions
            res = perform_hepsiburada_direct_push_actions(user_id, to_update, to_create, to_zero, src, job_id)
            
        elif marketplace == 'pazarama':
            from app.services.pazarama_service import perform_pazarama_direct_push_actions
            res = perform_pazarama_direct_push_actions(user_id, to_update, to_create, to_zero, src, job_id)
            
        elif marketplace == 'n11':
            from app.services.n11_service import perform_n11_direct_push_actions
            res = perform_n11_direct_push_actions(user_id, to_update, to_create, to_zero, src, job_id)
            
        return res

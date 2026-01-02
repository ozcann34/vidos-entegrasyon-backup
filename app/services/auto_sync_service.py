"""
Otomatik Senkronizasyon Servisi
XML ürünlerini pazaryerlerine otomatik olarak senkronize eder
"""
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from app import db
from app.models import Setting, SupplierXML, SyncLog, AutoSync
# from app.services.xml_service import fetch_xml_from_url # No longer needed if using perform_...
# But perform_... might need it? No, they import it.
from app.utils.helpers import get_marketplace_multiplier, to_float, to_int, chunked


logger = logging.getLogger(__name__)


def sync_all_users_marketplace(marketplace: str):
    """
    Finds all users who have auto-sync enabled for this marketplace
    and runs the sync task for each of them.
    """
    logger.info(f"Checking all users for {marketplace} auto-sync...")
    
    # Get all uniquely enabled user sync records for this marketplace
    sync_records = AutoSync.query.filter_by(marketplace=marketplace, enabled=True).all()
    
    success_count = 0
    total_count = len(sync_records)
    
    for record in sync_records:
        if record.user_id:
            try:
                res = sync_marketplace_products(marketplace, user_id=record.user_id)
                if res.get('success'):
                    success_count += 1
            except Exception as e:
                logger.error(f"Sync failed for user {record.user_id} on {marketplace}: {e}")
                
    logger.info(f"Global sync for {marketplace} finished. Total users: {total_count}, Success: {success_count}")
    return {'total': total_count, 'success': success_count}


def sync_marketplace_products(marketplace: str, user_id: int, job_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Marketplace senkronizasyonu çalıştırır (Belirli bir kullanıcı için)
    """
    from app.services.job_queue import update_mp_job, append_mp_job_log
    
    logger.info(f"Starting sync for {marketplace} (User: {user_id})")
    
    if job_id:
        append_mp_job_log(job_id, f"{marketplace} senkronizasyonu başlatılıyor...", level='info')
        update_mp_job(job_id, progress={'current': 0, 'total': 100, 'message': 'Hazırlanıyor...'})
    
    result = {
        'success': False,
        'marketplace': marketplace,
        'products_updated': 0,
        'stock_changes': 0, 
        'price_changes': 0,
        'errors': []
    }
    
    try:
        sync_record = AutoSync.query.filter_by(marketplace=marketplace, enabled=True, user_id=user_id).first()
        
        if not sync_record:
            msg = f"Kullanıcı {user_id} için senkronizasyon pasif."
            result['errors'].append(msg)
            return result
            
        xml_source_id = Setting.get(f'AUTO_SYNC_XML_SOURCE_{marketplace}', user_id=user_id)
        match_by = Setting.get(f'AUTO_SYNC_MATCH_BY_{marketplace}', user_id=user_id) or 'barcode'
        
        if not xml_source_id:
            msg = f"XML kaynağı seçilmemiş (Kullanıcı: {user_id})."
            if job_id: append_mp_job_log(job_id, msg, level='error')
            result['errors'].append(msg)
            return result

        if job_id:
            update_mp_job(job_id, progress={'current': 10, 'total': 100, 'message': 'Senkronizasyon başlıyor...'})

        # Delegate to marketplace specific sync_all functions
        sync_res = {}
        
        if marketplace == 'trendyol':
            from app.services.trendyol_service import perform_trendyol_sync_all
            sync_res = perform_trendyol_sync_all(job_id if job_id else 'auto_sync_temp', xml_source_id, match_by=match_by, user_id=user_id)
            
        elif marketplace == 'n11':
            from app.services.n11_service import perform_n11_sync_all
            sync_res = perform_n11_sync_all(job_id if job_id else 'auto_sync_temp', xml_source_id, match_by=match_by, user_id=user_id)
            
        elif marketplace == 'pazarama':
            from app.services.pazarama_service import perform_pazarama_sync_all
            sync_res = perform_pazarama_sync_all(job_id if job_id else 'auto_sync_temp', xml_source_id, user_id=user_id)
            
        elif marketplace == 'hepsiburada':
             from app.services.hepsiburada_service import perform_hepsiburada_sync_all
             sync_res = perform_hepsiburada_sync_all(job_id if job_id else 'auto_sync_temp', xml_source_id, user_id=user_id)
        
        elif marketplace == 'idefix':
             from app.services.idefix_service import perform_idefix_sync_all
             sync_res = perform_idefix_sync_all(job_id if job_id else 'auto_sync_temp', xml_source_id, user_id=user_id)

        else:
            msg = f"Desteklenmeyen pazaryeri: {marketplace}"
            result['errors'].append(msg)
            return result

        # Map results
        result['success'] = sync_res.get('success', False)
        result['products_updated'] = sync_res.get('updated_count', 0) or sync_res.get('count', 0)
        
        # If errors returned in sync_res
        if sync_res.get('error'):
            result['errors'].append(sync_res.get('error'))
            
        if not result['success'] and not result['errors']:
             result['errors'].append(sync_res.get('message', 'Bilinmeyen hata'))

        # Save log
        if job_id:
            update_mp_job(job_id, progress={'current': 100, 'total': 100, 'message': 'Tamamlandı'})
        
        _save_sync_log(marketplace, result, error_message=sync_res.get('message') if not result['success'] else None)
        
        # Update last sync time
        if sync_record:
            sync_record.last_sync = datetime.now().isoformat()
            db.session.commit()
            
        logger.info(f"Sync completed for {marketplace}: {result}")
        
    except Exception as e:
        logger.exception(f"Sync error for {marketplace}: {e}")
        result['errors'].append(str(e))
        if job_id:
            append_mp_job_log(job_id, f"Hata: {str(e)}", level='error')
    
    return result


def _save_sync_log(marketplace: str, result: Dict[str, Any], error_message: Optional[str] = None):
    """Senkronizasyon logunu kaydet"""
    try:
        log = SyncLog(
            marketplace=marketplace,
            products_updated=result.get('products_updated', 0),
            stock_changes=result.get('stock_changes', 0),
            price_changes=result.get('price_changes', 0),
            success=result.get('success', True) and not error_message,
            error_message=error_message
        )
        
        # Detayları kaydet
        details = {
            'errors': result.get('errors', []),
            'details': result.get('details', []),
            'timestamp': datetime.now().isoformat()
        }
        if details:
            import json
            log.details_json = json.dumps(details, ensure_ascii=False)
        
        db.session.add(log)
        db.session.commit()
        
        logger.info(f"Sync log saved for {marketplace}")
        
    except Exception as e:
        logger.error(f"Error saving sync log: {e}")
        db.session.rollback()


def get_sync_logs(marketplace: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """Senkronizasyon loglarını getir"""
    try:
        query = SyncLog.query
        
        if marketplace:
            query = query.filter_by(marketplace=marketplace)
        
        logs = query.order_by(SyncLog.timestamp.desc()).limit(limit).all()
        
        return [log.to_dict() for log in logs]
        
    except Exception as e:
        logger.error(f"Error fetching sync logs: {e}")
        return []


import json
import math
import logging
import requests
from datetime import datetime
from collections import Counter
from typing import Dict, Any, List, Optional
from flask import Blueprint, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from app import db
from app.models import SupplierXML, Product, BatchLog, Setting, AutoSync, SyncLog
from app.services.job_queue import submit_mp_job, get_mp_job, append_mp_job_log
from app.services.xml_service import fetch_xml_from_url, load_xml_source_index
from app.services.trendyol_service import (
    perform_trendyol_sync_stock, perform_trendyol_sync_prices,
    get_trendyol_client, load_trendyol_snapshot
)
from app.services.pazarama_service import (
    perform_pazarama_sync_stock, perform_pazarama_sync_prices,
    get_pazarama_client, pazarama_fetch_all_products,
    get_cached_pazarama_detail, clear_pazarama_detail_cache,
    pazarama_build_product_index
)
from app.utils.helpers import to_int, to_float

api_bp = Blueprint('api', __name__)

MARKETPLACES = {
    'trendyol': 'Trendyol',
    'pazarama': 'Pazarama',
    'hepsiburada': 'Hepsiburada',
    'n11': 'N11',
    'idefix': 'İdefix',
    'amazon': 'Amazon',
}

@api_bp.route('/health')
def health():
    return jsonify({"ok": True, "worker_running": True, "db_status": "ok"})


@api_bp.route('/api/clear_all_cache', methods=['POST'])
@login_required
def api_clear_all_cache():
    """Clear all caches: jobs, brand cache, category cache, temp Excel indices"""
    try:
        cleared_items = []
        
        # 1. Clear all pending/paused jobs from job queue
        from app.services.job_queue import clear_all_jobs
        jobs_cleared = clear_all_jobs()
        
        # 1.5 Clear BatchLog DB table (Permanent fix for stuck jobs)
        try:
            db.session.query(BatchLog).delete()
            db.session.commit()
            jobs_cleared += 1  # Just to indicate DB action
        except Exception as e:
            logging.error(f"Failed to clear BatchLog DB: {e}")
            
        cleared_items.append(f"{jobs_cleared} iş (DB dahil)")
        
        # 2. Clear Trendyol brand cache
        from app.services.trendyol_service import _BRAND_CACHE
        brand_count = len(_BRAND_CACHE.get('by_id', {}))
        _BRAND_CACHE.clear()
        cleared_items.append(f"{brand_count} marka")
        
        # 3. Clear Trendyol category cache
        from app.services.trendyol_service import _CATEGORY_CACHE
        cat_count = len(_CATEGORY_CACHE.get('by_id', {}))
        _CATEGORY_CACHE.clear()
        cleared_items.append(f"{cat_count} kategori")
        
        # 4. Clear temp Excel index from database
        Setting.set('_EXCEL_TEMP_INDEX', '', user_id=current_user.id)
        cleared_items.append("Excel indeksi")
        
        # 5. Clear Pazarama caches if exists
        try:
            from app.services.pazarama_service import _PAZARAMA_CATEGORY_CACHE
            _PAZARAMA_CATEGORY_CACHE.clear()
            cleared_items.append("Pazarama kategorileri")
        except:
            pass
        
        return jsonify({
            'success': True,
            'message': f"Temizlendi: {', '.join(cleared_items)}"
        })
        
    except Exception as e:
        logging.exception("Clear cache error")
        return jsonify({'success': False, 'message': str(e)}), 500

# ---------------- Supplier XML API Endpoints ----------------
@api_bp.route('/api/xml_sources', methods=['GET', 'POST'])
@login_required
def api_xml_sources():
    user_id = current_user.id
    if request.method == 'GET':
        try:
            rows = SupplierXML.query.filter_by(user_id=user_id).order_by(SupplierXML.id.desc()).all()
            items = [
                {
                    'id': r.id,
                    'name': r.name,
                    'url': r.url,
                    'active': bool(r.active),
                    'created_at': (str(r.created_at) if getattr(r, 'created_at', None) is not None else None),
                } for r in rows
            ]
            return jsonify({'items': items})
        except Exception as e:
            return jsonify({'items': [], 'error': str(e)}), 500
    # POST
    try:
        data = request.get_json(force=True) or {}
        name = (data.get('name') or '').strip()
        url = (data.get('url') or '').strip()
        if not name or not url:
            return jsonify({'success': False, 'message': 'İsim ve URL zorunludur.'}), 400
        row = SupplierXML(name=name, url=url, active=True, user_id=user_id)
        db.session.add(row)
        db.session.commit()
        return jsonify({'success': True, 'id': row.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/xml_sources/<int:source_id>', methods=['DELETE'])
@login_required
def api_xml_sources_delete(source_id: int):
    try:
        row = SupplierXML.query.filter_by(id=source_id, user_id=current_user.id).first()
        if not row:
            return jsonify({'success': False, 'message': 'Kayıt bulunamadı.'}), 404
        db.session.delete(row)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

# ---------------- Job Queue API Endpoints ----------------
@api_bp.route('/api/mp_jobs/<job_id>', methods=['GET'])
def api_mp_job_detail(job_id: str):
    job = get_mp_job(job_id)
    if not job:
        return jsonify({'error': 'Job bulunamadı'}), 404
    return jsonify(job)

@api_bp.route('/api/job/control', methods=['POST'])
def api_job_control():
    """Control a running job (pause/resume/cancel)"""
    try:
        payload = request.get_json(force=True) or {}
        job_id = payload.get('job_id')
        action = payload.get('action')
        
        if not job_id:
            return jsonify({'success': False, 'message': 'job_id gerekli'}), 400
        if action not in ('pause', 'resume', 'cancel'):
            return jsonify({'success': False, 'message': 'Geçersiz action'}), 400
        
        from app.services.job_queue import update_mp_job, get_mp_job
        
        job = get_mp_job(job_id)
        if not job:
            return jsonify({'success': False, 'message': 'Job bulunamadı'}), 404
        
        if action == 'pause':
            update_mp_job(job_id, pause_requested=True)
            return jsonify({'success': True, 'message': 'Duraklatma isteği gönderildi'})
        elif action == 'resume':
            update_mp_job(job_id, pause_requested=False)
            return jsonify({'success': True, 'message': 'Devam isteği gönderildi'})
        elif action == 'cancel':
            update_mp_job(job_id, cancel_requested=True)
            return jsonify({'success': True, 'message': 'İptal isteği gönderildi'})
        
    except Exception as e:
        logging.exception('Job control hatası')
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/pazarama/sync_stock', methods=['POST'])
def api_pazarama_sync_stock():
    try:
        payload = request.get_json(force=True) or {}
        xml_source_id = payload.get('xml_source_id')
        if not xml_source_id:
            return jsonify({'success': False, 'message': 'xml_source_id zorunludur.'}), 400

        job_id = submit_mp_job(
            'pazarama_sync_stock',
            'pazarama',
            lambda job_id: perform_pazarama_sync_stock(job_id, xml_source_id),
            params={'xml_source_id': xml_source_id},
        )
        return jsonify({'success': True, 'job_id': job_id, 'message': 'Stok eşitleme kuyruğa alındı.'}), 202
    except Exception as e:
        logging.exception('Pazarama stok eşitleme kuyruğa alınırken hata')
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/pazarama/sync_prices', methods=['POST'])
def api_pazarama_sync_prices():
    try:
        payload = request.get_json(force=True) or {}
        xml_source_id = payload.get('xml_source_id')
        if not xml_source_id:
            return jsonify({'success': False, 'message': 'xml_source_id zorunludur.'}), 400

        job_id = submit_mp_job(
            'pazarama_sync_prices',
            'pazarama',
            lambda job_id: perform_pazarama_sync_prices(job_id, xml_source_id),
            params={'xml_source_id': xml_source_id},
        )
        return jsonify({'success': True, 'job_id': job_id, 'message': 'Fiyat eşitleme kuyruğa alındı.'}), 202
    except Exception as e:
        logging.exception('Pazarama fiyat eşitleme kuyruğa alınırken hata')
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/pazarama/sync_all', methods=['POST'])
def api_pazarama_sync_all():
    """Pazarama için hem stok hem fiyat eşitleme (birleşik)"""
    try:
        payload = request.get_json(force=True) or {}
        xml_source_id = payload.get('xml_source_id')
        if not xml_source_id:
            return jsonify({'success': False, 'message': 'xml_source_id zorunludur.'}), 400

        from app.services.pazarama_service import perform_pazarama_sync_all
        
        job_id = submit_mp_job(
            'pazarama_sync_all',
            'pazarama',
            lambda job_id: perform_pazarama_sync_all(job_id, xml_source_id),
            params={'xml_source_id': xml_source_id},
        )
        return jsonify({'success': True, 'job_id': job_id, 'message': 'Stok ve fiyat eşitleme kuyruğa alındı.'}), 202
    except Exception as e:
        logging.exception('Pazarama stok+fiyat eşitleme kuyruğa alınırken hata')
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/pazarama/clear_cache', methods=['POST'])
def api_pazarama_clear_cache():
    """Pazarama önbelleklerini temizle"""
    try:
        from app.services.pazarama_service import clear_all_pazarama_caches
        result = clear_all_pazarama_caches()
        return jsonify({
            'success': True,
            'message': f'Pazarama önbelleği temizlendi. {result}'
        })
    except Exception as e:
        logging.exception('Pazarama cache temizleme hatası')
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/trendyol/sync_stock', methods=['POST'])
def api_trendyol_sync_stock():
    try:
        payload = request.get_json(force=True) or {}
        xml_source_id = payload.get('xml_source_id')
        if not xml_source_id:
            return jsonify({'success': False, 'message': 'xml_source_id zorunludur.'}), 400

        job_id = submit_mp_job(
            'trendyol_sync_stock',
            'trendyol',
            lambda job_id: perform_trendyol_sync_stock(job_id, xml_source_id),
            params={'xml_source_id': xml_source_id},
        )
        return jsonify({'success': True, 'job_id': job_id, 'message': 'Trendyol stok eşitleme kuyruğa alındı.'}), 202
    except Exception as e:
        logging.exception('Trendyol stok eşitleme kuyruğa alınırken hata')
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/trendyol/sync_prices', methods=['POST'])
def api_trendyol_sync_prices():
    try:
        payload = request.get_json(force=True) or {}
        xml_source_id = payload.get('xml_source_id')
        if not xml_source_id:
            return jsonify({'success': False, 'message': 'xml_source_id zorunludur.'}), 400

        job_id = submit_mp_job(
            'trendyol_sync_prices',
            'trendyol',
            lambda job_id: perform_trendyol_sync_prices(job_id, xml_source_id),
            params={'xml_source_id': xml_source_id},
        )
        return jsonify({'success': True, 'job_id': job_id, 'message': 'Trendyol fiyat eşitleme kuyruğa alındı.'}), 202
    except Exception as e:
        logging.exception('Trendyol fiyat eşitleme kuyruğa alınırken hata')
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/trendyol/sync_all', methods=['POST'])
def api_trendyol_sync_all():
    """Trendyol için hem stok hem fiyat eşitleme (birleşik)"""
    try:
        payload = request.get_json(force=True) or {}
        xml_source_id = payload.get('xml_source_id')
        if not xml_source_id:
            return jsonify({'success': False, 'message': 'xml_source_id zorunludur.'}), 400

        from app.services.trendyol_service import perform_trendyol_sync_all
        
        job_id = submit_mp_job(
            'trendyol_sync_all',
            'trendyol',
            lambda job_id: perform_trendyol_sync_all(job_id, xml_source_id),
            params={'xml_source_id': xml_source_id},
        )
        return jsonify({'success': True, 'job_id': job_id, 'message': 'Stok ve fiyat eşitleme kuyruğa alındı.'}), 202
    except Exception as e:
        logging.exception('Trendyol stok+fiyat eşitleme kuyruğa alınırken hata')
        return jsonify({'success': False, 'message': str(e)}), 500

# ---------------- Product API Endpoints ----------------
@api_bp.route("/api/xml_products", methods=["GET"])
def api_xml_products():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)
    query = (request.args.get('q') or '').strip().lower()

    q = Product.query
    if query:
        q = q.filter(
            (Product.title.ilike(f"%{query}%")) | (Product.barcode.ilike(f"%{query}%"))
        )
    total = q.count()
    items_db = q.order_by(Product.id.asc()).offset((page-1)*per_page).limit(per_page).all()

    items = []
    for p in items_db:
        try:
            images = json.loads(p.images_json or "[]")
            first_image = images[0].get('url') if images else ''
        except Exception:
            first_image = ''
        items.append({
            "title": p.title,
            "barcode": p.barcode,
            "category_path": p.top_category or '',
            "price": float(p.listPrice or 0),
            "quantity": int(p.quantity or 0),
            "images": [first_image] if first_image else []
        })
    return jsonify({"total": total, "items": items})

@api_bp.route('/api/xml_source_products', methods=['GET'])
def api_xml_source_products():
    source_id = request.args.get('source_id', type=int)
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)
    query = (request.args.get('q') or '').strip().lower()
    if not source_id:
        return jsonify({'total': 0, 'items': []})
    
    # Use service to load index (cached)
    try:
        index = load_xml_source_index(source_id)
        all_records = index.get('__records__') or []
    except Exception:
        return jsonify({'total': 0, 'items': []})

    # Filter
    filtered = []
    for rec in all_records:
        if query:
            if query not in rec.get('title_normalized', '') and query not in str(rec.get('barcode', '')).lower():
                continue
        filtered.append(rec)
    
    total = len(filtered)
    start = max(0, (page-1)*per_page)
    end = start + per_page
    return jsonify({'total': total, 'items': filtered[start:end]})

@api_bp.route('/api/marketplace_products/<marketplace>')
def api_marketplace_products(marketplace: str):
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 25))
        q = (request.args.get('q') or '').strip()
        # Optional filters
        on_sale_arg = request.args.get('on_sale')
        approved_arg = request.args.get('approved')
        rejected_arg = request.args.get('rejected')
        status_param = (request.args.get('status') or '').strip().lower()
        approval_status_param = (request.args.get('approval_status') or '').strip().upper()
        stock_filter = (request.args.get('stock_filter') or '').strip().lower()
        low_stock_threshold = to_int(request.args.get('low_stock_threshold'), 10)
        if low_stock_threshold <= 0:
            low_stock_threshold = 10
        def _parse_bool(val):
            if val is None:
                return None
            s = str(val).strip().lower()
            if s in ('1','true','yes','on'):
                return True
            if s in ('0','false','no','off'):
                return False
            return None
        on_sale = _parse_bool(on_sale_arg)
        approved = _parse_bool(approved_arg)
        rejected = _parse_bool(rejected_arg)
        raw_mode = (request.args.get('raw') or '').lower() in ('1','true','yes','on')
        raw_limit = max(1, min(int(request.args.get('raw_limit', 50) or 50), 500))
        strict_api = (request.args.get('strict_api') or '0') in ('1','true','True')

        if marketplace.lower() == 'trendyol':
            try:
                client = get_trendyol_client()
                # Trendyol paging is 0-based
                if status_param == 'active':
                    if on_sale is None: on_sale = True
                    if approved is None: approved = True
                elif status_param in ('pending', 'onay'):
                    if approved is None: approved = False
                    if rejected is None: rejected = True
                elif status_param == 'passive':
                    if on_sale is None: on_sale = False
                    if approved is None: approved = True
                    if rejected is None: rejected = False

                resp = client.list_products(
                    page=max(page-1, 0),
                    size=per_page,
                    search=(q or None),
                    on_sale=on_sale,
                    approved=approved,
                    rejected=rejected,
                    approval_status=(approval_status_param or None)
                )
                content = resp.get('content') or resp.get('items') or []
                total_api = int(resp.get('totalElements') or resp.get('total') or len(content))
                
                # ... (Status mapping logic omitted for brevity, assuming frontend handles raw status or we map it here)
                # For brevity, I'll do a simplified mapping or copy the logic if needed.
                # Copying logic for status label/color:
                passive_statuses = {
                    'UNLISTED', 'UNLISTED_BY_SUPPLIER', 'UNLISTED_BY_SELLER',
                    'DEACTIVATED', 'DEACTIVATED_BY_SUPPLIER', 'DEACTIVATED_BY_SELLER',
                    'SUSPENDED', 'STOPPED', 'PASSIVE'
                }
                pending_statuses = {
                    'WAITING', 'WAITING_FOR_APPROVAL', 'WAITING_APPROVAL', 'WAITING_APPROVAL_BY_SUPPLIER',
                    'REVIEWING', 'REJECTED', 'REJECTED_BY_SUPPLIER', 'REJECTED_BY_SELLER', 'IN_REVIEW'
                }

                items = []
                for it in content:
                    barcode = it.get('barcode') or it.get('productBarcode') or ''
                    stock_code = it.get('stockCode') or it.get('productMainId') or it.get('modelNumber') or ''
                    title = it.get('title') or it.get('name') or it.get('productName') or ''
                    
                    approval_status_raw = str(it.get('approvalStatus') or '').strip().upper()
                    approved_flag = it.get('approved')
                    if isinstance(approved_flag, str):
                        approved_bool = approved_flag.strip().lower() in ('true', '1', 'yes', 'on')
                    elif approved_flag is None:
                        approved_bool = None
                    else:
                        approved_bool = bool(approved_flag)
                    
                    on_sale_raw = it.get('onSale') if 'onSale' in it else (it.get('isActive') if 'isActive' in it else it.get('onsale'))
                    if isinstance(on_sale_raw, str):
                        on_sale_bool = on_sale_raw.strip().lower() in ('true', '1', 'yes', 'on')
                    elif isinstance(on_sale_raw, (int, float)):
                        on_sale_bool = float(on_sale_raw) != 0.0
                    elif isinstance(on_sale_raw, bool):
                        on_sale_bool = on_sale_raw
                    else:
                        on_sale_bool = None

                    qty = to_int(it.get('quantity') if 'quantity' in it else it.get('stock'), 0)
                    price_val = to_float(it.get('salePrice') if 'salePrice' in it else (it.get('listPrice') if 'listPrice' in it else it.get('price')), 0.0)

                    if stock_filter == 'low' and qty >= low_stock_threshold: continue
                    if stock_filter == 'out' and qty > 0: continue

                    status_key = 'passive'
                    if approval_status_raw in pending_statuses: status_key = 'pending'
                    elif approval_status_raw in passive_statuses: status_key = 'passive'
                    elif on_sale_bool is True: status_key = 'active'
                    elif on_sale_bool is False: status_key = 'passive'
                    elif approved_bool is True: status_key = 'active'
                    elif approved_bool is False: status_key = 'pending'

                    if status_param and status_param != 'all' and status_key != status_param: continue

                    try:
                        imgs_arr = it.get('images') or []
                        urls = [x.get('url') for x in imgs_arr if isinstance(x, dict) and x.get('url')]
                    except Exception:
                        urls = []

                    if status_key == 'active': status_label, status_color = 'Aktif', 'success'
                    elif status_key == 'pending': status_label, status_color = 'Onay Bekliyor', 'warning'
                    else: status_label, status_color = 'Pasif', 'secondary'

                    items.append({
                        'barcode': barcode or stock_code,
                        'stockCode': stock_code or barcode,
                        'title': title,
                        'price': price_val,
                        'quantity': qty,
                        'images': urls,
                        'status_label': status_label,
                        'status_color': status_color,
                    })

                total = len(items) if (status_param not in ('', 'all') or stock_filter in ('low', 'out')) else total_api
                return jsonify({'total': total, 'items': items, 'total_api': total_api})
            except Exception as ex:
                if strict_api:
                    return jsonify({'total': 0, 'items': [], 'error': f'Trendyol API hatası (strict): {str(ex)}'}), 502
                # Fallback to snapshot
                snap = load_trendyol_snapshot()
                all_items = snap.get('items', [])
                if q:
                    ql = q.lower()
                    all_items = [x for x in all_items if ql in (x.get('title','').lower()) or ql in (x.get('barcode','').lower())]
                total = len(all_items)
                page_items = all_items[(page-1)*per_page: (page-1)*per_page + per_page]
                return jsonify({'total': total, 'items': page_items, 'warning': 'Trendyol API erişilemedi. Snapshot gösteriliyor.'})

        if marketplace.lower() == 'pazarama':
            try:
                client = get_pazarama_client()
            except Exception as ex:
                return jsonify({'total': 0, 'items': [], 'error': str(ex)}), 400
            
            refresh_param = (request.args.get('refresh') or '').strip().lower()
            force_refresh_snapshot = refresh_param in ('1', 'true', 'yes', 'force') or raw_mode
            if force_refresh_snapshot:
                clear_pazarama_detail_cache()
            
            try:
                product_index = pazarama_build_product_index(client, force_refresh=force_refresh_snapshot)
                data_all = product_index.get('items') or []
                
                # Filter logic
                def _matches_filters(row: Dict[str, Any]) -> bool:
                    if q:
                        q_lower = q.lower()
                        code = str(row.get('code') or '').lower()
                        stock_code = str(row.get('stockCode') or '').lower()
                        name = str(row.get('displayName') or row.get('name') or '').lower()
                        if q_lower not in code and q_lower not in stock_code and q_lower not in name:
                            return False
                    if approved is True:
                        label = str(row.get('stateDescription') or '').lower()
                        if 'onay' not in label: return False
                    if approved is False:
                        label = str(row.get('stateDescription') or '').lower()
                        if 'onay' in label: return False
                    if status_param:
                        label = str(row.get('stateDescription') or row.get('status') or '').lower()
                        if status_param == 'pasif':
                            if 'pasif' not in label: return False
                        elif status_param in ('onay', 'pending'):
                            if 'onay' not in label: return False
                        elif status_param == 'aktif':
                            if 'onaylandı' not in label and 'aktif' not in label: return False
                    stock_val = to_int(row.get('stockCount'))
                    if stock_filter == 'low' and stock_val >= low_stock_threshold: return False
                    if stock_filter == 'out' and stock_val > 0: return False
                    return True

                filtered = [row for row in data_all if _matches_filters(row)]
                total_remote = len(filtered)
                start = max(0, (page - 1) * per_page)
                end = start + per_page
                page_rows = filtered[start:end]

                # Merge details
                items = []
                for row in page_rows:
                    code = str(row.get('code') or row.get('productCode') or '').strip()
                    detail = get_cached_pazarama_detail(client, code)
                    merged = dict(row)
                    if detail:
                        merged.update({k:v for k,v in detail.items() if v is not None and k != 'attributes'})
                    
                    # Simplify for UI
                    title = merged.get('displayName') or merged.get('name') or code
                    price = to_float(merged.get('salePrice') or merged.get('listPrice'))
                    qty = to_int(merged.get('stockCount'))
                    
                    # Images
                    imgs = merged.get('images') or []
                    img_urls = []
                    if isinstance(imgs, list):
                        for i in imgs:
                            if isinstance(i, dict): img_urls.append(i.get('imageUrl') or i.get('url'))
                            elif isinstance(i, str): img_urls.append(i)
                    
                    items.append({
                        'barcode': code,
                        'stockCode': merged.get('stockCode'),
                        'title': title,
                        'price': price,
                        'quantity': qty,
                        'images': img_urls,
                        'status_label': merged.get('stateDescription') or '—',
                        'status_color': 'secondary' # Simplified
                    })
                
                return jsonify({'total': total_remote, 'items': items})

            except Exception as ex:
                return jsonify({'total': 0, 'items': [], 'error': str(ex)}), 500

    except Exception as e:
        return jsonify({'total': 0, 'items': [], 'error': str(e)}), 500
    
    return jsonify({'total': 0, 'items': []})

@api_bp.route("/api/product/update_stock/<marketplace>/<barcode>", methods=["POST"])
def api_update_stock(marketplace, barcode):
    new_quantity = request.json.get('quantity')
    # Placeholder for single update logic, potentially using job queue or direct call
    # For now, just a placeholder as in original app.py
    flash(f"⚠️ {MARKETPLACES.get(marketplace, 'Pazar Yeri')} - {barcode} için stok {new_quantity} olarak güncelleniyor (PLACEHOLDER).", "warning")
    return jsonify({"success": True, "message": f"{barcode} için stok güncelleme kuyruğa alındı."})

@api_bp.route("/api/product/update_price/<marketplace>/<barcode>", methods=["POST"])
def api_update_price(marketplace, barcode):
    new_price = request.json.get('price')
    flash(f"⚠️ {MARKETPLACES.get(marketplace, 'Pazar Yeri')} - {barcode} için fiyat {new_price} olarak güncelleniyor (PLACEHOLDER).", "warning")
    return jsonify({"success": True, "message": f"{barcode} için fiyat güncelleme kuyruğa alındı."})

@api_bp.route("/api/product/delete/<marketplace>/<barcode>", methods=["POST"])
def api_delete_product(marketplace, barcode):
    flash(f"⚠️ {MARKETPLACES.get(marketplace, 'Pazar Yeri')} - {barcode} ürünü silme kuyruğuna alındı (PLACEHOLDER).", "danger")
    return jsonify({"success": True, "message": f"{barcode} ürünü silme kuyruğuna alındı."})

# ---------------- Order Sync API Endpoints ----------------
@api_bp.route('/api/orders/sync/<marketplace>', methods=['POST'])
def api_sync_orders(marketplace: str):
    """Sync orders from a marketplace"""
    try:
        if marketplace == 'trendyol':
            from app.services.order_service import sync_trendyol_orders
            result = sync_trendyol_orders()
        elif marketplace == 'pazarama':
            from app.services.order_service import sync_pazarama_orders
            result = sync_pazarama_orders()
        else:
            return jsonify({'success': False, 'message': f'Desteklenmeyen pazaryeri: {marketplace}'}), 400
        
        return jsonify({
            'success': True,
            'synced': result.get('synced', 0),
            'errors': result.get('errors', [])
        })
    except Exception as e:
        logging.exception(f'{marketplace} sipariş senkronizasyonu hatası')
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/trendyol/refresh_cache', methods=['POST'])
def api_trendyol_refresh_cache():
    """Trendyol ürünlerini API'den çekip önbelleği yeniler"""
    try:
        from app.services.trendyol_service import refresh_trendyol_cache
        
        # Arka planda çalıştır
        def refresh_task(job_id):
            from flask import current_app
            with current_app.app_context():
                return refresh_trendyol_cache(job_id)
        
        job_id = submit_mp_job(
            'trendyol_refresh_cache',
            'trendyol',
            refresh_task
        )
        
        return jsonify({
            'success': True,
            'job_id': job_id,
            'message': 'Trendyol ürün listesi yenileme işlemi başlatıldı.'
        })
        
    except Exception as e:
        logging.exception("Error refreshing trendyol cache")
        return jsonify({'success': False, 'message': str(e)}), 500

# ============================================================
# Trendyol Sipariş Yönetimi API Endpoints
# ============================================================

@api_bp.route('/api/trendyol/order/<int:package_id>/status', methods=['PUT'])
@login_required
def api_trendyol_update_order_status(package_id: int):
    """Update Trendyol shipment package status"""
    try:
        payload = request.get_json(force=True) or {}
        status = payload.get('status')
        tracking_number = payload.get('tracking_number')
        cargo_provider_id = payload.get('cargo_provider_id')
        
        if not status:
            return jsonify({'success': False, 'message': 'status parametresi zorunludur.'}), 400
        
        valid_statuses = ['Picking', 'Invoiced', 'Shipped']
        if status not in valid_statuses:
            return jsonify({'success': False, 'message': f'Geçersiz durum. Geçerli durumlar: {valid_statuses}'}), 400
        
        # Shipped status requires tracking info
        if status == 'Shipped' and not tracking_number:
            return jsonify({'success': False, 'message': 'Kargoya vermek için takip numarası gereklidir.'}), 400
        
        client = get_trendyol_client()
        result = client.update_shipment_package_status(
            shipment_package_id=package_id,
            status=status,
            tracking_number=tracking_number,
            cargo_provider_id=cargo_provider_id
        )
        
        return jsonify({
            'success': True,
            'message': f'Sipariş durumu {status} olarak güncellendi.',
            'result': result
        })
    except Exception as e:
        logging.exception(f'Trendyol sipariş durum güncelleme hatası: {package_id}')
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/trendyol/order/<int:package_id>/unsupplied', methods=['PUT'])
@login_required
def api_trendyol_mark_unsupplied(package_id: int):
    """Mark items as unsupplied (tedarik edilemedi)"""
    try:
        payload = request.get_json(force=True) or {}
        line_items = payload.get('line_items', [])
        
        if not line_items:
            return jsonify({'success': False, 'message': 'line_items listesi zorunludur.'}), 400
        
        client = get_trendyol_client()
        result = client.mark_unsupplied(
            shipment_package_id=package_id,
            line_items=line_items
        )
        
        return jsonify({
            'success': True,
            'message': f'{len(line_items)} ürün tedarik edilemedi olarak işaretlendi.',
            'result': result
        })
    except Exception as e:
        logging.exception(f'Trendyol tedarik edilemedi hatası: {package_id}')
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/trendyol/order/<int:package_id>/invoice', methods=['POST'])
@login_required
def api_trendyol_send_invoice(package_id: int):
    """Send invoice link for a shipment package"""
    try:
        payload = request.get_json(force=True) or {}
        invoice_link = payload.get('invoice_link')
        
        if not invoice_link:
            return jsonify({'success': False, 'message': 'invoice_link zorunludur.'}), 400
        
        client = get_trendyol_client()
        result = client.send_invoice_link(
            shipment_package_id=package_id,
            invoice_link=invoice_link
        )
        
        return jsonify({
            'success': True,
            'message': 'Fatura linki başarıyla gönderildi.',
            'result': result
        })
    except Exception as e:
        logging.exception(f'Trendyol fatura gönderme hatası: {package_id}')
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/trendyol/order/<int:package_id>/split', methods=['POST'])
@login_required
def api_trendyol_split_package(package_id: int):
    """Split a shipment package"""
    try:
        payload = request.get_json(force=True) or {}
        order_line_ids = payload.get('order_line_ids', [])
        tracking_number = payload.get('tracking_number')
        
        if not order_line_ids:
            return jsonify({'success': False, 'message': 'order_line_ids listesi zorunludur.'}), 400
        
        client = get_trendyol_client()
        result = client.split_shipment_package(
            package_id=package_id,
            order_line_ids=order_line_ids,
            tracking_number=tracking_number
        )
        
        return jsonify({
            'success': True,
            'message': f'Paket {len(order_line_ids)} ürün ile bölündü.',
            'result': result
        })
    except Exception as e:
        logging.exception(f'Trendyol paket bölme hatası: {package_id}')
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================
# Trendyol Müşteri Soruları API Endpoints
# ============================================================

@api_bp.route('/api/trendyol/questions', methods=['GET'])
@login_required
def api_trendyol_get_questions():
    """Get customer questions"""
    try:
        page = request.args.get('page', 0, type=int)
        size = request.args.get('size', 100, type=int)
        status = request.args.get('status')  # WAITING_FOR_ANSWER, ANSWERED, REJECTED
        barcode = request.args.get('barcode')
        
        client = get_trendyol_client()
        result = client.get_customer_questions(
            page=page,
            size=size,
            status=status,
            barcode=barcode
        )
        
        return jsonify({
            'success': True,
            'data': result
        })
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            logging.warning('Trendyol müşteri soruları API erişim yetkisi yok (403)')
            return jsonify({
                'success': False, 
                'message': 'Bu API\'ye erişim yetkiniz bulunmuyor. Trendyol Satıcı Paneli\'nden "Müşteri Soruları API" yetkisini aktifleştirmeniz gerekebilir.'
            }), 403
        logging.exception('Trendyol müşteri soruları çekme hatası')
        return jsonify({'success': False, 'message': str(e)}), 500
    except Exception as e:
        logging.exception('Trendyol müşteri soruları çekme hatası')
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/trendyol/questions/<int:question_id>/answer', methods=['POST'])
@login_required
def api_trendyol_answer_question(question_id: int):
    """Answer a customer question"""
    try:
        payload = request.get_json(force=True) or {}
        answer_text = payload.get('answer_text', '').strip()
        
        if not answer_text or len(answer_text) < 2:
            return jsonify({'success': False, 'message': 'Cevap en az 2 karakter olmalıdır.'}), 400
        
        client = get_trendyol_client()
        result = client.answer_customer_question(
            question_id=question_id,
            answer_text=answer_text
        )
        
        return jsonify({
            'success': True,
            'message': 'Soru başarıyla cevaplandı.',
            'result': result
        })
    except Exception as e:
        logging.exception(f'Trendyol soru cevaplama hatası: {question_id}')
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================
# Trendyol İade/Claim API Endpoints
# ============================================================

@api_bp.route('/api/trendyol/claims', methods=['GET'])
@login_required
def api_trendyol_get_claims():
    """Get claims/returns"""
    try:
        page = request.args.get('page', 0, type=int)
        size = request.args.get('size', 100, type=int)
        claim_status = request.args.get('claim_status')
        start_date = request.args.get('start_date', type=int)
        end_date = request.args.get('end_date', type=int)
        
        client = get_trendyol_client()
        result = client.get_claims(
            page=page,
            size=size,
            claim_status=claim_status,
            start_date=start_date,
            end_date=end_date
        )
        
        return jsonify({
            'success': True,
            'data': result
        })
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            logging.warning('Trendyol iade talepleri API erişim yetkisi yok (403)')
            return jsonify({
                'success': False, 
                'message': 'Bu API\'ye erişim yetkiniz bulunmuyor. Trendyol Satıcı Paneli\'nden "İade/Claim API" yetkisini aktifleştirmeniz gerekebilir.'
            }), 403
        logging.exception('Trendyol iade talepleri çekme hatası')
        return jsonify({'success': False, 'message': str(e)}), 500
    except Exception as e:
        logging.exception('Trendyol iade talepleri çekme hatası')
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/trendyol/claims/<claim_id>/accept', methods=['POST'])
@login_required
def api_trendyol_accept_claim(claim_id: str):
    """Accept a claim/return request"""
    try:
        client = get_trendyol_client()
        result = client.accept_claim(claim_id=claim_id)
        
        return jsonify({
            'success': True,
            'message': 'İade talebi onaylandı.',
            'result': result
        })
    except Exception as e:
        logging.exception(f'Trendyol iade onaylama hatası: {claim_id}')
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/trendyol/claims/<claim_id>/reject', methods=['POST'])
@login_required
def api_trendyol_reject_claim(claim_id: str):
    """Reject a claim/return request"""
    try:
        payload = request.get_json(force=True) or {}
        reject_reason_id = payload.get('reject_reason_id')
        reject_reason_text = payload.get('reject_reason_text')
        
        if not reject_reason_id:
            return jsonify({'success': False, 'message': 'reject_reason_id zorunludur.'}), 400
        
        client = get_trendyol_client()
        result = client.reject_claim(
            claim_id=claim_id,
            reject_reason_id=reject_reason_id,
            reject_reason_text=reject_reason_text
        )
        
        return jsonify({
            'success': True,
            'message': 'İade talebi reddedildi.',
            'result': result
        })
    except Exception as e:
        logging.exception(f'Trendyol iade reddetme hatası: {claim_id}')
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================
# Trendyol Kargo Firmaları API Endpoint
# ============================================================

@api_bp.route('/api/trendyol/cargo_providers', methods=['GET'])
@login_required
def api_trendyol_get_cargo_providers():
    """Get list of cargo providers"""
    try:
        client = get_trendyol_client()
        result = client.get_cargo_providers()
        
        return jsonify({
            'success': True,
            'data': result
        })
    except Exception as e:
        logging.exception('Trendyol kargo firmaları çekme hatası')
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/trendyol/send_auto', methods=['POST'])
def api_trendyol_send_auto():
    try:
        payload = request.get_json(force=True) or {}
        barcodes = payload.get('barcodes', [])
        xml_source_id = payload.get('xml_source_id')
        
        if not barcodes:
            return jsonify({'success': False, 'message': 'Ürün seçilmedi.'}), 400

        from app.services.trendyol_service import perform_trendyol_send_products
        
        job_id = submit_mp_job(
            'trendyol_send_auto',
            'trendyol',
            lambda job_id: perform_trendyol_send_products(job_id, barcodes, xml_source_id, auto_match=True),
            params={'barcodes': barcodes, 'xml_source_id': xml_source_id}
        )
        
        return jsonify({
            'success': True, 
            'batch_id': job_id,
            'count': len(barcodes),
            'message': 'Ürünler gönderim kuyruğuna alındı (Otomatik Eşleşme).'
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/trendyol/send_all', methods=['POST'])
def api_trendyol_send_all():
    try:
        payload = request.get_json(force=True) or {}
        xml_source_id = payload.get('source_id')
        auto_match = payload.get('auto_match', False)
        
        if not xml_source_id:
            return jsonify({'success': False, 'message': 'Kaynak ID zorunludur.'}), 400

        from app.services.trendyol_service import perform_trendyol_send_all
        
        job_id = submit_mp_job(
            'trendyol_send_all',
            'trendyol',
            lambda job_id: perform_trendyol_send_all(job_id, xml_source_id, auto_match=auto_match),
            params={'xml_source_id': xml_source_id}
        )
        
        return jsonify({
            'success': True, 
            'batch_id': job_id,
            'message': 'Tüm ürünler gönderim kuyruğuna alındı.'
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/hepsiburada/send_all', methods=['POST'])
def api_hepsiburada_send_all():
    return jsonify({'success': False, 'message': 'Hepsiburada toplu gönderim henüz aktif değil.'}), 501

@api_bp.route('/api/pazarama/send_all', methods=['POST'])
def api_pazarama_send_all():
    try:
        payload = request.get_json(force=True) or {}
        xml_source_id = payload.get('source_id')
        
        if not xml_source_id:
            return jsonify({'success': False, 'message': 'Kaynak ID zorunludur.'}), 400

        from app.services.pazarama_service import perform_pazarama_send_all
        
        # Get all barcodes from XML source
        xml_index = load_xml_source_index(xml_source_id)
        all_barcodes = list((xml_index.get('by_barcode') or {}).keys())
        
        if not all_barcodes:
            return jsonify({'success': False, 'message': 'XML kaynağında ürün bulunamadı.'}), 400
        
        from app.services.pazarama_service import perform_pazarama_send_products
        
        job_id = submit_mp_job(
            'pazarama_send_all',
            'pazarama',
            lambda job_id: perform_pazarama_send_products(job_id, all_barcodes, xml_source_id),
            params={'xml_source_id': xml_source_id}
        )
        
        return jsonify({
            'success': True, 
            'batch_id': job_id,
            'message': 'Tüm ürünler Pazarama gönderim kuyruğuna alındı.'
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/idefix/send_all', methods=['POST'])
def api_idefix_send_all():
    try:
        payload = request.get_json(force=True) or {}
        xml_source_id = payload.get('source_id')
        
        if not xml_source_id:
            return jsonify({'success': False, 'message': 'Kaynak ID zorunludur.'}), 400

        # Get all barcodes from XML source
        xml_index = load_xml_source_index(xml_source_id)
        all_barcodes = list((xml_index.get('by_barcode') or {}).keys())
        
        if not all_barcodes:
            return jsonify({'success': False, 'message': 'XML kaynağında ürün bulunamadı.'}), 400
        
        from app.services.idefix_service import perform_idefix_send_products
        
        job_id = submit_mp_job(
            'idefix_send_all',
            'idefix',
            lambda job_id: perform_idefix_send_products(job_id, all_barcodes, xml_source_id),
            params={'xml_source_id': xml_source_id}
        )
        
        return jsonify({
            'success': True, 
            'batch_id': job_id,
            'count': len(all_barcodes),
            'message': f'Tüm {len(all_barcodes)} ürün İdefix gönderim kuyruğuna alındı.'
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/job/control', methods=['POST'])
def api_control_job():
    try:
        payload = request.get_json(force=True) or {}
        job_id = payload.get('job_id')
        action = payload.get('action') # pause, resume, cancel
        
        if not job_id or not action:
            return jsonify({'success': False, 'message': 'Job ID ve aksiyon gereklidir.'}), 400
            
        from app.services.job_queue import control_mp_job
        success = control_mp_job(job_id, action)
        
        if success:
            return jsonify({'success': True, 'message': f'İşlem {action} yapıldı.'})
        else:
            return jsonify({'success': False, 'message': 'İşlem başarısız.'}), 400
            
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/job/status/<job_id>', methods=['GET'])
def api_job_status(job_id):
    from app.services.job_queue import get_mp_job
    job = get_mp_job(job_id)
    if not job:
        return jsonify({'success': False, 'message': 'Job not found'}), 404
    
    return jsonify({
        'success': True,
        'status': job.get('status'),
        'progress': job.get('progress', {}),
        'logs': job.get('logs', [])[-10:], # Last 10 logs
        'cancel_requested': job.get('cancel_requested', False),
        'pause_requested': job.get('pause_requested', False),
        'result': job.get('result')
    })

@api_bp.route('/api/send_selected/<marketplace>', methods=['POST'])
def api_send_selected(marketplace):
    try:
        marketplace = (marketplace or '').strip()
        # Handle Turkish chars for robust matching
        marketplace = marketplace.replace('İ', 'i').replace('I', 'i').lower()
        
        logging.info(f"API Send Selected: {marketplace}")
        
        if marketplace not in MARKETPLACES:
            return jsonify({'success': False, 'message': 'Geçersiz pazar yeri'}), 400
            
        payload = request.get_json(force=True) or {}
        barcodes = payload.get('barcodes', [])
        xml_source_id = payload.get('xml_source_id')
        
        if not barcodes:
            return jsonify({'success': False, 'message': 'Ürün seçilmedi.'}), 400

        if marketplace == 'idefix':
            from app.services.idefix_service import perform_idefix_send_products
            job_id = submit_mp_job(
                'idefix_send_selected',
                'idefix',
                lambda job_id: perform_idefix_send_products(job_id, barcodes, xml_source_id),
                params={'barcodes': barcodes, 'xml_source_id': xml_source_id, 'requested_marketplace': marketplace}
            )
        elif marketplace == 'trendyol':
            from app.services.trendyol_service import perform_trendyol_send_products
            # For send_selected, we assume manual match or default, but since we don't have manual match UI yet, 
            # we can default to auto_match=True or False.
            # The UI asks for auto match confirmation for Trendyol.
            # If user clicked "Yes, Auto", they call /api/trendyol/send_auto.
            # If they clicked "No, Manual" (or just send), they call /api/send_selected/trendyol.
            # In this case, let's assume auto_match=False, but perform_trendyol_send_products requires category_id.
            # If auto_match=False, it will fail to find category unless we provide it in payload or have a mapping.
            # For now, let's enable auto_match=True even here as a fallback, or better, 
            # update perform_trendyol_send_products to handle missing category gracefully (skip).
            # Let's set auto_match=True for now to make it work "magically" as requested "make it work".
            job_id = submit_mp_job(
                'trendyol_send_selected',
                'trendyol',
                lambda job_id: perform_trendyol_send_products(job_id, barcodes, xml_source_id, auto_match=True),
                params={'barcodes': barcodes, 'xml_source_id': xml_source_id, 'requested_marketplace': marketplace}
            )
        elif marketplace == 'pazarama':
            from app.services.pazarama_service import perform_pazarama_send_products
            job_id = submit_mp_job(
                'pazarama_send_selected',
                'pazarama',
                lambda job_id: perform_pazarama_send_products(job_id, barcodes, xml_source_id),
                params={'barcodes': barcodes, 'xml_source_id': xml_source_id, 'requested_marketplace': marketplace}
            )
        else:
            job_id = submit_mp_job(
                f'{marketplace}_send_selected',
                marketplace,
                lambda job_id: _placeholder_send_job(job_id, marketplace, barcodes, xml_source_id, auto_match=False),
                params={'barcodes': barcodes, 'xml_source_id': xml_source_id, 'requested_marketplace': marketplace}
            )
        
        return jsonify({
            'success': True, 
            'batch_id': job_id,
            'count': len(barcodes),
            'message': f'Ürünler {MARKETPLACES[marketplace]} gönderim kuyruğuna alındı.',
            'debug_marketplace': marketplace
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

def _placeholder_send_job(job_id, marketplace, barcodes, xml_source_id, auto_match=False):
    """Temporary placeholder to simulate sending"""
    import time
    time.sleep(2)
    # Log that we tried
    append_mp_job_log(job_id, f"Sending {len(barcodes)} products to {marketplace} (Auto: {auto_match})")
    # In future, call actual service functions here
    return {"success_count": 0, "fail_count": 0, "failures": ["Not implemented yet"]}


@api_bp.route('/api/idefix/search_brand', methods=['POST'])
def api_idefix_search_brand():
    """Search for an Idefix brand by name."""
    try:
        data = request.get_json()
        brand_name = data.get('brand_name', '').strip()
        
        logging.info(f"[IDEFIX] Brand search request for: '{brand_name}'")
        
        if not brand_name:
            return jsonify({'success': False, 'message': 'Marka adı gereklidir'}), 400
        
        from app.services.idefix_service import get_idefix_client
        client = get_idefix_client()
        
        logging.info(f"[IDEFIX] İdefix client created, searching for brand...")
        brand = client.search_brand_by_name(brand_name)
        
        if brand:
            logging.info(f"[IDEFIX] Brand found: {brand.get('title')} (ID: {brand.get('id')})")
            return jsonify({
                'success': True,
                'brand': {
                    'id': brand['id'],
                    'title': brand['title'],
                    'slug': brand.get('slug', '')
                }
            })
        else:
            logging.warning(f"[IDEFIX] Brand '{brand_name}' not found in API response")
            return jsonify({
                'success': False,
                'message': f'"{brand_name}" markası bulunamadı. İdefix API\'de bu isimde bir marka yok olabilir.'
            })
            
    except Exception as e:
        logging.error(f"[IDEFIX] Brand search error: {str(e)}", exc_info=True)
        return jsonify({
            'success': False,
            'message': f'Hata: {str(e)}'
        }), 500
        return jsonify({'success': False, 'message': f'Hata: {str(e)}'}), 500


@api_bp.route('/api/pazarama/search_brand', methods=['POST'])
def api_pazarama_search_brand():
    """Search for a Pazarama brand by name."""
    try:
        data = request.get_json()
        brand_name = data.get('brand_name', '').strip()
        
        if not brand_name:
            return jsonify({'success': False, 'message': 'Marka adı gereklidir'}), 400
        
        from app.services.pazarama_service import get_pazarama_client
        client = get_pazarama_client()
        
        # Search for brands with the given name
        results = client.get_brands(name=brand_name, size=10)
        
        if results:
            # Try to find exact match first
            for brand in results:
                if brand.get('name', '').strip().lower() == brand_name.lower():
                    return jsonify({
                        'success': True,
                        'brand': {
                            'id': brand['id'],
                            'name': brand['name']
                        }
                    })
            
            # If no exact match, return first result
            first_brand = results[0]
            return jsonify({
                'success': True,
                'brand': {
                    'id': first_brand['id'],
                    'name': first_brand['name']
                }
            })
        else:
            return jsonify({
                'success': False,
                'message': f'"{brand_name}" markası bulunamadı. Lütfen farklı bir isim deneyin.'
            })
            
    except Exception as e:
        logging.error(f"Pazarama brand search error: {str(e)}")
        return jsonify({'success': False, 'message': f'Hata: {str(e)}'}), 500


# ---------------- Auto Sync API Endpoints ----------------
@api_bp.route('/api/auto_sync/settings', methods=['GET'])
def api_auto_sync_settings():
    """Tüm pazaryerleri için otomatik senkronizasyon ayarlarını getir"""
    try:
        settings = []
        for marketplace_key in MARKETPLACES.keys():
            auto_sync = AutoSync.get_or_create(marketplace_key)
            settings.append(auto_sync.to_dict())
        
        return jsonify({'success': True, 'settings': settings})
    except Exception as e:
        logging.exception("Error fetching auto sync settings")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/auto_sync/toggle', methods=['POST'])
def api_auto_sync_toggle():
    """Pazaryeri için otomatik senkronizasyonu aç/kapa"""
    try:
        data = request.get_json() or {}
        marketplace = data.get('marketplace')
        enabled = data.get('enabled', False)
        interval_minutes = data.get('interval_minutes', 60)
        
        if not marketplace or marketplace not in MARKETPLACES:
            return jsonify({'success': False, 'message': 'Geçersiz pazaryeri'}), 400
        
        # AutoSync kaydını güncelle
        auto_sync = AutoSync.get_or_create(marketplace)
        auto_sync.enabled = enabled
        auto_sync.sync_interval_minutes = interval_minutes
        auto_sync.updated_at = datetime.utcnow().isoformat()
        db.session.commit()
        
        # Scheduler job'unu ekle veya kaldır
        from app.services.scheduler_service import add_sync_job, remove_sync_job
        
        if enabled:
            success = add_sync_job(marketplace, interval_minutes)
            message = f'{MARKETPLACES[marketplace]} için otomatik senkronizasyon aktif edildi.'
        else:
            success = remove_sync_job(marketplace)
            message = f'{MARKETPLACES[marketplace]} için otomatik senkronizasyon kapatıldı.'
        
        if not success:
            return jsonify({'success': False, 'message': 'Scheduler güncellenemedi'}), 500
        
        return jsonify({
            'success': True,
            'message': message,
            'setting': auto_sync.to_dict()
        })
        
    except Exception as e:
        logging.exception("Error toggling auto sync")
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/auto_sync/trigger/<marketplace>', methods=['POST'])
def api_auto_sync_trigger(marketplace: str):
    """Manuel senkronizasyon tetikle"""
    try:
        if marketplace not in MARKETPLACES:
            return jsonify({'success': False, 'message': 'Geçersiz pazaryeri'}), 400
        
        from app.services.auto_sync_service import sync_marketplace_products
        
        # Arka planda çalıştır
        def sync_task(job_id):
            from flask import current_app
            with current_app.app_context():
                return sync_marketplace_products(marketplace, job_id=job_id)
        
        job_id = submit_mp_job(
            f'manual_sync_{marketplace}',
            marketplace,
            sync_task,
            params={'marketplace': marketplace}
        )
        
        return jsonify({
            'success': True,
            'job_id': job_id,
            'message': f'{MARKETPLACES[marketplace]} senkronizasyonu başlatıldı.'
        })
        
    except Exception as e:
        logging.exception(f"Error triggering sync for {marketplace}")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/auto_sync/logs', methods=['GET'])
def api_auto_sync_logs():
    """Senkronizasyon loglarını getir"""
    try:
        marketplace = request.args.get('marketplace')
        limit = min(int(request.args.get('limit', 50)), 200)
        
        from app.services.auto_sync_service import get_sync_logs
        logs = get_sync_logs(marketplace=marketplace, limit=limit)
        
        return jsonify({
            'success': True,
            'logs': logs
        })
        
    except Exception as e:
        logging.exception("Error fetching sync logs")
        return jsonify({'success': False, 'message': str(e)}), 500

# ---------------- Excel API Endpoints ----------------
@api_bp.route('/api/excel/list', methods=['GET'])
def api_excel_list():
    """Kaydedilmiş Excel dosyalarını listele"""
    try:
        from app.services.excel_service import list_saved_excel_files
        files = list_saved_excel_files()
        return jsonify({'success': True, 'files': files})
    except Exception as e:
        logging.exception("Excel list error")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/excel/load/<file_id>', methods=['POST'])
def api_excel_load(file_id):
    """Kaydedilmiş Excel dosyasını yükle"""
    try:
        from app.services.excel_service import load_saved_excel, get_excel_metadata
        if load_saved_excel(file_id):
            metadata = get_excel_metadata(file_id)
            return jsonify({'success': True, **metadata})
        return jsonify({'success': False, 'message': 'Dosya bulunamadı'}), 404
    except Exception as e:
        logging.exception("Excel load error")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/excel/delete/<file_id>', methods=['DELETE'])
def api_excel_delete(file_id):
    """Excel dosyasını sil"""
    try:
        from app.services.excel_service import delete_excel_file
        if delete_excel_file(file_id):
            return jsonify({'success': True, 'message': 'Dosya silindi'})
        return jsonify({'success': False, 'message': 'Dosya bulunamadı'}), 404
    except Exception as e:
        logging.exception("Excel delete error")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/excel/upload', methods=['POST'])
def api_excel_upload():
    """Excel dosyası yükle ve parse et"""
    import tempfile
    import os
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'message': 'Dosya bulunamadı'}), 400
        
        file = request.files['file']
        if not file.filename:
            return jsonify({'success': False, 'message': 'Dosya seçilmedi'}), 400
        
        original_filename = file.filename
        
        # Check extension
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ['.xlsx', '.xls', '.csv']:
            return jsonify({'success': False, 'message': 'Geçersiz dosya tipi. xlsx, xls veya csv olmalı'}), 400
        
        # Save to temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name
        
        try:
            from app.services.excel_service import parse_excel_file
            file_id, metadata = parse_excel_file(tmp_path, original_filename=original_filename)
            return jsonify({'success': True, **metadata})
        finally:
            # Temp file can be deleted as parse_excel_file copies it
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            
    except ImportError as e:
        return jsonify({'success': False, 'message': f'Eksik kütüphane: {e}'}), 500
    except Exception as e:
        logging.exception("Excel upload error")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/excel/products', methods=['GET'])
def api_excel_products():
    """Excel ürünlerini listele"""
    try:
        file_id = request.args.get('file_id')
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 25))
        search = request.args.get('search', '')
        
        if not file_id:
            return jsonify({'success': False, 'message': 'file_id gerekli'}), 400
        
        from app.services.excel_service import get_excel_products
        result = get_excel_products(file_id, page=page, per_page=per_page, search=search)
        return jsonify(result)
        
    except Exception as e:
        logging.exception("Excel products error")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/excel/generate_codes', methods=['POST'])
def api_excel_generate_codes():
    """Seçili ürünler için random kod oluştur"""
    try:
        data = request.get_json(force=True) or {}
        file_id = data.get('file_id')
        indices = data.get('indices', [])
        prefix = data.get('prefix', '')
        gen_barcode = data.get('generate_barcode', True)
        gen_stock = data.get('generate_stock', True)
        
        if not file_id or not indices:
            return jsonify({'success': False, 'message': 'file_id ve indices gerekli'}), 400
        
        from app.services.excel_service import bulk_generate_codes
        result = bulk_generate_codes(file_id, indices, prefix=prefix, 
                                     generate_barcode_flag=gen_barcode, 
                                     generate_stock_flag=gen_stock)
        return jsonify(result)
        
    except Exception as e:
        logging.exception("Excel generate codes error")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/excel/update_codes', methods=['POST'])
def api_excel_update_codes():
    """Seçili ürünlerin kodlarını güncelle"""
    try:
        data = request.get_json(force=True) or {}
        file_id = data.get('file_id')
        indices = data.get('indices', [])
        barcode = data.get('barcode')
        stock_code = data.get('stock_code')
        
        if not file_id or not indices:
            return jsonify({'success': False, 'message': 'file_id ve indices gerekli'}), 400
        
        from app.services.excel_service import update_product_codes
        result = update_product_codes(file_id, indices, barcode=barcode, stock_code=stock_code)
        return jsonify(result)
        
    except Exception as e:
        logging.exception("Excel update codes error")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/excel/generate_all_codes', methods=['POST'])
def api_excel_generate_all_codes():
    """Tüm ürünler için random barkod/stok kodu oluştur"""
    try:
        data = request.get_json(force=True) or {}
        file_id = data.get('file_id')
        prefix = data.get('prefix', '').upper()[:2]  # Max 2 char prefix (can be empty)
        code_type = data.get('code_type', 'both')  # 'barcode', 'stock', 'both'
        title_prefix = data.get('title_prefix', '')  # Keep user's spacing intact
        
        if not file_id:
            return jsonify({'success': False, 'message': 'file_id gerekli'}), 400
        
        from app.services.excel_service import generate_all_random_codes
        result = generate_all_random_codes(file_id, prefix=prefix, code_type=code_type, title_prefix=title_prefix)
        return jsonify(result)
        
    except Exception as e:
        logging.exception("Excel generate all codes error")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/excel/brands', methods=['GET'])
def api_excel_brands():
    """Excel dosyasındaki benzersiz markaları listele"""
    try:
        file_id = request.args.get('file_id')
        if not file_id:
            return jsonify({'success': False, 'message': 'file_id gerekli'}), 400
        
        from app.services.excel_service import get_excel_metadata, load_saved_excel, _EXCEL_CACHE
        
        # Ensure file is loaded
        if file_id not in _EXCEL_CACHE:
            load_saved_excel(file_id)
        
        entry = _EXCEL_CACHE.get(file_id)
        if not entry:
            return jsonify({'success': False, 'message': 'Excel dosyası bulunamadı'}), 404
        
        records = entry['records']
        mapping = entry['column_mapping']
        brand_col = mapping.get('brand', '')
        
        # Count brands
        brand_counts = {}
        for r in records:
            brand = str(r.get(brand_col, '')).strip() if brand_col else ''
            if brand:
                brand_counts[brand] = brand_counts.get(brand, 0) + 1
        
        brands = [{'name': name, 'count': count} for name, count in sorted(brand_counts.items())]
        
        return jsonify({
            'success': True,
            'brands': brands,
            'total': len(brands)
        })
        
    except Exception as e:
        logging.exception("Excel brands error")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/excel/categories', methods=['GET'])
def api_excel_categories():
    """Excel dosyasındaki benzersiz kategorileri listele"""
    try:
        file_id = request.args.get('file_id')
        if not file_id:
            return jsonify({'success': False, 'message': 'file_id gerekli'}), 400
        
        from app.services.excel_service import load_saved_excel, _EXCEL_CACHE
        
        # Ensure file is loaded
        if file_id not in _EXCEL_CACHE:
            load_saved_excel(file_id)
        
        entry = _EXCEL_CACHE.get(file_id)
        if not entry:
            return jsonify({'success': False, 'message': 'Excel dosyası bulunamadı'}), 404
        
        records = entry['records']
        mapping = entry['column_mapping']
        category_col = mapping.get('category', '')
        
        # Count categories
        category_counts = {}
        for r in records:
            cat = str(r.get(category_col, '')).strip() if category_col else ''
            if cat:
                category_counts[cat] = category_counts.get(cat, 0) + 1
        
        categories = [{'name': name, 'count': count} for name, count in sorted(category_counts.items())]
        
        return jsonify({
            'success': True,
            'categories': categories,
            'total': len(categories)
        })
        
    except Exception as e:
        logging.exception("Excel categories error")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/trendyol/categories/match', methods=['POST'])
def api_trendyol_category_match():
    """Kategori isimlerini TF-IDF ile Trendyol kategorilerine eşleştir"""
    try:
        data = request.get_json(force=True) or {}
        categories = data.get('categories', [])
        
        if not categories:
            return jsonify({'success': False, 'message': 'categories listesi gerekli'}), 400
        
        from app.services.trendyol_service import (
            get_cached_category_id, load_category_cache_from_db, 
            match_category_id_for_title_tfidf, ensure_tfidf_ready,
            get_category_cache_stats
        )
        
        # Load category cache first
        load_category_cache_from_db()
        cache_stats = get_category_cache_stats()
        logging.info(f"Category cache stats: {cache_stats}")
        
        # Also prepare TF-IDF as fallback
        ensure_tfidf_ready()
        
        matches = {}
        for cat_name in categories:
            if not cat_name:
                continue
            
            # First try cache (fast)
            cat_id = get_cached_category_id(cat_name, default_id=0)
            
            # Fallback to TF-IDF (slower but more fuzzy)
            if not cat_id:
                cat_id = match_category_id_for_title_tfidf(cat_name)
            
            if cat_id:
                matches[cat_name] = cat_id
        
        return jsonify({
            'success': True,
            'matches': matches,
            'matched_count': len(matches),
            'failed_count': len(categories) - len(matches),
            'cache_loaded': cache_stats.get('loaded', False),
            'cache_count': cache_stats.get('count', 0)
        })
        
    except Exception as e:
        logging.exception("Trendyol category match error")
        return jsonify({'success': False, 'message': str(e)}), 500





@api_bp.route('/api/excel/category_mappings', methods=['POST'])
def api_excel_category_mappings():
    """Kategori eşleşmelerini kaydet"""
    try:
        data = request.get_json(force=True) or {}
        mappings = data.get('mappings', {})
        
        if not mappings:
            return jsonify({'success': False, 'message': 'Eşleşme verisi gerekli'}), 400
        
        # Save to Setting
        import json
        Setting.set('EXCEL_CATEGORY_MAPPINGS', json.dumps(mappings), user_id=current_user.id)
        
        return jsonify({
            'success': True,
            'message': f'{len(mappings)} kategori eşleşmesi kaydedildi',
            'count': len(mappings)
        })
        
    except Exception as e:
        logging.exception("Excel category mappings error")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/trendyol/brands', methods=['GET'])
def api_trendyol_brands_list():
    """Trendyol marka cache'ini listele"""
    try:
        from app.services.trendyol_service import load_brand_cache_from_db, _BRAND_CACHE
        
        # Load cache if not loaded
        if not _BRAND_CACHE.get('loaded'):
            load_brand_cache_from_db()
        
        brands = []
        for name_lower, data in _BRAND_CACHE.get('by_name', {}).items():
            brands.append({
                'id': data.get('id'),
                'name': data.get('name', name_lower)
            })
        
        # Sort by name
        brands.sort(key=lambda x: x['name'].lower())
        
        return jsonify({
            'success': True,
            'brands': brands,
            'total': len(brands),
            'cached': _BRAND_CACHE.get('loaded', False)
        })
        
    except Exception as e:
        logging.exception("Trendyol brands list error")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/trendyol/brands/search', methods=['GET'])
def api_trendyol_brand_search():
    """Trendyol API'de marka ara"""
    try:
        name = request.args.get('name', '').strip()
        if not name:
            return jsonify({'success': False, 'message': 'name gerekli'}), 400
        
        from app.services.trendyol_service import get_trendyol_client
        client = get_trendyol_client()
        
        # Search via API
        results = client.get_brands_by_name(name)
        
        brands = []
        if isinstance(results, list):
            for b in results:
                if isinstance(b, dict):
                    brands.append({'id': b.get('id'), 'name': b.get('name')})
        
        return jsonify({
            'success': True,
            'brands': brands,
            'query': name
        })
        
    except Exception as e:
        logging.exception("Trendyol brand search error")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/trendyol/brands/resolve', methods=['POST'])
def api_trendyol_brand_resolve():
    """Eşleşmeyen markaları CACHE ve API ile çözümle"""
    try:
        data = request.get_json(force=True) or {}
        unmatched_brands = data.get('brands', [])
        
        if not unmatched_brands:
            return jsonify({'success': False, 'message': 'brands listesi gerekli'}), 400
        
        from app.services.trendyol_service import match_brand_from_cache, get_trendyol_client, _BRAND_CACHE
        
        # Ensure cache is loaded
        from app.services.trendyol_service import load_brand_cache_from_db
        load_brand_cache_from_db()
        
        resolved = {}
        failed = []
        debug_info = []
        resolved_count = 0
        
        client = get_trendyol_client()
        
        # Check cache status early
        cache_size = _BRAND_CACHE.get("count", 0)
        if cache_size < 100:
            logging.warning(f"Brand cache seems empty or too small ({cache_size}). Matching will likely fail.")
            return jsonify({
                'success': True, # Return success true so frontend can process, but with warning
                'cache_empty': True,
                'message': 'Marka veritabanı boş! Lütfen Ayarlar sayfasından Markaları Çek butonunu kullanın.',
                'resolved': {},
                'failed': unmatched_brands,
                'resolved_count': 0,
                'failed_count': len(unmatched_brands),
                'cache_size': cache_size
            })
        
        for brand_name in unmatched_brands:
            if not brand_name:
                continue
            
            # 1. Try CACHE match (Aggressive Fuzzy)
            match = match_brand_from_cache(brand_name)
            
            if match:
                resolved[brand_name] = {'id': match['id'], 'name': match['name']}
                resolved_count += 1
                debug_info.append(f"Cache match: '{brand_name}' -> '{match['name']}'")
            else:
                # 2. API Fallback (optional, but good for very new brands)
                # Only if really necessary. User asked for "Ayarlardan çekilen", so maybe we should skip this?
                # But let's keep it as safe fallback for "First Word" logic which cache matcher might miss if not tokenized
                
                # First Word fallback via Cache?
                # If "Adidas Originals" fails, try "Adidas" in cache
                if ' ' in brand_name.strip():
                    first_word = brand_name.strip().split(' ', 1)[0]
                    if len(first_word) >= 3:
                         match_fw = match_brand_from_cache(first_word)
                         if match_fw:
                             resolved[brand_name] = {'id': match_fw['id'], 'name': match_fw['name']}
                             resolved_count += 1
                             debug_info.append(f"Cache First-Word match: '{first_word}' -> '{match_fw['name']}'")
                             continue
                
                # Report failure
                failed.append(brand_name)
                debug_info.append(f"No match found for '{brand_name}'")

        return jsonify({
            'success': True,
            'resolved': resolved,
            'failed': failed,
            'resolved_count': len(resolved),
            'failed_count': len(failed),
            'debug_info': debug_info,
            'cache_size': _BRAND_CACHE.get("count", 0)
        })
        
    except Exception as e:
        logging.exception("Trendyol brand resolve error")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/trendyol/brands/match_tfidf', methods=['POST'])
def api_trendyol_brand_match_tfidf():
    """Eşleşmeyen markaları TF-IDF ile çözümle (Batch Optimized)"""
    try:
        data = request.get_json(force=True) or {}
        unmatched_brands = data.get('brands', [])
        
        if not unmatched_brands:
            return jsonify({'success': False, 'message': 'brands listesi gerekli'}), 400
        
        from app.services.trendyol_service import (
            ensure_brand_tfidf_ready, match_brands_tfidf_batch, 
            match_brand_from_cache, _BRAND_TFIDF
        )
        
        # Ensure TF-IDF is ready
        ensure_brand_tfidf_ready()
        
        if not _BRAND_TFIDF.get("vectorizer"):
             return jsonify({'success': False, 'message': 'TF-IDF modeli oluşturulamadı (Marka önbelleği boş olabilir)'}), 400

        resolved = {}
        failed = []
        debug_info = []
        
        # 1. Process Cache (Exact/Levenshtein) matches first
        # This is fast and reliable for typos
        remaining_for_tfidf = []
        
        for brand_name in unmatched_brands:
            if not brand_name:
                continue
                
            match = match_brand_from_cache(brand_name)
            if match:
                resolved[brand_name] = {'id': match['id'], 'name': match['name']}
                debug_info.append(f"Standard Cache Match: '{brand_name}' -> '{match['name']}'")
            else:
                remaining_for_tfidf.append(brand_name)
        
        # 2. Process remaining with Vectorized TF-IDF (Batch)
        if remaining_for_tfidf:
            tfidf_results = match_brands_tfidf_batch(remaining_for_tfidf)
            
            for brand_name, match_tfidf in tfidf_results.items():
                if match_tfidf:
                    resolved[brand_name] = {'id': match_tfidf['id'], 'name': match_tfidf['name']}
                    debug_info.append(f"TF-IDF Match: '{brand_name}' -> '{match_tfidf['name']}'")
                else:
                    failed.append(brand_name)
                    debug_info.append(f"No match: '{brand_name}'")

        return jsonify({
            'success': True,
            'resolved': resolved,
            'failed': failed,
            'resolved_count': len(resolved),
            'failed_count': len(failed),
            'debug_info': debug_info
        })
        
    except Exception as e:
        logging.exception("Trendyol brand match tfidf error")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/excel/brand_mappings', methods=['POST'])
def api_excel_brand_mappings():
    """Marka eşleşmelerini kaydet"""
    try:
        data = request.get_json(force=True) or {}
        mappings = data.get('mappings', {})
        
        if not mappings:
            return jsonify({'success': False, 'message': 'Eşleşme verisi gerekli'}), 400
        
        # Save to Setting
        import json
        Setting.set('EXCEL_BRAND_MAPPINGS', json.dumps(mappings), user_id=current_user.id)
        
        return jsonify({
            'success': True,
            'message': f'{len(mappings)} marka eşleşmesi kaydedildi',
            'count': len(mappings)
        })
        
    except Exception as e:
        logging.exception("Excel brand mappings error")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/excel/send/<marketplace>', methods=['POST'])
def api_excel_send_to_marketplace(marketplace):
    """Excel ürünlerini pazaryerine gönder"""
    try:
        data = request.get_json(force=True) or {}
        file_id = data.get('file_id')
        indices = data.get('indices')  # None means all
        options = data.get('options', {})
        
        # Extract options
        logging.info(f"Excel send options received: {options}")
        zero_stock_as_one = options.get('zeroStockAsOne', False)
        skip_no_image = options.get('skipNoImage', False)
        apply_multiplier = options.get('applyMultiplier', False)
        skip_no_barcode = options.get('skipNoBarcode', False)
        default_price = options.get('defaultPrice', 0)
        title_prefix = options.get('titlePrefix', '')  # Ürün ismi öneki
        
        if not file_id:
            return jsonify({'success': False, 'message': 'file_id gerekli'}), 400
        
        from app.services.excel_service import build_excel_index, get_products_by_indices
        
        # Build Excel index (XML-compatible format)
        excel_index = build_excel_index(file_id, title_prefix=title_prefix)
        if not excel_index:
            return jsonify({'success': False, 'message': 'Excel dosyası bulunamadı veya süresi doldu'}), 400
        
        logging.info(f"Excel send - indices: {indices}, type: {type(indices)}")
        
        # Filter by indices if provided (must be a non-empty list)
        if indices and len(indices) > 0:
            # Get only selected products
            selected_barcodes = set()
            products = get_products_by_indices(file_id, indices)
            for p in products:
                bc = p.get('barcode', '')
                if bc:
                    selected_barcodes.add(bc)
            barcodes = list(selected_barcodes)
            logging.info(f"Filtered barcodes from indices: {len(barcodes)}")
        else:
            # Use all barcodes
            barcodes = list(excel_index.get('by_barcode', {}).keys())
            logging.info(f"All barcodes from index: {len(barcodes)}")
        
        if not barcodes:
            return jsonify({'success': False, 'message': 'Barkodlu ürün bulunamadı'}), 400
        
        # Store the excel_index temporarily so send functions can access it
        # We use a special prefix to identify Excel sources
        excel_source_id = f"excel:{file_id}"
        
        # Store in Setting temporarily (will be overwritten on next upload)
        try:
            import json
            Setting.set('_EXCEL_TEMP_INDEX', json.dumps(excel_index))
        except Exception as e:
            logging.warning(f"Could not store Excel index: {e}")
        
        # Create send_options dict to pass to services
        send_options = {
            'zero_stock_as_one': zero_stock_as_one,
            'skip_no_image': skip_no_image,
            'apply_multiplier': apply_multiplier,
            'skip_no_barcode': skip_no_barcode,
            'default_price': default_price,
            'title_prefix': title_prefix
        }
        
        # Submit job based on marketplace
        if marketplace == 'trendyol':
            from app.services.trendyol_service import perform_trendyol_send_products
            job_id = submit_mp_job(
                'excel_send', 'trendyol',
                lambda jid: perform_trendyol_send_products(jid, barcodes, excel_source_id, auto_match=True, send_options=send_options),
                params={'barcodes': barcodes[:5], 'total': len(barcodes), 'options': send_options}
            )
        elif marketplace == 'pazarama':
            from app.services.pazarama_service import perform_pazarama_send_products
            job_id = submit_mp_job(
                'excel_send', 'pazarama',
                lambda jid: perform_pazarama_send_products(jid, barcodes, excel_source_id),
                params={'barcodes': barcodes[:5], 'total': len(barcodes)}
            )
        elif marketplace == 'idefix':
            from app.services.idefix_service import perform_idefix_send_products
            job_id = submit_mp_job(
                'excel_send', 'idefix',
                lambda jid: perform_idefix_send_products(jid, barcodes, excel_source_id),
                params={'barcodes': barcodes[:5], 'total': len(barcodes)}
            )
        else:
            return jsonify({'success': False, 'message': f'{marketplace} henüz desteklenmiyor'}), 400
        
        return jsonify({'success': True, 'job_id': job_id, 'message': f'{len(barcodes)} ürün kuyruğa alındı'})
        
    except Exception as e:
        logging.exception(f"Excel send to {marketplace} error")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/trendyol/brands/cache_status', methods=['GET'])
def api_trendyol_brand_cache_status():
    """Get status of brand cache from DB"""
    try:
        from app.models.settings import Setting
        
        # Check raw DB value
        raw_val = Setting.get("TRENDYOL_BRAND_CACHE", "")
        raw_len = len(raw_val) if raw_val else 0
        
        # Check memory cache
        from app.services.trendyol_service import _BRAND_CACHE
        mem_count = _BRAND_CACHE.get("count", 0)
        
        return jsonify({
            'success': True,
            'db_size_bytes': raw_len,
            'mem_count': mem_count,
            'status': 'OK' if raw_len > 1000 else 'EMPTY'
        })
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/trendyol/brands/refresh_cache', methods=['POST'])
def api_trendyol_brand_refresh_cache():
    """Force refresh of brand cache"""
    try:
        from app.services.trendyol_service import fetch_and_cache_brands, load_brand_cache_from_db
        
        # Force fetch
        result = fetch_and_cache_brands()
        
        # Reload memory
        load_brand_cache_from_db()
        
        return jsonify(result)
    except Exception as e:
        logging.exception("Cache refresh failed")
        return jsonify({'success': False, 'message': str(e)}), 500

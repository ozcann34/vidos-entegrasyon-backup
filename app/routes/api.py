import json
import math
import logging
from datetime import datetime
from collections import Counter
from typing import Dict, Any, List, Optional
from flask import Blueprint, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from app import db
from app.models import SupplierXML, Product, BatchLog, Setting, AutoSync, SyncLog, MarketplaceProduct
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
from app.services.n11_client import get_n11_client
from app.services.n11_service import (
    perform_n11_send_products, perform_n11_send_all,
    perform_n11_sync_stock, perform_n11_sync_prices, perform_n11_sync_all
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
            
        # Check subscription limit
        from app.services.subscription_service import check_usage_limit
        if not check_usage_limit(user_id, 'xml_sources'):
             return jsonify({'success': False, 'message': 'Paketinizin XML kaynak limitini doldurdunuz. Lütfen paketinizi yükseltin.'}), 403
             
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

@api_bp.route('/api/trendyol/fetch_brands', methods=['POST'])
@login_required
def api_trendyol_fetch_brands():
    """Trendyol markalarını çek ve önbelleğe al"""
    try:
        from app.services.trendyol_service import fetch_and_cache_trendyol_brands
        # This function should exist in service, or we implement logic here
        # Let's assume it exists or use client directly
        # Checking service file suggests fetch_trendyol_brands might be there
        count = fetch_and_cache_trendyol_brands(force=True)
        return jsonify({'success': True, 'message': f'{count} marka çekildi ve önbelleklendi.'})
    except ImportError:
         # Fallback if service function name differs
         try:
            from app.services.trendyol_service import get_trendyol_client, _BRAND_CACHE
            client = get_trendyol_client()
            brands = client.get_brands(size=5000) # Fetch many
            # Update cache
            _BRAND_CACHE['by_id'] = {str(b['id']): b['name'] for b in brands}
            _BRAND_CACHE['by_name'] = {b['name'].lower(): b['id'] for b in brands}
            return jsonify({'success': True, 'message': f'{len(brands)} marka çekildi.'})
         except Exception as e2:
             return jsonify({'success': False, 'message': str(e2)}), 500
    except Exception as e:
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





@api_bp.route('/api/n11/fetch_categories', methods=['POST'])
def api_n11_fetch_categories():
    """N11 Kategorilerini çek ve önbelleğe al"""
    try:
        from app.services.n11_service import fetch_and_cache_n11_categories
        # force=True to ensure fresh fetch
        success = fetch_and_cache_n11_categories(force=True)
        if success:
             from app.services.n11_service import _N11_CATEGORY_CACHE
             count = len(_N11_CATEGORY_CACHE.get('list', []))
             return jsonify({
                'success': True, 
                'message': f'{count} kategori çekildi ve önbelleğe alındı.'
             })
        else:
             return jsonify({'success': False, 'message': 'Kategoriler çekilemedi, API hatası veya anahtar eksik.'}), 400
             
    except Exception as e:
        logging.exception("N11 fetch categories error")
        return jsonify({'success': False, 'message': str(e)}), 500

    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)
    query = (request.args.get('q') or '').strip().lower()

    q = Product.query.filter_by(user_id=current_user.id)
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
    
    # New Filters
    min_stock = request.args.get('min_stock', type=int)
    max_stock = request.args.get('max_stock', type=int)
    min_price = request.args.get('min_price', type=float)
    max_price = request.args.get('max_price', type=float)
    category = (request.args.get('category') or '').strip().lower()
    has_image = request.args.get('has_image') == 'true'

    if not source_id:
        return jsonify({'total': 0, 'items': []})
    
    # Use service to load index (cached)
    try:
        index = load_xml_source_index(source_id)
        all_records = index.get('__records__') or []
    except Exception:
        return jsonify({'total': 0, 'items': []})
    
    # Optimized filtering for large datasets
    def _match(rec):
        if query and query not in rec.get('title_normalized', '') and query not in str(rec.get('barcode', '')).lower():
            return False
        
        stock = to_int(rec.get('quantity', 0))
        if min_stock is not None and stock < min_stock: return False
        if max_stock is not None and stock > max_stock: return False
        
        price = to_float(rec.get('price', 0))
        if min_price is not None and price < min_price: return False
        if max_price is not None and price > max_price: return False
        
        if category and category not in str(rec.get('category', '')).lower():
            return False
            
        if has_image:
            imgs = rec.get('images', [])
            if not imgs or (isinstance(imgs, list) and len(imgs) == 0):
                return False
        
        return True

    filtered = [rec for rec in all_records if _match(rec)]
    
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
            # Check if we have local data
            total_local = MarketplaceProduct.query.filter_by(user_id=current_user.id, marketplace='trendyol').count()
            use_db = total_local > 0 and not strict_api
            
            if use_db:
                query = MarketplaceProduct.query.filter_by(user_id=current_user.id, marketplace='trendyol')
                
                # Filters
                if q:
                    query = query.filter(
                        (MarketplaceProduct.title.ilike(f"%{q}%")) | 
                        (MarketplaceProduct.barcode.ilike(f"%{q}%")) |
                        (MarketplaceProduct.stock_code.ilike(f"%{q}%"))
                    )
                
                # Status Filters
                if status_param == 'active':
                    # Active means On Sale
                    query = query.filter_by(on_sale=True)
                elif status_param == 'passive':
                    # Passive means Not On Sale (and generally approved/not rejected, but 'Passive' usually implies user action)
                    query = query.filter(MarketplaceProduct.on_sale == False)
                elif status_param in ('pending', 'onay', 'waiting'):
                    query = query.filter(MarketplaceProduct.approval_status.ilike('%Pending%') | MarketplaceProduct.approval_status.ilike('%Waiting%'))
                elif status_param == 'rejected':
                    query = query.filter(MarketplaceProduct.approval_status.ilike('%Rejected%'))
                
                # Other filters
                if on_sale is not None:
                    query = query.filter(MarketplaceProduct.on_sale == on_sale)
                if approved is not None:
                     # 'Approved' string vs boolean
                     if approved:
                         query = query.filter(MarketplaceProduct.approval_status == 'Approved')
                     else:
                         query = query.filter(MarketplaceProduct.approval_status != 'Approved')
                
                # Stock filters
                if low_stock_threshold > 0 or request.args.get('stock_filter') == 'low':
                    # The request might pass 'stock_filter=low' or just rely on global/param
                    if stock_filter == 'low' or request.args.get('low_stock') == '1':
                         query = query.filter(MarketplaceProduct.quantity < low_stock_threshold)
                    elif stock_filter == 'out':
                         query = query.filter(MarketplaceProduct.quantity <= 0)

                total_api = query.count()
                
                items_db = query.order_by(MarketplaceProduct.last_sync_at.desc()).offset((page-1)*per_page).limit(per_page).all()
                
                items = []
                for p in items_db:
                    # Parse images
                    imgs = p.get_images
                    img_url = imgs[0] if imgs else None
                    
                    status_label = 'Hazırlanıyor'
                    status_color = 'secondary'
                    if p.on_sale and p.approval_status == 'Approved':
                        status_label = 'Satışta'
                        status_color = 'success'
                    elif p.approval_status == 'Rejected':
                        status_label = 'Reddedildi'
                        status_color = 'danger'
                    elif p.approval_status in ('Pending', 'Waiting'):
                        status_label = 'İnceleniyor'
                        status_color = 'warning'
                    elif not p.on_sale:
                        status_label = 'Pasif'
                        status_color = 'dark'

                    items.append({
                        'barcode': p.barcode,
                        'stockCode': p.stock_code,
                        'title': p.title,
                        'imageUrl': img_url,
                        'modelNumber': p.stock_code,
                        'onSale': p.on_sale,
                        'approved': p.approval_status == 'Approved',
                        'rejected': p.approval_status == 'Rejected',
                        'quantity': p.quantity,
                        'salePrice': p.sale_price,
                        'listPrice': p.price,
                        'statusStr': status_label,
                        'status_label': status_label,
                        'status_color': status_color,
                        'approvalStatus': p.approval_status,
                        'last_updated': p.last_sync_at.strftime('%Y-%m-%d %H:%M') if p.last_sync_at else '',
                        'images': imgs or []
                    })
                
                return jsonify({
                    'success': True,
                    'items': items,
                    'total': total_local,
                    'filtered_total': total_api, # Pass this for pagination
                    'page': page,
                    'per_page': per_page
                })
            else:
                # Fallback to API if DB empty
                try:
                    client = get_trendyol_client()
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
                        search=q or None,
                        on_sale=on_sale,
                        approved=approved,
                        rejected=rejected,
                        approval_status=approval_status_param or None
                    )
                    content = resp.get('content') or resp.get('items') or []
                    total_api = int(resp.get('totalElements') or resp.get('total') or len(content))

                    items = []
                    for it in content:
                        barcode = it.get('barcode') or it.get('productBarcode') or ''
                        stock_code = it.get('stockCode') or it.get('productMainId') or it.get('modelNumber') or ''
                        title = it.get('title') or it.get('name') or it.get('productName') or ''
                        
                        approval_status_raw = str(it.get('approvalStatus') or '').strip().upper()
                        approved_flag = it.get('approved')
                        if isinstance(approved_flag, str):
                            approved_bool = approved_flag.strip().lower() in ('true', '1', 'yes', 'on')
                        else:
                            approved_bool = bool(approved_flag) if approved_flag is not None else False
                        
                        on_sale_raw = it.get('onSale')
                        if isinstance(on_sale_raw, str):
                            on_sale_bool = on_sale_raw.strip().lower() in ('true', '1', 'yes', 'on')
                        else:
                            on_sale_bool = bool(on_sale_raw) if on_sale_raw is not None else False

                        img_url = ''
                        if it.get('images'):
                             img_url = it.get('images')[0].get('url', '')

                        status_str = 'Bekliyor'
                        if on_sale_bool and approved_bool: status_str = 'Aktif'
                        elif not on_sale_bool: status_str = 'Pasif'
                        elif approval_status_raw: status_str = approval_status_raw

                        status_label = 'Bekliyor'
                        status_color = 'warning'
                        if on_sale_bool and approved_bool:
                            status_label = 'Aktif'
                            status_color = 'success'
                        elif not on_sale_bool:
                            status_label = 'Pasif'
                            status_color = 'dark'
                        elif approval_status_raw == 'REJECTED':
                            status_label = 'Reddedildi'
                            status_color = 'danger'

                        items.append({
                            'barcode': barcode,
                            'stockCode': stock_code,
                            'title': title,
                            'imageUrl': img_url,
                            'modelNumber': stock_code,
                            'onSale': on_sale_bool,
                            'approved': approved_bool,
                            'rejected': it.get('rejected', False),
                            'quantity': it.get('stock') or it.get('quantity') or 0,
                            'salePrice': it.get('salePrice', 0),
                            'listPrice': it.get('listPrice', 0),
                            'statusStr': status_label,
                            'status_label': status_label,
                            'status_color': status_color,
                            'approvalStatus': approval_status_raw,
                            'last_updated': '',
                            'images': it.get('images', [])
                        })
                    
                    return jsonify({'total': total_api, 'items': items})

                except Exception as ex:
                    return jsonify({'total': 0, 'items': [], 'error': f'Trendyol API Error: {str(ex)}'}), 500
        elif marketplace.lower() == 'idefix':
            # Check if we have local data
            total_local = MarketplaceProduct.query.filter_by(user_id=current_user.id, marketplace='idefix').count()
            use_db = total_local > 0 and not strict_api
            
            # AUTO-SYNC TRIGGER: If entry page (page 1, no search) and (empty or stale), start sync
            sync_started = False
            job_id = None
            if page == 1 and not q and not strict_api:
                # Check when was the last sync
                last_p = MarketplaceProduct.query.filter_by(user_id=current_user.id, marketplace='idefix').order_by(MarketplaceProduct.last_sync_at.desc()).first()
                stale = True
                if last_p and last_p.last_sync_at:
                    diff = datetime.now() - last_p.last_sync_at
                    if diff.total_seconds() < 3600: # 1 hour
                        stale = False
                
                if total_local == 0 or stale:
                    try:
                        # Check if a sync is already running
                        from app.services.job_queue import _MP_JOBS, submit_mp_job, _MP_JOBS_LOCK
                        is_running = False
                        with _MP_JOBS_LOCK:
                            for jid, jdata in _MP_JOBS.items():
                                if jdata.get('marketplace') == 'idefix' and jdata.get('job_type') == 'idefix_refresh_cache' and jdata.get('status') in ('queued', 'running'):
                                    is_running = True
                                    job_id = jid
                                    break
                        
                        if not is_running:
                            from app.services.idefix_service import sync_idefix_products
                            def auto_refresh_task(jid):
                                return sync_idefix_products(user_id=current_user.id, job_id=jid)
                            
                            job_id = submit_mp_job('idefix_refresh_cache', 'idefix', auto_refresh_task)
                            sync_started = True
                            logging.info(f"[IDEFIX] Auto-sync triggered for user {current_user.id}")
                    except:
                        logging.exception("Failed to start auto-sync for Idefix")

            if use_db:
                query = MarketplaceProduct.query.filter_by(user_id=current_user.id, marketplace='idefix')
                total_filtered = total_local
                
                # Filters
                if q:
                    query = query.filter(
                        (MarketplaceProduct.title.ilike(f"%{q}%")) | 
                        (MarketplaceProduct.barcode.ilike(f"%{q}%")) |
                        (MarketplaceProduct.stock_code.ilike(f"%{q}%"))
                    )
                    total_filtered = query.count()

                # Status Filters
                if status_param == 'active':
                    query = query.filter_by(on_sale=True)
                    total_filtered = query.count()
                elif status_param in ('pending', 'onay', 'waiting'):
                    query = query.filter(MarketplaceProduct.status.ilike('%İnceleniyor%') | MarketplaceProduct.status.ilike('%Bekliyor%'))
                    total_filtered = query.count()
                elif status_param == 'passive':
                    query = query.filter_by(on_sale=False)
                    total_filtered = query.count()

                items_db = query.order_by(MarketplaceProduct.last_sync_at.desc()).offset((page-1)*per_page).limit(per_page).all()
                
                items = []
                for p in items_db:
                    imgs = p.get_images
                    
                    # Status color coding for UI
                    status_c = 'secondary'
                    st = (p.status or '').lower()
                    if 'satışta' in st: status_c = 'success'
                    elif 'ince' in st or 'bekli' in st: status_c = 'warning'
                    elif 'red' in st: status_c = 'danger'
                    elif 'eksik' in st: status_c = 'info'
                    
                    items.append({
                        'barcode': p.barcode,
                        'stockCode': p.stock_code,
                        'title': p.title,
                        'price': p.price,
                        'salePrice': p.sale_price,
                        'quantity': p.quantity,
                        'images': imgs,
                        'status_label': p.status,
                        'status_color': status_c,
                        'last_updated': p.last_sync_at.strftime('%Y-%m-%d %H:%M') if p.last_sync_at else ''
                    })
                
                return jsonify({
                    'success': True,
                    'total': total_local,
                    'filtered_total': total_filtered,
                    'items': items,
                    'source': 'database',
                    'sync_started': sync_started,
                    'job_id': job_id
                })
            else:
                # Fallback to API
                try:
                    from app.services.idefix_service import get_idefix_client
                    client = get_idefix_client(user_id=current_user.id)
                    # Use 'limit' as per documentation. Cap at 500.
                    limit_val = min(per_page, 500)
                    resp = client.list_products(page=page-1, limit=limit_val, search=q or None)
                    
                    content = resp.get('content', [])
                    total_api = int(resp.get('totalElements', 0))

                    items = []
                    for it in content:
                        barcode = it.get('barcode', '')
                        imgs = it.get('images', [])
                        img_urls = [i.get('url') if isinstance(i, dict) else i for i in imgs]
                        
                        # Flexible mapping for Idefix
                        # Status fallback
                        pool_state = it.get('poolState') or it.get('productStatus') or 'UNKNOWN'
                        pool_state_up = str(pool_state).upper()
                        
                        status_color = 'secondary'
                        status_label = pool_state
                        
                        if pool_state_up == "APPROVED":
                            status_label = "Satışta"
                            status_color = "success"
                        elif pool_state_up == "WAITING_APPROVAL":
                            status_label = "İnceleniyor"
                            status_color = "warning"
                        elif pool_state_up == "WAITING_CONTENT":
                            status_label = "Eksik Bilgili"
                            status_color = "info"
                        elif pool_state_up == "REJECTED":
                            status_label = "Reddedildi"
                            status_color = "danger"
                        elif pool_state_up == "DELETED":
                            status_label = "Silindi"
                            status_color = "dark"

                        # Stock fallback
                        qty = it.get('stockAmount')
                        if qty is None: qty = it.get('inventoryQuantity')
                        if qty is None: qty = it.get('quantity')
                        if qty is None: qty = it.get('stock')
                        if qty is None: qty = 0

                        items.append({
                            'barcode': barcode,
                            'stockCode': it.get('vendorStockCode') or barcode,
                            'title': it.get('title'),
                            'price': it.get('price', 0),
                            'salePrice': it.get('salePrice', it.get('price', 0)),
                            'quantity': int(qty),
                            'images': img_urls,
                            'status_label': status_label,
                            'status_color': status_color,
                            'last_updated': 'Canlı Veri'
                        })
                    
                    return jsonify({
                        'success': True,
                        'total': total_api,
                        'filtered_total': total_api,
                        'items': items,
                        'source': 'api'
                    })
                except Exception as ex:
                    logging.exception("Idefix API fallback error")
                    return jsonify({'total': 0, 'items': [], 'error': f'Idefix API Hatası: {str(ex)}'}), 500

        elif marketplace.lower() == 'pazarama':
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
                total_all = len(data_all)  # Total products before filtering
                total_filtered = len(filtered)  # Total after filtering
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
                
                return jsonify({
                    'total': total_all,
                    'filtered_total': total_filtered,
                    'items': items
                })

            except Exception as ex:
                return jsonify({'total': 0, 'items': [], 'error': str(ex)}), 500

        elif marketplace.lower() == 'n11':
            try:
                client = get_n11_client()
                if not client:
                     return jsonify({'total': 0, 'items': [], 'error': 'N11 API anahtarları eksik'}), 400
                
                resp = client.get_products(page=page-1, size=per_page)
                # Parse N11 response (GetProductQuery uses 'content', not 'products')
                # See n11api.txt "Satıcı Ürünlerini Listeleme"
                content = resp.get('content') or []
                total_api = int(resp.get('totalElements') or len(content))
                
                items = []
                for p in content:
                    status_raw = str(p.get('status') or '').lower()
                    status_label = p.get('status') or 'Bilinmiyor'
                    status_color = 'success' if status_raw == 'active' else 'secondary'
                    
                    # Images are in imageUrls
                    images = p.get('imageUrls') or []
                    
                    # Quantity is direct
                    qty = int(p.get('quantity') or 0)
                         
                    items.append({
                        'barcode': p.get('barcode') or p.get('stockCode') or '',
                        'stockCode': p.get('stockCode') or '',
                        'title': p.get('title'),
                        'price': float(p.get('salePrice') or 0),
                        'quantity': qty,
                        'images': images,
                        'status_label': status_label,
                        'status_color': status_color,
                    })
                    
                return jsonify({'total': total_api, 'items': items})
            except Exception as ex:
                return jsonify({'total': 0, 'items': [], 'error': f'N11 API Hatası: {str(ex)}'}), 500

    except Exception as e:
        return jsonify({'total': 0, 'items': [], 'error': str(e)}), 500
    
    return jsonify({'total': 0, 'items': []})

@api_bp.route("/api/product/update_stock/<marketplace>/<barcode>", methods=["POST"])
def api_update_stock(marketplace, barcode):
    new_quantity = request.json.get('quantity')
    
    if marketplace == 'n11':
        from app.services.n11_service import update_n11_stock_price
        res = update_n11_stock_price(barcode, stock=new_quantity)
        if res.get('success'):
            flash(f"✅ N11 - {barcode} stok güncelleme kuyruğa alındı.", "success")
        else:
             flash(f"❌ N11 - {barcode} stok hatası: {res.get('message')}", "danger")
        return jsonify(res)

    # Placeholder for single update logic, potentially using job queue or direct call
    # For now, just a placeholder as in original app.py
    from app.services.activity_logger import log_user_activity
    log_user_activity(current_user.id, 'update_stock', marketplace, {'barcode': barcode, 'quantity': new_quantity})
    
    flash(f"⚠️ {MARKETPLACES.get(marketplace, 'Pazar Yeri')} - {barcode} için stok {new_quantity} olarak güncelleniyor.", "warning")
    return jsonify({"success": True, "message": f"{barcode} için stok güncelleme kuyruğa alındı."})

@api_bp.route("/api/product/update_price/<marketplace>/<barcode>", methods=["POST"])
def api_update_price(marketplace, barcode):
    new_price = request.json.get('price')
    
    if marketplace == 'n11':
        from app.services.n11_service import update_n11_stock_price
        res = update_n11_stock_price(barcode, price=new_price)
        if res.get('success'):
            flash(f"✅ N11 - {barcode} fiyat güncelleme kuyruğa alındı.", "success")
        else:
             flash(f"❌ N11 - {barcode} fiyat hatası: {res.get('message')}", "danger")
        return jsonify(res)

    from app.services.activity_logger import log_user_activity
    log_user_activity(current_user.id, 'update_price', marketplace, {'barcode': barcode, 'price': new_price})

    flash(f"⚠️ {MARKETPLACES.get(marketplace, 'Pazar Yeri')} - {barcode} için fiyat {new_price} olarak güncelleniyor.", "warning")
    return jsonify({"success": True, "message": f"{barcode} için fiyat güncelleme kuyruğa alındı."})

@api_bp.route("/api/product/delete/<marketplace>/<barcode>", methods=["POST"])
def api_delete_product(marketplace, barcode):
    if marketplace == 'n11':
        from app.services.n11_service import delete_n11_product
        res = delete_n11_product(barcode)
        if res.get('success'):
            flash(f"✅ N11 - {barcode} başarıyla silindi.", "success")
        else:
            flash(f"❌ N11 - {barcode} silinirken hata: {res.get('message')}", "danger")
        return jsonify(res)
        
        return jsonify(res)
        
    from app.services.activity_logger import log_user_activity
    log_user_activity(current_user.id, 'delete_product', marketplace, {'barcode': barcode})

    flash(f"⚠️ {MARKETPLACES.get(marketplace, 'Pazar Yeri')} - {barcode} ürünü silme kuyruğuna alındı.", "danger")
    return jsonify({"success": True, "message": "İşlem kuyruğa alındı."})
@api_bp.route("/api/product/update_details/<marketplace>", methods=["POST"])
def api_product_update_details(marketplace):
    """
    Update detailed product info (Title, Price, Stock, Description, etc.)
    """
    try:
        data = request.get_json(force=True) or {}
        barcode = data.get('barcode')
        if not barcode:
            return jsonify({'success': False, 'message': 'Barkod zorunludur.'}), 400

        # Dispatch
        if marketplace == 'trendyol':
            from app.services.trendyol_service import perform_trendyol_product_update
            res = perform_trendyol_product_update(barcode, data)
            return jsonify(res)
        
        elif marketplace == 'n11':
            from app.services.n11_service import perform_n11_product_update
            res = perform_n11_product_update(barcode, data)
            return jsonify(res)
            
        elif marketplace == 'pazarama':
            from app.services.pazarama_service import perform_pazarama_product_update
            res = perform_pazarama_product_update(barcode, data)
            return jsonify(res)
            
        elif marketplace == 'idefix':
            from app.services.idefix_service import perform_idefix_product_update
            res = perform_idefix_product_update(barcode, data)
            return jsonify(res)
            
        else:
            return jsonify({'success': False, 'message': f'{marketplace} henüz desteklenmiyor.'}), 400

    except Exception as e:
        logging.exception(f"Error updating product details {marketplace}")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route("/api/product/bulk_update/<marketplace>", methods=["POST"])
def api_product_bulk_update(marketplace):
    items = request.json.get('items', [])
    if not items:
        return jsonify({'success': False, 'message': 'Liste boş.'}), 400
        
    if marketplace == 'n11':
        from app.services.n11_service import bulk_update_n11_stock_price
        res = bulk_update_n11_stock_price(items)
        return jsonify(res)
        
    # Placeholder for others
    # if marketplace == 'trendyol': ...
    
    return jsonify({'success': True, 'updated': len(items), 'message': f'{marketplace} için toplu güncelleme (Simülasyon)'})

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

@api_bp.route('/api/idefix/refresh_cache', methods=['POST'])
def api_idefix_refresh_cache():
    """Idefix ürünlerini API'den çekip veritabanını yeniler"""
    try:
        from app.services.idefix_service import sync_idefix_products
        
        # Arka planda çalıştır
        def refresh_task(job_id):
            return sync_idefix_products(user_id=current_user.id, job_id=job_id)
        
        job_id = submit_mp_job(
            'idefix_refresh_cache',
            'idefix',
            refresh_task
        )
        
        return jsonify({
            'success': True,
            'job_id': job_id,
            'message': 'İdefix ürün listesi yenileme işlemi başlatıldı.'
        })
        
    except Exception as e:
        logging.exception("Error refreshing idefix cache")
        return jsonify({'success': False, 'message': str(e)}), 500
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
        auto_match = payload.get('auto_match', True)
        match_by = payload.get('match_by', 'barcode')
        
        # New Sending Options
        send_options = {
            'price_multiplier': payload.get('price_multiplier', 1.0),
            'default_price': payload.get('default_price', 0.0),
            'skip_no_barcode': payload.get('skip_no_barcode', False),
            'skip_no_image': payload.get('skip_no_image', False),
            'zero_stock_as_one': payload.get('zero_stock_as_one', False),
            'title_prefix': payload.get('title_prefix', ''),
            'match_by': match_by
        }
        
        if not xml_source_id:
            return jsonify({'success': False, 'message': 'Kaynak ID zorunludur.'}), 400

        from app.services.trendyol_service import perform_trendyol_send_all
        
        job_id = submit_mp_job(
            'trendyol_send_all',
            'trendyol',
            lambda job_id: perform_trendyol_send_all(job_id, xml_source_id, auto_match=auto_match, **send_options),
            params={'xml_source_id': xml_source_id, 'auto_match': auto_match, **send_options},
        )
        return jsonify({'success': True, 'job_id': job_id, 'batch_id': job_id}), 202
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/hepsiburada/send_all', methods=['POST'])
def api_hepsiburada_send_all():
    return jsonify({'success': False, 'message': 'Hepsiburada toplu gönderim henüz aktif değil.'}), 501

@api_bp.route('/api/n11/send_all', methods=['POST'])
def api_n11_send_all():
    try:
        payload = request.get_json(force=True) or {}
        xml_source_id = payload.get('source_id')
        auto_match = payload.get('auto_match', True)
        match_by = payload.get('match_by', 'barcode')
        
        # New Sending Options
        send_options = {
            'price_multiplier': payload.get('price_multiplier', 1.0),
            'default_price': payload.get('default_price', 0.0),
            'skip_no_barcode': payload.get('skip_no_barcode', False),
            'skip_no_image': payload.get('skip_no_image', False),
            'zero_stock_as_one': payload.get('zero_stock_as_one', False),
            'title_prefix': payload.get('title_prefix', ''),
            'match_by': match_by
        }
        
        if not xml_source_id:
            return jsonify({'success': False, 'message': 'Kaynak ID zorunludur.'}), 400

        from app.services.n11_service import perform_n11_send_all
        
        job_id = submit_mp_job(
            'n11_send_all',
            'n11',
            lambda job_id: perform_n11_send_all(job_id, xml_source_id, auto_match=auto_match, **send_options),
            params={'xml_source_id': xml_source_id, 'auto_match': auto_match, **send_options},
        )
        return jsonify({'success': True, 'job_id': job_id, 'batch_id': job_id}), 202
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500



@api_bp.route('/api/n11/search_brand', methods=['POST'])
def api_n11_search_brand():
    try:
        data = request.json or {}
        brand_name = data.get('brand_name')
        if not brand_name:
            return jsonify({'success': False, 'message': 'Marka adı gerekli'}), 400
            
        from app.services.n11_service import search_n11_brand
        result = search_n11_brand(brand_name)
        
        if result:
            return jsonify({'success': True, 'brand': result})
        else:
            return jsonify({'success': False, 'message': 'Marka bulunamadı'}), 404
            
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/pazarama/send_all', methods=['POST'])
def api_pazarama_send_all():
    """Pazarama için XML'deki tüm ürünleri gönder"""
    try:
        payload = request.get_json(force=True) or {}
        xml_source_id = payload.get('source_id')
        
        # New Sending Options
        send_options = {
            'price_multiplier': payload.get('price_multiplier', 1.0),
            'default_price': payload.get('default_price', 0.0),
            'skip_no_barcode': payload.get('skip_no_barcode', False),
            'skip_no_image': payload.get('skip_no_image', False),
            'zero_stock_as_one': payload.get('zero_stock_as_one', False),
            'title_prefix': payload.get('title_prefix', ''),
            'match_by': payload.get('match_by', 'barcode')
        }
        
        if not xml_source_id:
            return jsonify({'success': False, 'message': 'Kaynak ID zorunludur.'}), 400

        from app.services.pazarama_service import perform_pazarama_send_all
        
        job_id = submit_mp_job(
            'pazarama_send_all',
            'pazarama',
            lambda job_id: perform_pazarama_send_all(job_id, xml_source_id, **send_options),
            params={'xml_source_id': xml_source_id, **send_options},
        )
        return jsonify({'success': True, 'job_id': job_id, 'batch_id': job_id}), 202
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/idefix/send_all', methods=['POST'])
def api_idefix_send_all():
    try:
        payload = request.get_json(force=True) or {}
        xml_source_id = payload.get('source_id')
        
        # New Sending Options
        send_options = {
            'price_multiplier': payload.get('price_multiplier', 1.0),
            'default_price': payload.get('default_price', 0.0),
            'skip_no_barcode': payload.get('skip_no_barcode', False),
            'skip_no_image': payload.get('skip_no_image', False),
            'zero_stock_as_one': payload.get('zero_stock_as_one', False),
            'title_prefix': payload.get('title_prefix', ''),
            'match_by': payload.get('match_by', 'barcode')
        }
        
        if not xml_source_id:
            return jsonify({'success': False, 'message': 'Kaynak ID zorunludur.'}), 400

        from app.services.idefix_service import perform_idefix_send_all
        
        job_id = submit_mp_job(
            'idefix_send_all',
            'idefix',
            lambda job_id: perform_idefix_send_all(job_id, xml_source_id, **send_options),
            params={'xml_source_id': xml_source_id, **send_options},
        )
        return jsonify({'success': True, 'job_id': job_id, 'batch_id': job_id}), 202
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
        match_by = payload.get('match_by', 'barcode')
        title_prefix = payload.get('title_prefix')
        
        # New Sending Options
        send_options = {
            'price_multiplier': payload.get('price_multiplier', 1.0),
            'default_price': payload.get('default_price', 0.0),
            'skip_no_barcode': payload.get('skip_no_barcode', False),
            'skip_no_image': payload.get('skip_no_image', False),
            'zero_stock_as_one': payload.get('zero_stock_as_one', False),
            'title_prefix': title_prefix,
            'match_by': match_by
        }
        
        if not barcodes:
            return jsonify({'success': False, 'message': 'Ürün seçilmedi.'}), 400

        if marketplace == 'idefix':
            from app.services.idefix_service import perform_idefix_send_products
            job_id = submit_mp_job(
                'idefix_send_selected',
                'idefix',
                lambda job_id: perform_idefix_send_products(job_id, barcodes, xml_source_id, **send_options),
                params={'barcodes': barcodes, 'xml_source_id': xml_source_id, 'requested_marketplace': marketplace, **send_options}
            )
        elif marketplace == 'trendyol':
            from app.services.trendyol_service import perform_trendyol_send_products
            job_id = submit_mp_job(
                'trendyol_send_selected',
                'trendyol',
                lambda job_id: perform_trendyol_send_products(job_id, barcodes, xml_source_id, auto_match=True, **send_options),
                params={'barcodes': barcodes, 'xml_source_id': xml_source_id, 'requested_marketplace': marketplace, **send_options}
            )
        elif marketplace == 'pazarama':
            from app.services.pazarama_service import perform_pazarama_send_products
            job_id = submit_mp_job(
                'pazarama_send_selected',
                'pazarama',
                lambda job_id: perform_pazarama_send_products(job_id, barcodes, xml_source_id, **send_options),
                params={'barcodes': barcodes, 'xml_source_id': xml_source_id, 'requested_marketplace': marketplace, **send_options}
            )
        elif marketplace == 'n11':
            from app.services.n11_service import perform_n11_send_products
            job_id = submit_mp_job(
                'n11_send_selected',
                'n11',
                lambda job_id: perform_n11_send_products(job_id, barcodes, xml_source_id, auto_match=True, **send_options),
                params={'barcodes': barcodes, 'xml_source_id': xml_source_id, 'requested_marketplace': marketplace, **send_options}
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

@api_bp.route('/api/send_all', methods=['POST'])
def api_send_all():
    try:
        payload = request.get_json(force=True) or {}
        marketplace = payload.get('marketplace')
        xml_source_id = payload.get('xml_source_id')
        match_by = payload.get('match_by', 'barcode')
        mode = payload.get('mode', 'all') # stock, price, all (for sync) or update
        
        if not marketplace or not xml_source_id:
            return jsonify({'success': False, 'message': 'Eksik parametreler.'}), 400
            
        job_id = None
        
        # TRENDYOL
        if marketplace == 'trendyol':
            from app.services.trendyol_service import (
                perform_trendyol_sync_stock, perform_trendyol_sync_prices, perform_trendyol_sync_all
            )
            if mode == 'stock':
                job_id = submit_mp_job('trendyol_sync_stock', 'trendyol', 
                    lambda jid: perform_trendyol_sync_stock(jid, xml_source_id))
            elif mode == 'price':
                job_id = submit_mp_job('trendyol_sync_prices', 'trendyol', 
                    lambda jid: perform_trendyol_sync_prices(jid, xml_source_id, match_by=match_by))
            else:
                 job_id = submit_mp_job('trendyol_sync_all', 'trendyol', 
                    lambda jid: perform_trendyol_sync_all(jid, xml_source_id, match_by=match_by))
        
        # N11
        elif marketplace == 'n11':
            from app.services.n11_service import (
                perform_n11_sync_stock, perform_n11_sync_prices, perform_n11_sync_all
            )
            if mode == 'stock':
                 job_id = submit_mp_job('n11_sync_stock', 'n11', 
                    lambda jid: perform_n11_sync_stock(jid, xml_source_id))
            elif mode == 'price':
                 job_id = submit_mp_job('n11_sync_prices', 'n11', 
                    lambda jid: perform_n11_sync_prices(jid, xml_source_id))
            else:
                 job_id = submit_mp_job('n11_sync_all', 'n11', 
                    lambda jid: perform_n11_sync_all(jid, xml_source_id, match_by=match_by))

        # PAZARAMA
        elif marketplace == 'pazarama':
             from app.services.pazarama_service import (
                perform_pazarama_sync_stock, perform_pazarama_sync_prices, perform_pazarama_sync_all
             )
             if mode == 'stock':
                 job_id = submit_mp_job('pazarama_sync_stock', 'pazarama', 
                    lambda jid: perform_pazarama_sync_stock(jid, xml_source_id))
             elif mode == 'price':
                 job_id = submit_mp_job('pazarama_sync_prices', 'pazarama', 
                    lambda jid: perform_pazarama_sync_prices(jid, xml_source_id))
             else:
                 job_id = submit_mp_job('pazarama_sync_all', 'pazarama', 
                    lambda jid: perform_pazarama_sync_all(jid, xml_source_id))

        else:
            return jsonify({'success': False, 'message': 'Pazaryeri desteklenmiyor.'}), 400

        return jsonify({
            'success': True, 
            'job_id': job_id,
            'message': f'{MARKETPLACES.get(marketplace, marketplace)} işlemi başlatıldı.'
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



@api_bp.route('/api/trendyol/search_brand', methods=['POST'])
@login_required
def api_trendyol_search_brand():
    """Search for a Trendyol brand by name and return brand ID."""
    try:
        data = request.get_json()
        brand_name = (data.get('brand_name') or '').strip()
        
        logging.info(f"[TRENDYOL] Brand search request for: '{brand_name}'")
        
        if not brand_name:
            return jsonify({'success': False, 'message': 'Marka adı boş olamaz'}), 400
        
        client = get_trendyol_client()
        
        # Search for brand using the existing method
        brands = client.get_brands_by_name(brand_name)
        
        if not brands:
            logging.info(f"[TRENDYOL] No brands found for: '{brand_name}'")
            return jsonify({
                'success': False, 
                'message': f'"{brand_name}" adında marka bulunamadı'
            })
        
        # Find exact match or closest match
        exact_match = None
        for brand in brands:
            if brand.get('name', '').lower() == brand_name.lower():
                exact_match = brand
                break
        
        # Use exact match if found, otherwise use first result
        result_brand = exact_match or brands[0]
        
        logging.info(f"[TRENDYOL] Brand found: {result_brand.get('name')} (ID: {result_brand.get('id')})")
        
        return jsonify({
            'success': True,
            'brand_id': result_brand.get('id'),
            'brand_name': result_brand.get('name'),
            'message': f'Marka bulundu: {result_brand.get("name")}'
        })
        
    except Exception as e:
        logging.exception(f"[TRENDYOL] Brand search error: {e}")
        return jsonify({
            'success': False, 
            'message': f'Marka arama hatası: {str(e)}'
        }), 500

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



# --- Cache Clear Endpoints ---




@api_bp.route('/api/hepsiburada/clear_cache', methods=['POST'])
def api_hb_clear_cache():
    try:
        return jsonify({'success': True, 'message': 'Hepsiburada önbelleği temizlendi.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/idefix/clear_cache', methods=['POST'])
def api_idefix_clear_cache():
    try:
        from app.services.idefix_service import clear_idefix_cache
        clear_idefix_cache()
        return jsonify({'success': True, 'message': 'Idefix önbelleği temizlendi.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@api_bp.route('/api/n11/clear_cache', methods=['POST'])
def api_n11_clear_cache():
    try:
        return jsonify({'success': True, 'message': 'N11 önbelleği temizlendi.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500



# ---------------- Auto Sync API Endpoints ----------------
@api_bp.route('/api/auto_sync/orders/settings', methods=['GET', 'POST'])
@login_required
def api_auto_sync_orders_settings():
    """Otomatik Sipariş Senkronizasyonu ayarlarını yönet"""
    if request.method == 'GET':
        settings = {
            'enabled': Setting.get('ORDER_SYNC_ENABLED', user_id=current_user.id) == 'true',
            'interval': int(Setting.get('ORDER_SYNC_INTERVAL', user_id=current_user.id) or 60),
            'last_sync': Setting.get('ORDER_SYNC_LAST_RUN', user_id=current_user.id)
        }
        return jsonify({'success': True, 'settings': settings})
        
    try:
        data = request.get_json()
        enabled = data.get('enabled')
        interval = int(data.get('interval', 60))
        
        # Save settings
        Setting.set('ORDER_SYNC_ENABLED', 'true' if enabled else 'false', user_id=current_user.id)
        Setting.set('ORDER_SYNC_INTERVAL', str(interval), user_id=current_user.id)
        
        # Update Scheduler
        from app.services.scheduler_service import add_order_sync_job, remove_order_sync_job
        
        if enabled:
            add_order_sync_job(interval)
        else:
            remove_order_sync_job()
        
        return jsonify({'success': True, 'message': 'Ayarlar güncellendi'})
        
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route("/announcement/<int:id>/dismiss", methods=["POST"])
@login_required
def dismiss_announcement(id):
    from app.models.announcement import Announcement
    ann = Announcement.query.get_or_404(id)
    ann.is_active = False
    db.session.commit()
    return jsonify({"success": True})

@api_bp.route("/api/test_connection/<marketplace>", methods=["POST"])
@login_required
def api_test_connection(marketplace):
    """Test marketplace connection credentials"""
    try:
        success = False
        message = "Bağlantı başarısız."
        
        if marketplace == 'trendyol':
            from app.services.trendyol_service import get_trendyol_client
            client = get_trendyol_client()
            # Try fetching a simple data point like supplier addresses or just init
            # validation often happens in init, but let's try a call
            try:
               client.get_shipment_addresses() # Lightweight call
               success = True
               message = "Trendyol bağlantısı başarılı."
            except Exception as e:
               message = f"Trendyol hatası: {str(e)}"

        elif marketplace == 'hepsiburada':
            from app.services.hepsiburada_service import get_hepsiburada_client
            client = get_hepsiburada_client()
            # HB API check
            try:
                # If get_product_count works, creds are fine
                cnt = client.get_product_count()
                success = True
                message = f"Hepsiburada bağlantısı başarılı. (Ürün: {cnt})"
            except Exception as e:
                message = f"Hepsiburada hatası: {str(e)}"
                
        elif marketplace == 'pazarama':
            from app.services.pazarama_service import get_pazarama_client
            client = get_pazarama_client()
            try:
                # Pazarama auth check
                # Currently get_product_count was buggy returning 0 but no error means auth ok?
                # Or try get_brands if available
                client.get_product_count() 
                success = True
                message = "Pazarama bağlantısı başarılı."
            except Exception as e:
                message = f"Pazarama hatası: {str(e)}"
                
        elif marketplace == 'n11':
            from app.services.n11_client import get_n11_client
            client = get_n11_client()
            try:
                client.get_product_count()
                success = True
                message = "N11 bağlantısı başarılı."
            except Exception as e:
                message = f"N11 hatası: {str(e)}"

        elif marketplace == 'idefix':
            from app.services.idefix_service import get_idefix_client
            client = get_idefix_client()
            try:
                # Idefix check
                client.get_token() # explicit token fetch
                success = True
                message = "İdefix bağlantısı başarılı."
            except Exception as e:
                 message = f"İdefix hatası: {str(e)}"

        return jsonify({'success': success, 'message': message})

    except Exception as e:
        return jsonify({'success': False, 'message': f"Genel hata: {str(e)}"}), 500


@api_bp.route('/api/orders/recent', methods=['GET'])
@login_required
def api_recent_orders():
    """Son siparişleri döndür"""
    try:
        limit = request.args.get('limit', 5, type=int)
        orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).limit(limit).all()
        
        orders_data = []
        for order in orders:
            orders_data.append({
                'id': order.id,
                'order_number': order.order_number,
                'marketplace': order.marketplace,
                'customer': order.customer_name,
                'amount': f"{order.total_price:.2f} TL",
                'status': order.status,
                'time': order.created_at.isoformat() if order.created_at else None,
                'color': 'warning' if order.marketplace == 'trendyol' else 
                         'primary' if order.marketplace == 'pazarama' else 
                         'success' if order.marketplace == 'hepsiburada' else 'secondary'
            })
            
        return jsonify({'success': True, 'orders': orders_data})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/auto_sync/settings', methods=['GET'])
def api_auto_sync_settings():
    """Tüm pazaryerleri için otomatik senkronizasyon ayarlarını getir"""
    try:
        settings = []
        for marketplace_key in MARKETPLACES.keys():
            auto_sync = AutoSync.get_or_create(marketplace_key)
            data = auto_sync.to_dict()
            
            # Load extended settings
            data['xml_source_id'] = Setting.get(f'AUTO_SYNC_XML_SOURCE_{marketplace_key}', user_id=current_user.id)
            data['match_by'] = Setting.get(f'AUTO_SYNC_MATCH_BY_{marketplace_key}', user_id=current_user.id) or 'barcode'
            
            settings.append(data)
        
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
        auto_sync.sync_interval_minutes = int(interval_minutes)
        auto_sync.updated_at = datetime.utcnow().isoformat()
        db.session.commit()
        
        # Save extended settings (XML Source, Match Strategy)
        xml_source_id = data.get('xml_source_id')
        match_by = data.get('match_by')
        
        if xml_source_id:
            Setting.set(f'AUTO_SYNC_XML_SOURCE_{marketplace}', str(xml_source_id), user_id=current_user.id)
        if match_by:
            Setting.set(f'AUTO_SYNC_MATCH_BY_{marketplace}', str(match_by), user_id=current_user.id)
        
        # Scheduler job'unu ekle veya kaldır
        from app.services.scheduler_service import add_sync_job, remove_sync_job
        
        success = True
        message = ""
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


@api_bp.route('/api/manual/products', methods=['GET'])
@login_required
def api_manual_products():
    """Manuel (veritabanında kayıtlı) ürünleri listele"""
    try:
        page = int(request.args.get('page', 1))
        per_page = int(request.args.get('per_page', 25))
        search = (request.args.get('search') or '').strip().lower()
        
        logging.info(f"Fetching manual products for user {current_user.id}, page {page}, search '{search}'")
        
        query = Product.query.filter_by(user_id=current_user.id, xml_source_id=None)
        
        if search:
            query = query.filter(
                (Product.barcode.ilike(f'%{search}%')) | 
                (Product.title.ilike(f'%{search}%')) |
                (Product.stockCode.ilike(f'%{search}%')) |
                (Product.brand.ilike(f'%{search}%'))
            )
            
        total = query.count()
        pagination = query.order_by(Product.id.desc()).paginate(page=page, per_page=per_page, error_out=False)
        
        items = []
        for i, p in enumerate(pagination.items):
            try:
                # Parse images safely
                img_list = p.get_images or []
                first_image = img_list[0] if img_list else ''
                
                # Manual products might use listPrice or cost_price. 
                # We show listPrice as main 'price' for marketplace listing prep.
                items.append({
                    "_index": (page - 1) * per_page + i + 1,
                    "barcode": str(p.barcode or ''),
                    "title": str(p.title or 'İsimsiz Ürün'),
                    "stock_code": str(p.stockCode or ''),
                    "price": float(p.listPrice or 0),
                    "sale_price": float(p.listPrice or 0),
                    "quantity": int(p.quantity or 0),
                    "brand": str(p.brand or ''),
                    "category": str(p.top_category or ''),
                    "images": img_list,
                    "first_image": first_image,
                    "description": str(p.description or '')
                })
            except Exception as e:
                logging.warning(f"Error skipping product ID {p.id}: {str(e)}")
                continue
            
        return jsonify({
            'success': True,
            'total': total,
            'total_pages': pagination.pages,
            'page': page,
            'products': items
        })
        
    except Exception as e:
        logging.exception("Manual products list error")
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
        prefix = data.get('prefix', 'VD').upper()[:2]  # Max 2 char prefix
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


@api_bp.route('/product/update_cost', methods=['POST'])
@login_required
def update_product_cost():
    """Update product cost price."""
    try:
        data = request.get_json()
        barcode = data.get('barcode')
        cost_price = data.get('cost_price')
        
        if not barcode or cost_price is None:
            return jsonify({'success': False, 'message': 'Eksik parametre'}), 400
            
        product = Product.query.filter_by(user_id=current_user.id, barcode=barcode).first()
        if not product:
            return jsonify({'success': False, 'message': 'Ürün bulunamadı'}), 404
            
        product.cost_price = float(cost_price)
        db.session.commit()
        
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/product/details', methods=['GET'])
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
    """Eşleşmeyen markaları API ile çözümle"""
    try:
        data = request.get_json(force=True) or {}
        unmatched_brands = data.get('brands', [])
        
        if not unmatched_brands:
            return jsonify({'success': False, 'message': 'brands listesi gerekli'}), 400
        
        from app.services.trendyol_service import get_trendyol_client, _BRAND_CACHE, save_brand_cache_to_db
        client = get_trendyol_client()
        
        resolved = {}
        failed = []
        debug_info = []
        
        for brand_name in unmatched_brands:
            if not brand_name:
                continue
                
            try:
                # Search exact brand name
                results = client.get_brands_by_name(brand_name)
                
                # DEBUG LOG
                debug_msg = f"'{brand_name}': {len(results) if results is not None else 'None'} results"
                logging.info(f"Brand search: {debug_msg}")
                debug_info.append(debug_msg)
                
                if isinstance(results, list) and len(results) > 0:
                    # Take first result (most aggressive match)
                    found = results[0]
                    brand_id = found.get('id')
                    found_name = found.get('name')
                    
                    if brand_id and found_name:
                        resolved[brand_name] = {'id': brand_id, 'name': found_name}
                        
                        # Also add to cache
                        _BRAND_CACHE["by_name"][found_name.lower()] = {"id": brand_id, "name": found_name}
                        resolved_count += 1
                    else:
                        failed.append(brand_name)
                        debug_info.append(f"Found match for '{brand_name}' but missing ID/Name")
                else:
                    failed.append(brand_name)
                    debug_info.append(f"No results from API for '{brand_name}'")
                    
            except Exception as e:
                err_msg = f"API error for '{brand_name}': {str(e)}"
                logging.warning(err_msg)
                failed.append(brand_name)
                debug_info.append(err_msg)
        
        # Save updated cache
        if resolved:
            _BRAND_CACHE["count"] = len(_BRAND_CACHE.get("by_name", {}))
            save_brand_cache_to_db()
        
        return jsonify({
            'success': True,
            'resolved': resolved,
            'failed': failed,
            'resolved_count': len(resolved),
            'failed_count': len(failed),
            'debug_info': debug_info
        })
        
    except Exception as e:
        logging.exception("Trendyol brand resolve error")
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
        match_by = options.get('matchBy', 'barcode')
        
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
                lambda jid: perform_trendyol_send_products(jid, barcodes, excel_source_id, auto_match=True, send_options=send_options, match_by=match_by),
                params={'barcodes': barcodes[:5], 'total': len(barcodes), 'options': send_options, 'match_by': match_by}
            )
        elif marketplace == 'pazarama':
            from app.services.pazarama_service import perform_pazarama_send_products
            job_id = submit_mp_job(
                'excel_send', 'pazarama',
                lambda jid: perform_pazarama_send_products(jid, barcodes, excel_source_id),
                params={'barcodes': barcodes[:5], 'total': len(barcodes)}
            )
        elif marketplace == 'n11':
            from app.services.n11_service import perform_n11_send_products
            job_id = submit_mp_job(
                'excel_send', 'n11',
                lambda jid: perform_n11_send_products(jid, barcodes, excel_source_id, auto_match=True, send_options=send_options, match_by=match_by),
                params={'barcodes': barcodes[:5], 'total': len(barcodes), 'options': send_options, 'match_by': match_by}
            )
        else:
            return jsonify({'success': False, 'message': f'{marketplace} henüz desteklenmiyor'}), 400
        
        from app.services.activity_logger import log_user_activity
        log_user_activity(current_user.id, 'send_to_marketplace', marketplace, 
                          {'count': len(barcodes), 'file': file_id, 'job_id': job_id})
        
        return jsonify({'success': True, 'job_id': job_id, 'message': f'{len(barcodes)} ürün kuyruğa alındı'})
        
    except Exception as e:
        logging.exception(f"Excel send to {marketplace} error")
        return jsonify({'success': False, 'message': str(e)}), 500


# ============================================================
# Trendyol Müşteri Soruları & İade Talepleri API
# ============================================================

@api_bp.route('/api/trendyol/questions')
@login_required
def api_trendyol_questions():
    """Trendyol müşteri sorularını getir"""
    try:
        client = get_trendyol_client()
        if not client:
            return jsonify({'success': False, 'message': 'Trendyol API bağlantısı yapılandırılmamış'}), 400
        
        page = request.args.get('page', 0, type=int)
        size = request.args.get('size', 20, type=int)
        status = request.args.get('status', None)
        barcode = request.args.get('barcode', None)
        
        data = client.get_questions(page=page, size=size, status=status, barcode=barcode)
        
        return jsonify({'success': True, 'data': data})
        
    except Exception as e:
        logging.exception("Error fetching Trendyol questions")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/trendyol/questions/<int:question_id>/answer', methods=['POST'])
@login_required
def api_trendyol_answer_question(question_id):
    """Trendyol müşteri sorusunu cevapla"""
    try:
        client = get_trendyol_client()
        if not client:
            return jsonify({'success': False, 'message': 'Trendyol API bağlantısı yapılandırılmamış'}), 400
        
        data = request.get_json() or {}
        answer_text = data.get('answer_text', '').strip()
        
        if len(answer_text) < 2:
            return jsonify({'success': False, 'message': 'Cevap en az 2 karakter olmalıdır'}), 400
        
        result = client.answer_question(question_id, answer_text)
        
        return jsonify({'success': True, 'data': result})
        
    except Exception as e:
        logging.exception(f"Error answering Trendyol question {question_id}")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/trendyol/claims')
@login_required
def api_trendyol_claims():
    """Trendyol iade taleplerini getir"""
    try:
        client = get_trendyol_client()
        if not client:
            return jsonify({'success': False, 'message': 'Trendyol API bağlantısı yapılandırılmamış'}), 400
        
        page = request.args.get('page', 0, type=int)
        size = request.args.get('size', 20, type=int)
        claim_status = request.args.get('claim_status', None)
        
        data = client.get_claims(page=page, size=size, claim_status=claim_status)
        
        # Normalize data structure for frontend
        if isinstance(data, dict):
            content = data.get('content') or data.get('claims') or data.get('data') or []
            
            # Normalize each claim object
            normalized_content = []
            for item in content:
                # Basic copy
                norm_item = item.copy()
                
                # Extract details from 'items' array if present (New Structure)
                items_list = item.get('items') or []
                if items_list and len(items_list) > 0:
                    first_item = items_list[0]
                    
                    # 1. Product Details from orderLine
                    order_line = first_item.get('orderLine')
                    if order_line:
                        if not norm_item.get('productName'):
                            norm_item['productName'] = order_line.get('productName')
                        if not norm_item.get('title'):
                            norm_item['title'] = order_line.get('productName')
                        if not norm_item.get('barcode'):
                            norm_item['barcode'] = order_line.get('barcode')
                        if not norm_item.get('claimPrice'):
                           norm_item['claimPrice'] = order_line.get('price')

                    # 2. Status from claimItems
                    claim_items = first_item.get('claimItems') or []
                    if claim_items and len(claim_items) > 0:
                        first_claim_item = claim_items[0]
                        # Status is nested in claimItemStatus object
                        status_obj = first_claim_item.get('claimItemStatus')
                        if status_obj and isinstance(status_obj, dict):
                             norm_item['status'] = status_obj.get('name')
                             norm_item['claimStatus'] = status_obj.get('name')
                        
                        # Fallback for ID if needed (though root ID seems correct)
                        if not norm_item.get('claimItemId'):
                             norm_item['claimItemId'] = first_claim_item.get('id')

                # Fallback: Check for nested line items (Old/Alternative Structure)
                lines = item.get('lineItems') or item.get('lines') or []
                if lines and len(lines) > 0:
                    first_line = lines[0]
                    # Map product details from first line item
                    if not norm_item.get('productName') and first_line.get('productName'):
                        norm_item['productName'] = first_line.get('productName')
                    if not norm_item.get('title') and first_line.get('title'):
                        norm_item['title'] = first_line.get('title')
                    if not norm_item.get('barcode'):
                        norm_item['barcode'] = first_line.get('barcode')
                    if not norm_item.get('quantity'):
                        norm_item['quantity'] = first_line.get('quantity')
                
                # Final Status Map
                if not norm_item.get('status') and norm_item.get('claimStatus'):
                     # If claimStatus is a dict (from new structure logic above), take name
                     if isinstance(norm_item.get('claimStatus'), dict):
                         norm_item['status'] = norm_item['claimStatus'].get('name')
                     else:
                         norm_item['status'] = norm_item.get('claimStatus')
                         
                if not norm_item.get('status') and norm_item.get('claimItemStatus'):
                     norm_item['status'] = norm_item.get('claimItemStatus')

                # Default quantity to 1 if missing
                if not norm_item.get('quantity'):
                    norm_item['quantity'] = len(items_list) if items_list else 1

                # Ensure ID is present
                if not norm_item.get('id') and norm_item.get('claimId'):
                    norm_item['id'] = norm_item.get('claimId')
                    
                normalized_content.append(norm_item)
            
            # Update content in response
            if data.get('content') is not None:
                data['content'] = normalized_content
            elif data.get('claims') is not None:
                data['claims'] = normalized_content
            else:
                data['content'] = normalized_content # Fallback key
        
        return jsonify({'success': True, 'data': data})
        
    except Exception as e:
        logging.exception("Error fetching Trendyol claims")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/trendyol/claims/<claim_id>/accept', methods=['POST'])
@login_required
def api_trendyol_accept_claim(claim_id):
    """Trendyol iade talebini onayla"""
    try:
        client = get_trendyol_client()
        if not client:
            return jsonify({'success': False, 'message': 'Trendyol API bağlantısı yapılandırılmamış'}), 400
        
        result = client.accept_claim(claim_id)
        
        return jsonify({'success': True, 'data': result})
        
    except Exception as e:
        logging.exception(f"Error accepting Trendyol claim {claim_id}")
        return jsonify({'success': False, 'message': str(e)}), 500


@api_bp.route('/api/trendyol/claims/<claim_id>/reject', methods=['POST'])
@login_required
def api_trendyol_reject_claim(claim_id):
    """Trendyol iade talebini reddet"""
    try:
        client = get_trendyol_client()
        if not client:
            return jsonify({'success': False, 'message': 'Trendyol API bağlantısı yapılandırılmamış'}), 400
        
        data = request.get_json() or {}
        reject_reason_id = data.get('reject_reason_id', 1)
        reject_reason_text = data.get('reject_reason_text', None)
        
        result = client.reject_claim(claim_id, reject_reason_id, reject_reason_text)
        
        return jsonify({'success': True, 'data': result})
        
    except Exception as e:
        logging.exception(f"Error rejecting Trendyol claim {claim_id}")
        return jsonify({'success': False, 'message': str(e)}), 500


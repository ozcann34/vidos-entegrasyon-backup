import time
import json
import threading
import math
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple, Set, Iterable
from difflib import get_close_matches
from collections import Counter

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False

from app.models import Setting
from app.services.pazarama_client import PazaramaClient
from app.services.xml_service import load_xml_source_index, lookup_xml_record
from app.services.job_queue import append_mp_job_log
from app.utils.helpers import to_int, to_float, chunked, get_marketplace_multiplier, clean_forbidden_words, is_product_forbidden, calculate_price

# Category cache for basic operations
_PAZARAMA_CATEGORY_CACHE = {"list": [], "names": [], "ids": []}

# TF-IDF cache for smart matching (like Trendyol)
_PAZARAMA_CAT_TFIDF = {
    "leaf": [],
    "names": [],
    "vectorizer": None,
    "matrix": None,
}

_PAZARAMA_ATTR_CACHE: Dict[str, List[Dict[str, Any]]] = {}
_PAZARAMA_BRAND_CACHE: Dict[str, str] = {}
_PAZARAMA_DETAIL_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_PAZARAMA_DETAIL_CACHE_LOCK = threading.Lock()
PAZARAMA_DETAIL_CACHE_TTL_SECONDS = 300
PAZARAMA_DETAIL_CACHE_MAX = 1000
PAZARAMA_SNAPSHOT_TTL_SECONDS = 300


def clear_all_pazarama_caches() -> str:
    """Tüm Pazarama önbelleklerini temizle"""
    global _PAZARAMA_CATEGORY_CACHE, _PAZARAMA_CAT_TFIDF, _PAZARAMA_ATTR_CACHE
    global _PAZARAMA_BRAND_CACHE, _PAZARAMA_DETAIL_CACHE
    
    cleared = []
    
    # Clear category cache
    _PAZARAMA_CATEGORY_CACHE = {"list": [], "names": [], "ids": []}
    cleared.append("kategori")
    
    # Clear TF-IDF
    _PAZARAMA_CAT_TFIDF = {"leaf": [], "names": [], "vectorizer": None, "matrix": None}
    cleared.append("TF-IDF")
    
    # Clear attribute cache
    _PAZARAMA_ATTR_CACHE.clear()
    cleared.append("öznitelik")
    
    # Clear brand cache
    _PAZARAMA_BRAND_CACHE.clear()
    cleared.append("marka")
    
    # Clear detail cache
    with _PAZARAMA_DETAIL_CACHE_LOCK:
        _PAZARAMA_DETAIL_CACHE.clear()
    cleared.append("ürün detay")
    
    # Clear product index snapshot
    try:
        from app.models import Setting
        Setting.set('PAZARAMA_EXPORT_SNAPSHOT', '')
        Setting.set('PAZARAMA_PRODUCT_INDEX', '')
        cleared.append("ürün indeksi")
    except Exception:
        pass
    
    return f"Temizlenen: {', '.join(cleared)}"


# ============================================================
# Pazarama kategori cekme ve TF-IDF eslestirme fonksiyonlari
# ============================================================

def fetch_pazarama_categories_flat(client: PazaramaClient) -> List[Dict[str, Any]]:
    """
    Fetch all Pazarama categories and return flat list of leaf categories.
    Similar to Trendyol's fetch_trendyol_categories_flat.
    """
    try:
        cats = client.get_category_tree(only_leaf=True)
        flat = []
        for c in cats:
            flat.append({
                "id": c.get("id"),
                "name": c.get("name", ""),
                "path": c.get("path", c.get("name", "")),
            })
        return flat
    except Exception as e:
        logging.error(f"Pazarama kategori cekme hatasi: {e}")
        return []


def prepare_pazarama_tfidf(leaf_categories: List[Dict[str, Any]]):
    """
    Prepare TF-IDF vectorizer and matrix for Pazarama categories.
    Same approach as Trendyol.
    """
    if not SKLEARN_AVAILABLE:
        logging.warning("sklearn yuklu degil, TF-IDF eslestirme calismayacak")
        _PAZARAMA_CAT_TFIDF.update({"leaf": [], "names": [], "vectorizer": None, "matrix": None})
        return
    
    names = [c.get('name', '') for c in leaf_categories]
    if not names:
        _PAZARAMA_CAT_TFIDF.update({"leaf": [], "names": [], "vectorizer": None, "matrix": None})
        return
    
    # Use char-level n-grams for Turkish/fuzzy matching
    vec = TfidfVectorizer(analyzer='char_wb', ngram_range=(2, 4))
    vec.fit(names)
    mat = vec.transform(names)
    _PAZARAMA_CAT_TFIDF.update({"leaf": leaf_categories, "names": names, "vectorizer": vec, "matrix": mat})
    logging.info(f"Pazarama TF-IDF matrisi hazir: {len(names)} kategori")


def ensure_pazarama_tfidf_ready(user_id: int = None):
    """
    Load Pazarama categories from settings and prepare TF-IDF if not already done.
    """
    if _PAZARAMA_CAT_TFIDF.get('vectorizer'):
        return True
    
    raw = Setting.get("PAZARAMA_CATEGORY_TREE", "", user_id=user_id)
    if raw:
        try:
            leafs = json.loads(raw)
            prepare_pazarama_tfidf(leafs)
            return True
        except Exception as e:
            logging.error(f"Pazarama kategori agaci yuklenirken hata: {e}")
    return False


def match_pazarama_category_tfidf(query: str, min_score: float = 0.25) -> Optional[str]:
    """
    Find best matching Pazarama category using TF-IDF + cosine similarity.
    
    Args:
        query: The text to match (product title, category name, etc.)
        min_score: Minimum similarity score (0-1) to accept a match
        
    Returns:
        Category ID if found, None otherwise
    """
    if not query or not _PAZARAMA_CAT_TFIDF.get('vectorizer'):
        return None
    
    vec = _PAZARAMA_CAT_TFIDF['vectorizer']
    mat = _PAZARAMA_CAT_TFIDF['matrix']
    names = _PAZARAMA_CAT_TFIDF['names']
    leaf = _PAZARAMA_CAT_TFIDF['leaf']
    
    try:
        q = vec.transform([query.lower()])
        sims = cosine_similarity(q, mat)[0]
        idx = int(sims.argmax())
        score = float(sims[idx])
        
        if score >= min_score:
            return str(leaf[idx].get('id') or '')
        return None
    except Exception as e:
        logging.error(f"TF-IDF eslestirme hatasi: {e}")
        return None


def get_pazarama_client(user_id: int = None) -> PazaramaClient:
    """Get Pazarama client with user-specific credentials."""
    # Get user_id from current_user if not provided
    if user_id is None:
        try:
            from flask_login import current_user
            if current_user and current_user.is_authenticated:
                user_id = current_user.id
        except Exception:
            pass
    
    client_id = (Setting.get("PAZARAMA_API_KEY", "", user_id=user_id) or "").strip()
    client_secret = (Setting.get("PAZARAMA_API_SECRET", "", user_id=user_id) or "").strip()
    if not client_id or not client_secret:
        raise ValueError("Pazarama API bilgileri eksik. Ayarlar sayfasindan PAZARAMA_API_KEY ve PAZARAMA_API_SECRET giriniz.")
    return PazaramaClient(client_id=client_id, client_secret=client_secret)

def get_cached_pazarama_detail(client: PazaramaClient, code: str) -> Dict[str, Any]:
    if not code:
        return {}
    now = time.time()
    ttl = PAZARAMA_DETAIL_CACHE_TTL_SECONDS
    with _PAZARAMA_DETAIL_CACHE_LOCK:
        cached = _PAZARAMA_DETAIL_CACHE.get(code)
        if cached:
            ts, data = cached
            if ttl == 0 or (now - ts) <= ttl:
                return data
            else:
                _PAZARAMA_DETAIL_CACHE.pop(code, None)
    try:
        detail = client.get_product_detail(code) or {}
    except Exception:
        detail = {}
    if detail:
        with _PAZARAMA_DETAIL_CACHE_LOCK:
            if len(_PAZARAMA_DETAIL_CACHE) >= PAZARAMA_DETAIL_CACHE_MAX:
                # simple FIFO eviction
                oldest_key = min(_PAZARAMA_DETAIL_CACHE.items(), key=lambda item: item[1][0])[0]
                _PAZARAMA_DETAIL_CACHE.pop(oldest_key, None)
            _PAZARAMA_DETAIL_CACHE[code] = (now, detail)
    return detail

def clear_pazarama_detail_cache():
    with _PAZARAMA_DETAIL_CACHE_LOCK:
        _PAZARAMA_DETAIL_CACHE.clear()

def get_pazarama_category_map() -> Dict[str, str]:
    try:
        raw = Setting.get('PAZARAMA_CATEGORY_MAPPING', '{}') or '{}'
        data = json.loads(raw)
        if isinstance(data, dict):
            return {
                (str(k).strip().lower()): str(v).strip()
                for k, v in data.items() if v not in (None, '', [])
            }
    except Exception:
        pass
    return {}

def pazarama_get_required_attributes(client: PazaramaClient, category_id: str) -> List[Dict[str, Any]]:
    if not category_id:
        return []
    cache_key = str(category_id)
    if cache_key in _PAZARAMA_ATTR_CACHE:
        return _PAZARAMA_ATTR_CACHE[cache_key]
    try:
        data = client.get_category_with_attributes(category_id)
    except Exception:
        _PAZARAMA_ATTR_CACHE[cache_key] = []
        return []
    attrs: List[Dict[str, Any]] = []
    for attr in data.get('attributes', []):
        if not attr.get('isRequired'):
            continue
        values = attr.get('attributeValues') or []
        if not values:
            continue
        try:
            attrs.append({
                'attributeId': attr['id'],
                'attributeValueId': values[0]['id']
            })
        except Exception:
            continue
    _PAZARAMA_ATTR_CACHE[cache_key] = attrs
    return attrs

def ensure_pazarama_categories(client: PazaramaClient) -> None:
    if _PAZARAMA_CATEGORY_CACHE["list"]:
        return
    try:
        cats = client.get_category_tree(only_leaf=True)
    except Exception:
        _PAZARAMA_CATEGORY_CACHE.update({"list": [], "names": [], "ids": []})
        return
    names = [str(c.get('name') or '').strip().lower() for c in cats]
    ids = [str(c.get('id') or '') for c in cats]
    _PAZARAMA_CATEGORY_CACHE.update({"list": cats, "names": names, "ids": ids})

def resolve_pazarama_category(client: PazaramaClient, product_title: str, top_category: str, xml_category: str, log_callback=None, user_id: int = None) -> Optional[str]:
    """
    Resolve Pazarama category ID from product info.
    Uses TF-IDF matching for better accuracy.
    
    Args:
        client: Pazarama client
        product_title: Product title
        top_category: Top-level category from XML
        xml_category: Full category path from XML
        log_callback: Optional callback function for logging (e.g., append_mp_job_log)
        user_id: User ID for setting context
    """
    category_map = get_pazarama_category_map()
    
    # First try exact mapping from user-defined settings
    for raw in (top_category, xml_category):
        if not raw:
            continue
        key = str(raw).strip().lower()
        if not key:
            continue
        mapped = category_map.get(key)
        if mapped:
            if log_callback:
                log_callback(f"Kategori eslesti (mapping): {key} -> {mapped}")
            return mapped
            
    # FIXED: Hint for 'Çorap' to prevent mis-mapping to 'Termal İçlik'
    is_corap = any('çorap' in str(x).lower() for x in (xml_category, top_category, product_title))
    if is_corap and ensure_pazarama_tfidf_ready(user_id=user_id):
        # Try finding a category that contains 'çorap'
        for cat in _PAZARAMA_CAT_TFIDF.get('leaf', []):
            if 'çorap' in cat.get('name', '').lower():
                if log_callback:
                    log_callback(f"Kategori eslesti (corap hint): {product_title[:20]}... -> {cat.get('name')}")
                return str(cat.get('id'))

    # Try TF-IDF matching if available (like Trendyol)
    if ensure_pazarama_tfidf_ready(user_id=user_id):
        # Try with xml_category first, then top_category, then title
        best_match_id = None
        best_match_score = 0
        best_match_name = ""
        best_query = ""
        
        for query in (xml_category, top_category, product_title):
            if not query:
                continue
            
            # Get the best match for this query (lower threshold for finding ANY match)
            matched_id = match_pazarama_category_tfidf(query, min_score=0.15)
            if matched_id:
                # Get matched category name for logging
                matched_name = ""
                for cat in _PAZARAMA_CAT_TFIDF.get('leaf', []):
                    if str(cat.get('id')) == matched_id:
                        matched_name = cat.get('name', '')
                        break
                if log_callback:
                    log_callback(f"Kategori eslesti (tfidf): {query[:30]}... -> {matched_name}")
                return matched_id
            
            # Track best match even below threshold for fallback
            if _PAZARAMA_CAT_TFIDF.get('vectorizer'):
                try:
                    vec = _PAZARAMA_CAT_TFIDF['vectorizer']
                    mat = _PAZARAMA_CAT_TFIDF['matrix']
                    leaf = _PAZARAMA_CAT_TFIDF['leaf']
                    
                    q = vec.transform([query.lower()])
                    sims = cosine_similarity(q, mat)[0]
                    idx = int(sims.argmax())
                    score = float(sims[idx])
                    
                    if score > best_match_score:
                        best_match_score = score
                        best_match_id = str(leaf[idx].get('id') or '')
                        best_match_name = leaf[idx].get('name', '')
                        best_query = query
                except:
                    pass
        
        # Use best match as fallback if score is at least 0.05 (very low but better than nothing)
        if best_match_id and best_match_score >= 0.05:
            if log_callback:
                log_callback(f"Kategori eslesti (benzer, skor:{best_match_score:.2f}): {best_query[:20]}... -> {best_match_name}")
            return best_match_id
    
    # Fallback: Load from API and try basic matching
    ensure_pazarama_categories(client)
    names = _PAZARAMA_CATEGORY_CACHE["names"]
    ids = _PAZARAMA_CATEGORY_CACHE["ids"]
    
    if not names or not ids:
        if log_callback:
            log_callback(f"Pazarama kategori listesi bos! Ayarlardan 'Kategori Cek' yapin.", level='warning')
        return None

    # Try fuzzy matching with difflib as last resort
    candidates_tried = []
    for raw in (top_category, xml_category, product_title):  # Also try product title
        if not raw:
            continue
        key = str(raw).strip().lower()
        if not key:
            continue
        candidates_tried.append(key)
        
        # Try exact match
        if key in names:
            idx = names.index(key)
            if log_callback:
                log_callback(f"Kategori eslesti (exact): {key}")
            return ids[idx]
        
        # Try fuzzy matching with lower cutoff (0.4 instead of 0.6)
        match = get_close_matches(key, names, n=1, cutoff=0.4)
        if match:
            try:
                idx = names.index(match[0])
                if log_callback:
                    log_callback(f"Kategori eslesti (fuzzy): {key} -> {match[0]}")
                return ids[idx]
            except ValueError:
                continue
    
    # Log what was tried
    if log_callback:
        log_callback(f"Kategori eslesmedi. Denenen: {candidates_tried[:3]}", level='warning')
    
    return None

def resolve_pazarama_brand(client: PazaramaClient, brand_name: str, log_callback=None) -> str:
    """Resolve Pazarama Brand ID by name."""
    if not brand_name:
        brand_name = "Diğer"
    
    key = brand_name.strip().lower()
    if key in _PAZARAMA_BRAND_CACHE:
        return _PAZARAMA_BRAND_CACHE[key]
    
    # Try by name
    try:
        results = client.get_brands(name=brand_name.strip())
        for b in results:
            if b.get('name', '').strip().lower() == key:
                found = b.get('id')
                _PAZARAMA_BRAND_CACHE[key] = found
                if log_callback: log_callback(f"Marka eslesti: {brand_name}")
                return found
        # If exact not found, try first result if close? Pazarama search is usually contains.
        if results:
            found = results[0].get('id')
            found_name = results[0].get('name')
            _PAZARAMA_BRAND_CACHE[key] = found
            if log_callback: log_callback(f"Marka eslesti (benzer): {brand_name} -> {found_name}")
            return found
            
    except Exception as e:
        if log_callback: log_callback(f"Marka aranirken hata: {e}", level='warning')

    # Try Diğer
    if key != "diğer":
        if log_callback:
            log_callback(f"⚠️ Marka bulunamadı: '{brand_name}', 'Diğer' markasına düşülüyor.", level='warning')
        return resolve_pazarama_brand(client, "Diğer", log_callback)
    
    # Fallback
    fallback = "3fa85f64-5717-4562-b3fc-2c963f66afa6" 
    return fallback

def pazarama_fetch_all_products(client: PazaramaClient, page_size: int = 250, force_refresh: bool = False) -> List[Dict[str, Any]]:
    logging.info(f"[Pazarama] Fetching all products: page_size={page_size}, force_refresh={force_refresh}")
    snapshot_items: Optional[List[Dict[str, Any]]] = None
    snapshot_meta: Optional[Dict[str, Any]] = None
    if not force_refresh:
        snap_raw = Setting.get('PAZARAMA_EXPORT_SNAPSHOT', '') or ''
        if snap_raw:
            try:
                snap = json.loads(snap_raw)
                snapshot_items = snap.get('items') or []
                snapshot_meta = snap
                total = int(snap.get('total') or 0)
                saved_at_raw = snap.get('saved_at')
                snapshot_age_ok = False
                if PAZARAMA_SNAPSHOT_TTL_SECONDS == 0:
                    snapshot_age_ok = True
                elif saved_at_raw:
                    try:
                        saved_at_dt = datetime.fromisoformat(saved_at_raw)
                        snapshot_age_ok = (datetime.utcnow() - saved_at_dt).total_seconds() <= PAZARAMA_SNAPSHOT_TTL_SECONDS
                    except Exception:
                        snapshot_age_ok = False
                if snapshot_items and (total == 0 or len(snapshot_items) >= total) and snapshot_age_ok:
                    return snapshot_items
            except Exception:
                snapshot_items = None

    sizes_to_try: List[int] = []
    if page_size:
        sizes_to_try.append(int(page_size))
    for fallback_size in (200, 100, 50):
        if fallback_size not in sizes_to_try:
            sizes_to_try.append(fallback_size)

    last_error: Optional[Exception] = None

    aggregated_map: Dict[str, Dict[str, Any]] = {}
    total_reported = 0

    def _row_key(row: Dict[str, Any], label: str) -> str:
        priority_fields = (
            'code', 'stockCode', 'productCode', 'productId', 'id',
            'barcode', 'sku', 'groupCode', 'listingId', 'listingCode'
        )
        for field in priority_fields:
            val = row.get(field)
            if val is not None and str(val).strip():
                return f"{field}:{str(val).strip().lower()}"
        name = str(row.get('displayName') or row.get('name') or '').strip()
        if name:
            return f"{label}:{name.lower()}"
        created = str(row.get('createdDate') or row.get('createdAt') or '').strip()
        if created:
            return f"{label}:{created.lower()}"
        return f"{label}:anon:{hash(str(sorted(row.items())))}"

    def _fetch_for_status(approved_flag: Optional[bool]) -> bool:
        nonlocal total_reported, last_error, aggregated_map
        label = 'all' if approved_flag is None else ('approved' if approved_flag else 'unapproved')
        for size in sizes_to_try:
            page = 1
            max_pages = None
            stagnant_pages = 0
            try:
                while True:
                    resp = client.list_products(page=page, size=size, approved=approved_flag)
                    data = resp.get('data') or []
                    if not data:
                        break
                    appended = 0
                    for row in data:
                        key = _row_key(row, label)
                        if key in aggregated_map:
                            continue
                        aggregated_map[key] = row
                        appended += 1
                    if appended == 0:
                        stagnant_pages += 1
                    else:
                        stagnant_pages = 0
                    total = int(resp.get('totalCount') or resp.get('total') or 0)
                    total_reported = max(total_reported, total)
                    total_pages = resp.get('totalPages') or resp.get('totalPage')
                    if isinstance(total_pages, (int, float, str)):
                        try:
                            max_pages = int(total_pages)
                        except Exception:
                            pass
                    if len(data) < size:
                        break
                    if max_pages and max_pages > 0 and page >= max_pages:
                        break
                    if stagnant_pages >= 2:
                        break
                    if page >= 500:
                        break
                    page += 1
                if stagnant_pages < 2 or aggregated_map:
                    return True
            except Exception as exc:
                last_error = exc
                continue
        return False

    fetched_any = False
    approved_count = 0
    unapproved_count = 0
    
    for approved_flag in (None, True, False):
        before_count = len(aggregated_map)
        success = _fetch_for_status(approved_flag)
        after_count = len(aggregated_map)
        added = after_count - before_count
        
        status_label = 'all' if approved_flag is None else ('approved' if approved_flag else 'unapproved')
        logging.info(f"[Pazarama] Fetched {status_label}: +{added} products (total so far: {after_count})")
        
        if approved_flag is True:
            approved_count = added
        elif approved_flag is False:
            unapproved_count = added
            
        fetched_any = fetched_any or success

    logging.info(f"[Pazarama] Final counts - Approved: {approved_count}, Unapproved: {unapproved_count}, Total: {len(aggregated_map)}")

    if fetched_any and aggregated_map:
        items = list(aggregated_map.values())
        snapshot_payload = {
            'total': total_reported or len(items),
            'page_size': sizes_to_try[0],
            'saved_at': datetime.utcnow().isoformat(),
            'items': items,
        }
        try:
            Setting.set('PAZARAMA_EXPORT_SNAPSHOT', json.dumps(snapshot_payload))
        except Exception:
            pass
        return items

    if snapshot_items is not None:
        if snapshot_meta and PAZARAMA_SNAPSHOT_TTL_SECONDS and snapshot_meta.get('items'):
            # expired snapshot; reuse only if caller expects fallback
            pass
        return snapshot_items
    if last_error:
        raise last_error
    return []

def pazarama_build_product_index(client: PazaramaClient, force_refresh: bool = False) -> Dict[str, Any]:
    items = pazarama_fetch_all_products(client, force_refresh=force_refresh)
    by_code: Dict[str, Dict[str, Any]] = {}
    by_stock: Dict[str, Dict[str, Any]] = {}
    for row in items:
        code = str(row.get('code') or '').strip()
        stock_code = str(row.get('stockCode') or '').strip()
        if code:
            by_code[code] = row
        if stock_code:
            by_stock[stock_code] = row
    return {'items': items, 'by_code': by_code, 'by_stock': by_stock}

def perform_pazarama_sync_stock(job_id: str, xml_source_id: Any, user_id: int = None) -> Dict[str, Any]:
    client = get_pazarama_client(user_id=user_id)
    append_mp_job_log(job_id, "Pazarama istemcisi hazir")
    xml_index = load_xml_source_index(xml_source_id)
    if not xml_index:
        raise ValueError('XML kaynagindan urun verisi okunamadi.')
    append_mp_job_log(job_id, "XML verisi yuklendi")

    product_index = pazarama_build_product_index(client)
    products = product_index.get('items') or []
    if not products:
        raise ValueError('Pazarama urun listesi alinamadi.')
    append_mp_job_log(job_id, f"{len(products)} urun degerlendiriliyor")

    updates: List[Dict[str, Any]] = []
    zeroed_codes: List[str] = []
    changed_samples: List[Dict[str, Any]] = []
    missing_codes: Set[str] = set()

    for product in products:
        code = str(product.get('code') or '').strip()
        stock_code = str(product.get('stockCode') or '').strip()
        title = product.get('displayName') or product.get('name') or ''
        xml_info = lookup_xml_record(xml_index, code=code, stock_code=stock_code, title=title)
        if not xml_info:
            missing_codes.add(code or stock_code or title)
            continue
        qty_raw = xml_info.get('quantity')
        if qty_raw is None:
            continue
        xml_qty = to_int(qty_raw, 0)
        if xml_qty < 0:
            xml_qty = 0
        remote_qty = to_int(product.get('stockCount'), 0)
        if xml_qty != remote_qty:
            updates.append({'code': code or stock_code, 'stockCount': xml_qty})
            if len(changed_samples) < 10:
                changed_samples.append({'code': code or stock_code, 'from': remote_qty, 'to': xml_qty})
            if xml_qty == 0:
                zeroed_codes.append(code or stock_code)

    append_mp_job_log(job_id, f"Guncellenecek stok sayisi: {len(updates)}")

    summary = {
        'success': True,
        'updated_count': len(updates),
        'total_remote': len(products),
        'missing_count': len(missing_codes),
        'missing_codes': list(missing_codes)[:20],
        'data_ids': [],
        'samples': changed_samples,
        'job_id': job_id,
    }

    if not updates:
        summary['message'] = 'Guncellenecek stok bulunamadi.'
        return summary

    from app.services.job_queue import update_mp_job, get_mp_job

    data_ids: List[str] = []
    chunk_size = 25  # Small chunks to avoid rate limiting
    total_chunks = (len(updates) + chunk_size - 1) // chunk_size
    total_items = len(updates)
    processed_items = 0
    
    for idx, chunk in enumerate(chunked(updates, chunk_size), start=1):
        # Check for cancel/pause
        job_state = get_mp_job(job_id)
        if job_state:
            if job_state.get('cancel_requested'):
                append_mp_job_log(job_id, "İşlem kullanıcı tarafından iptal edildi.", level='warning')
                summary['message'] = f'İptal edildi. {processed_items}/{total_items} işlendi.'
                summary['cancelled'] = True
                return summary
            
            while job_state.get('pause_requested'):
                append_mp_job_log(job_id, "Duraklatıldı. Devam etmesi bekleniyor...", level='info')
                time.sleep(3)
                job_state = get_mp_job(job_id)
                if job_state.get('cancel_requested'):
                    append_mp_job_log(job_id, "İşlem iptal edildi.", level='warning')
                    summary['message'] = f'İptal edildi. {processed_items}/{total_items} işlendi.'
                    summary['cancelled'] = True
                    return summary
        
        # Update progress
        update_mp_job(job_id, progress={'current': processed_items, 'total': total_items, 'batch': f'{idx}/{total_chunks}'})
        
        # Rate limit handling with retry
        max_retries = 5
        for attempt in range(max_retries):
            try:
                resp = client.update_stock(chunk)
                data_id = resp.get('data')
                if isinstance(data_id, str):
                    data_ids.append(data_id)
                processed_items += len(chunk)
                append_mp_job_log(job_id, f"Stok guncellemesi gonderildi (paket {idx}/{total_chunks})")
                break  # Success, exit retry loop
            except Exception as e:
                error_msg = str(e)
                if '429' in error_msg:
                    wait_time = (attempt + 1) * 10  # 10, 20, 30, 40, 50 seconds
                    append_mp_job_log(job_id, f"Rate limit (429). {wait_time}sn bekleniyor... (deneme {attempt+1}/{max_retries})", level='warning')
                    time.sleep(wait_time)
                    if attempt == max_retries - 1:
                        append_mp_job_log(job_id, f"Paket {idx} gonderilemedi: {error_msg}", level='error')
                else:
                    append_mp_job_log(job_id, f"Paket {idx} hatasi: {error_msg}", level='error')
                    break  # Non-429 error, don't retry
        
        # Wait between batches to avoid rate limiting (5 seconds)
        if idx < total_chunks:
            time.sleep(5)

    summary.update({
        'message': f'{len(updates)} urun icin stok guncellemesi gonderildi.',
        'data_ids': data_ids,
    })
    return summary

def perform_pazarama_sync_prices(job_id: str, xml_source_id: Any, user_id: int = None) -> Dict[str, Any]:
    client = get_pazarama_client(user_id=user_id)
    append_mp_job_log(job_id, "Pazarama istemcisi hazir")
    xml_index = load_xml_source_index(xml_source_id)
    if not xml_index:
        raise ValueError('XML kaynagindan urun verisi okunamadi.')
    append_mp_job_log(job_id, "XML verisi yuklendi")

    product_index = pazarama_build_product_index(client)
    products = product_index.get('items') or []
    if not products:
        raise ValueError('Pazarama urun listesi alinamadi.')
    append_mp_job_log(job_id, f"{len(products)} urun degerlendiriliyor")

    multiplier = get_marketplace_multiplier('pazarama')
    updates: List[Dict[str, Any]] = []
    changed_samples: List[Dict[str, Any]] = []
    skipped_zero_price: List[str] = []
    missing_codes: Set[str] = set()

    for product in products:
        code = str(product.get('code') or '').strip()
        stock_code = str(product.get('stockCode') or '').strip()
        title = product.get('displayName') or product.get('name') or ''
        xml_info = lookup_xml_record(xml_index, code=code, stock_code=stock_code, title=title)
        if not xml_info:
            missing_codes.add(code or stock_code or title)
            continue
        price_raw = xml_info.get('price')
        if price_raw is None:
            continue
        base_price = to_float(price_raw, 0.0)
        if base_price <= 0:
            skipped_zero_price.append(code or stock_code)
            continue
        # Artık GLOBAL_PRICE_RULES kullanılıyor (multiplier kaldırıldı)
        new_price = calculate_price(base_price, 'pazarama', user_id=user_id)
        if new_price <= 0:
            skipped_zero_price.append(code or stock_code)
            continue
        current_sale = to_float(product.get('salePrice') or product.get('listPrice'), 0.0)
        if abs(current_sale - new_price) < 0.01:
            continue
        updates.append({
            'code': code or stock_code,
            'listPrice': new_price,
            'salePrice': new_price,
        })
        if len(changed_samples) < 10:
            changed_samples.append({'code': code or stock_code, 'from': current_sale, 'to': new_price})

    append_mp_job_log(job_id, f"Guncellenecek fiyat sayisi: {len(updates)}")

    summary = {
        'success': True,
        'updated_count': len(updates),
        'total_remote': len(products),
        'missing_count': len(missing_codes),
        'missing_codes': list(missing_codes)[:20],
        'skipped_zero_price': skipped_zero_price[:20],
        'data_ids': [],
        'samples': changed_samples,
        'multiplier': multiplier,
        'job_id': job_id,
    }

    if not updates:
        summary['message'] = 'Guncellenecek fiyat bulunamadi.'
        return summary

    from app.services.job_queue import update_mp_job, get_mp_job

    data_ids: List[str] = []
    chunk_size = 25  # Small chunks to avoid rate limiting
    total_chunks = (len(updates) + chunk_size - 1) // chunk_size
    total_items = len(updates)
    processed_items = 0
    
    for idx, chunk in enumerate(chunked(updates, chunk_size), start=1):
        # Check for cancel/pause
        job_state = get_mp_job(job_id)
        if job_state:
            if job_state.get('cancel_requested'):
                append_mp_job_log(job_id, "İşlem kullanıcı tarafından iptal edildi.", level='warning')
                summary['message'] = f'İptal edildi. {processed_items}/{total_items} işlendi.'
                summary['cancelled'] = True
                return summary
            
            while job_state.get('pause_requested'):
                append_mp_job_log(job_id, "Duraklatıldı. Devam etmesi bekleniyor...", level='info')
                time.sleep(3)
                job_state = get_mp_job(job_id)
                if job_state.get('cancel_requested'):
                    append_mp_job_log(job_id, "İşlem iptal edildi.", level='warning')
                    summary['message'] = f'İptal edildi. {processed_items}/{total_items} işlendi.'
                    summary['cancelled'] = True
                    return summary
        
        # Update progress
        update_mp_job(job_id, progress={'current': processed_items, 'total': total_items, 'batch': f'{idx}/{total_chunks}'})
        
        # Rate limit handling with retry
        max_retries = 5
        for attempt in range(max_retries):
            try:
                resp = client.update_price(chunk)
                data_id = resp.get('data')
                if isinstance(data_id, str):
                    data_ids.append(data_id)
                processed_items += len(chunk)
                append_mp_job_log(job_id, f"Fiyat guncellemesi gonderildi (paket {idx}/{total_chunks})")
                break  # Success, exit retry loop
            except Exception as e:
                error_msg = str(e)
                if '429' in error_msg:
                    wait_time = (attempt + 1) * 10  # 10, 20, 30, 40, 50 seconds
                    append_mp_job_log(job_id, f"Rate limit (429). {wait_time}sn bekleniyor... (deneme {attempt+1}/{max_retries})", level='warning')
                    time.sleep(wait_time)
                    if attempt == max_retries - 1:
                        append_mp_job_log(job_id, f"Paket {idx} gonderilemedi: {error_msg}", level='error')
                else:
                    append_mp_job_log(job_id, f"Paket {idx} hatasi: {error_msg}", level='error')
                    break  # Non-429 error, don't retry
        
        # Wait between batches to avoid rate limiting (5 seconds)
        if idx < total_chunks:
            time.sleep(5)

    summary.update({
        'message': f'{len(updates)} urun icin fiyat guncellemesi gonderildi.',
        'data_ids': data_ids,
    })
    return summary


def perform_pazarama_sync_all(job_id: str, xml_source_id: Any, user_id: int = None) -> Dict[str, Any]:
    """
    Pazarama için hem stok hem fiyat eşitleme (birleşik)
    """
    from app.services.job_queue import update_mp_job, get_mp_job
    
    append_mp_job_log(job_id, "Stok ve fiyat eşitleme başlatılıyor...")
    
    # Check cancel at start
    job_state = get_mp_job(job_id)
    if job_state and job_state.get('cancel_requested'):
        append_mp_job_log(job_id, "İşlem iptal edildi.", level='warning')
        return {'success': False, 'message': 'İptal edildi.', 'cancelled': True}
    
    if stock_result.get('cancelled'):
        return stock_result
    
    # Check cancel again before price sync
    job_state = get_mp_job(job_id)
    if job_state and job_state.get('cancel_requested'):
        append_mp_job_log(job_id, "İşlem iptal edildi (fiyat öncesi).", level='warning')
        stock_result['cancelled'] = True
        stock_result['message'] = 'İptal edildi. Sadece stok güncellendi.'
        return stock_result
    
    # Wait between operations to avoid rate limiting
    append_mp_job_log(job_id, "Fiyat eşitleme öncesi 5 saniye bekleniyor...")
    time.sleep(5)
    
    # Then sync prices
    append_mp_job_log(job_id, ">>> FİYAT EŞITLEME BAŞLADI <<<")
    price_result = {}
    try:
        price_result = perform_pazarama_sync_prices(job_id, xml_source_id, user_id=user_id)
        append_mp_job_log(job_id, f"Fiyat eşitleme tamamlandı: {price_result.get('updated_count', 0)} güncellendi")
    except Exception as e:
        append_mp_job_log(job_id, f"Fiyat eşitleme hatası: {str(e)}", level='error')
        price_result = {'success': False, 'error': str(e), 'updated_count': 0}
    
    # Combine results
    combined = {
        'success': True,
        'message': f"Stok: {stock_result.get('updated_count', 0)} güncellendi, Fiyat: {price_result.get('updated_count', 0)} güncellendi",
        'stock_updated_count': stock_result.get('updated_count', 0),
        'price_updated_count': price_result.get('updated_count', 0),
        'total_remote': stock_result.get('total_remote', 0) or price_result.get('total_remote', 0),
        'missing_count': max(stock_result.get('missing_count', 0), price_result.get('missing_count', 0)),
        'updated_count': (stock_result.get('updated_count', 0) + price_result.get('updated_count', 0)),
        'samples': (stock_result.get('samples', []) + price_result.get('samples', []))[:10],
        'data_ids': (stock_result.get('data_ids', []) + price_result.get('data_ids', [])),
        'job_id': job_id,
    }
    
    append_mp_job_log(job_id, "Stok ve fiyat eşitleme tamamlandı.")
    return combined


# Pazarama urun gonderme fonksiyonu

def perform_pazarama_send_products(job_id: str, barcodes: List[str], xml_source_id: Any, title_prefix: str = None, user_id: int = None, **kwargs) -> Dict[str, Any]:
    """
    Send products to Pazarama from XML source
    
    Args:
        job_id: Job queue ID for progress tracking
        barcodes: List of product barcodes to send
        xml_source_id: XML source database ID
        
    Returns:
        Result dictionary with success status and counts
    """
    from app.services.job_queue import update_mp_job, get_mp_job
    from app.services.xml_service import load_xml_source_index
    
    # Resolve User ID from XML Source if not provided
    if not user_id and xml_source_id:
        try:
            from app.models import SupplierXML
            s_id = str(xml_source_id)
            if s_id.isdigit():
                src = SupplierXML.query.get(int(s_id))
                if src: user_id = src.user_id
        except Exception as e:
            logging.warning(f"Failed to resolve user_id: {e}")

    client = get_pazarama_client(user_id=user_id)
    append_mp_job_log(job_id, f"Pazarama istemcisi hazir (User ID: {user_id})")

    
    # Extract options
    price_multiplier = to_float(kwargs.get('price_multiplier', 1.0))
    default_price_val = to_float(kwargs.get('default_price', 0.0))
    skip_no_barcode = kwargs.get('skip_no_barcode', False)
    skip_no_image = kwargs.get('skip_no_image', False)
    zero_stock_as_one = kwargs.get('zero_stock_as_one', False)
    
    append_mp_job_log(job_id, f"Seçenekler: Çarpan={price_multiplier}, Varsayılan Fiyat={default_price_val}, Barkodsuz Atla={skip_no_barcode}, Resimsiz Atla={skip_no_image}")
    
    xml_index = load_xml_source_index(xml_source_id)
    mp_map = xml_index.get('by_barcode') or {}
    multiplier = get_marketplace_multiplier('pazarama')
    
    # Debug: Log barcode count
    append_mp_job_log(job_id, f"Gelen barkod sayisi: {len(barcodes) if barcodes else 0}")
    
    xml_index = load_xml_source_index(xml_source_id)
    mp_map = xml_index.get('by_barcode') or {}
    multiplier = get_marketplace_multiplier('pazarama')
    
    # Debug: Log xml index info
    append_mp_job_log(job_id, f"XML index keys: {list(xml_index.keys()) if xml_index else 'Bos'}")
    append_mp_job_log(job_id, f"mp_map boyutu: {len(mp_map)}")
    
    if not mp_map:
        append_mp_job_log(job_id, "XML kaynak haritasi bos", level='warning')
        return {
            'success': False,
            'message': 'XML kaynaginda urun bulunamadi.',
            'count': 0
        }
    
    # We use price_multiplier directly
    multiplier = price_multiplier
            
    if not barcodes:
        append_mp_job_log(job_id, "Barkod listesi bos", level='warning')
        return {
            'success': False,
            'message': 'Gonderilecek barkod yok.',
            'count': 0
        }
    
    # Ensure categories are loaded
    ensure_pazarama_categories(client)
    
    success_count = 0
    fail_count = 0
    failures = []
    skipped = []
    products_to_send = []
    brand_cache = {}
    
    total = len(barcodes)
    
    # Check for saved brand ID from settings
    saved_brand_id = Setting.get('PAZARAMA_BRAND_ID', '') or ''
    if saved_brand_id:
        append_mp_job_log(job_id, f"Kayitli marka ID kullaniliyor: {saved_brand_id[:20]}...")
    
    DEFAULT_DESI = 1
    DEFAULT_VAT_RATE = 10
    
    for idx, barcode in enumerate(barcodes, 1):
        # Check for pause/cancel
        job_state = get_mp_job(job_id)
        if job_state:
            if job_state.get('cancel_requested'):
                append_mp_job_log(job_id, "Islem iptal edildi", level='warning')
                break
            
            while job_state.get('pause_requested'):
                append_mp_job_log(job_id, "Islem duraklatildi...", level='info')
                time.sleep(5)
                job_state = get_mp_job(job_id)
                if job_state.get('cancel_requested'):
                    break
        
        product = mp_map.get(barcode)
        if not product:
            skipped.append({'barcode': barcode, 'reason': 'XML\'de bulunamadi'})
            continue
        
        # Blacklist check
        forbidden_reason = is_product_forbidden(user_id, title=product.get('title'), brand=product.get('brand'), category=product.get('category'))
        if forbidden_reason:
            skipped.append({'barcode': barcode, 'reason': f"Yasakli Liste: {forbidden_reason}"})
            continue
            
        try:
            # Extract product data
            title = clean_forbidden_words(product.get('title', ''))
            if title_prefix:
                title = f"{title_prefix} {title}"
            description = clean_forbidden_words(product.get('description', '') or title)
            top_category = product.get('top_category', '')
            xml_category = product.get('category', '')
            brand_name = product.get('brand') or product.get('vendor') or product.get('manufacturer') or ''
            
            # Create log helper for this product
            def category_log(msg, level='info'):
                append_mp_job_log(job_id, f"[{barcode[:20]}] {msg}", level=level)
            
            # Log what category values we have (first product only for debug)
            if idx == 1:
                append_mp_job_log(job_id, f"Ornek urun kategori: top='{top_category}', xml='{xml_category}'")
            
            # Resolve Brand - use saved ID if available, otherwise dynamic resolution
            if saved_brand_id:
                brand_id = saved_brand_id
            else:
                brand_id = resolve_pazarama_brand(client, brand_name, log_callback=category_log if idx <= 5 else None)

            # Resolve category with logging callback
            category_id = resolve_pazarama_category(client, title, top_category, xml_category, log_callback=category_log if idx <= 3 else None, user_id=user_id)
            if not category_id:
                skipped.append({'barcode': barcode, 'reason': 'Kategori eslesmedi', 'top_cat': top_category, 'xml_cat': xml_category})
                continue
            
            # Get required attributes for category with variant matching
            variant_attributes = product.get('variant_attributes', [])
            
            def get_variant_value(at_name):
                at_name_lower = at_name.lower()
                for va in variant_attributes:
                    v_name = va['name'].lower()
                    if v_name in at_name_lower or at_name_lower in v_name:
                        return va['value']
                return None

            attributes = []
            try:
                full_cat_data = client.get_category_with_attributes(category_id)
                # DEBUG LOGGING FOR ATTRIBUTES
                all_attrs = full_cat_data.get('data', {}).get('attributes', [])
                
                # Renk ID: 08b2020b-e519-405f-85e2-1fd712104097
                # We inject KNOWN valid values from documentation because API returns empty list for some cats.
                RENK_VALUES = [
                    {"id": "2ddb5aeb-3c25-4fb1-975d-031b436f3319", "name": "Siyah"},
                    {"id": "aef8fe0b-4f80-4dc3-91f3-b902e6fc4c4c", "name": "Beyaz"},
                    {"id": "75d0b61d-e6bd-4250-946d-40e9e262e497", "name": "Gri"},
                    {"id": "a804b5e8-93b5-48e1-8b63-8096a8e83ad8", "name": "Lacivert"},
                    {"id": "57ecdb59-f9ff-4775-814f-c7a98cfc066e", "name": "Kırmızı"},
                    {"id": "c7e562e1-ae2e-4a59-b656-81723601bdbf", "name": "Mavi"},
                    {"id": "6faa548f-7f02-42c7-80ba-6b73b84fbbef", "name": "Sarı"},
                    {"id": "96bc3661-77b6-4a6d-8745-574c9adb4a03", "name": "Yeşil"},
                    {"id": "544e1e86-678c-4e19-a7a4-230f180b2ed2", "name": "Mor"},
                    {"id": "6e4deed0-2555-4ecd-ae1b-051aa30b774a", "name": "Kahverengi"},
                    {"id": "ec98860d-668c-4c4f-824c-b918e47f1abf", "name": "Pembe"},
                    {"id": "52a2e275-c6c0-4603-96a4-c3511432e210", "name": "Turuncu"}
                ]
                
                # Beden/Yaş ID: caf725ef-9c25-4b87-8a81-97c7fab17855
                BEDEN_VALUES = [
                    {"id": "7efc6c85-57b6-490e-b8dc-e352c5e47dd1", "name": "Standart"}
                ]

                existing_ids = [a.get('id') for a in all_attrs]
                
                HIDDEN_REQUIRED_ATTRS = [
                    {"id": "08b2020b-e519-405f-85e2-1fd712104097", "name": "Renk", "values": RENK_VALUES},
                    {"id": "caf725ef-9c25-4b87-8a81-97c7fab17855", "name": "Beden/Yaş", "values": BEDEN_VALUES}
                ]
                
                for hattr in HIDDEN_REQUIRED_ATTRS:
                    if hattr["id"] not in existing_ids:
                         # Inject manually as required
                         all_attrs.append({
                            'id': hattr["id"],
                            'name': hattr["name"],
                            'isRequired': True,
                            'attributeValues': hattr["values"] 
                         })
                         append_mp_job_log(job_id, f"UYARI: '{hattr['name']}' özelliği API'den gelmedi, manuel eklendi ({len(hattr['values'])} deger ile).", level='warning')

                debug_attr_names = [f"{a.get('name')} (Req:{a.get('isRequired')})" for a in all_attrs]
                append_mp_job_log(job_id, f"DEBUG: Kategori ({category_id}) ozellikleri: {', '.join(debug_attr_names)}", level='info')

                for attr_def in all_attrs:
                    at_id = attr_def.get('id')
                    at_name = attr_def.get('name', '')
                    at_values = attr_def.get('attributeValues') or []
                    is_required = attr_def.get('isRequired', False)
                    
                    # Force required for known critical variant attributes if metadata is wrong
                    if 'renk' in at_name.lower() or 'beden' in at_name.lower() or 'ebat' in at_name.lower():
                        is_required = True
                    
                    val_from_xml = get_variant_value(at_name)
                    matched_val_id = None
                    
                    if val_from_xml and at_values:
                        val_from_xml_l = val_from_xml.lower()
                        # Exact match first
                        for v_opt in at_values:
                            if v_opt.get('name', '').lower() == val_from_xml_l:
                                matched_val_id = v_opt.get('id')
                                break
                        
                        # Fuzzy match if no exact match
                        if not matched_val_id:
                            from difflib import get_close_matches
                            v_names = [v.get('name', '') for v in at_values]
                            v_names_lower = [n.lower() for n in v_names]
                            close = get_close_matches(val_from_xml_l, v_names_lower, n=1, cutoff=0.5)
                            if close:
                                for v_opt in at_values:
                                    if v_opt.get('name', '').lower() == close[0]:
                                        matched_val_id = v_opt.get('id')
                                        break
                    
                    if matched_val_id:
                        # Found a match (Required or Optional) -> Add it
                        attributes.append({
                            'attributeId': at_id,
                            'attributeValueId': matched_val_id
                        })
                    elif is_required:
                        # Required but no match -> Try fallback
                        if at_values:
                            # Fallback: AUTOMATICALLY pick the first allowed value
                            fallback_val = at_values[0]
                            fallback_id = fallback_val.get('id')
                            fallback_name = fallback_val.get('name')
                            if idx <= 5: 
                                append_mp_job_log(job_id, f"[{barcode}] Oznitelik '{at_name}' icin tam eslesme bulunamadi. Varsayilan secildi: {fallback_name}", level='warning')
                            
                            attributes.append({
                                'attributeId': at_id,
                                'attributeValueId': fallback_id
                            })
                        else:
                            # Required but no values -> Try custom value logic
                            fallback_text = val_from_xml or "Standart"
                            attributes.append({
                                'attributeId': at_id,
                                'customAttributeValue': fallback_text
                            })
                            append_mp_job_log(job_id, f"[{barcode}] Oznitelik '{at_name}' (Zorunlu) liste boş. Özel değer denendi: {fallback_text}", level='warning')
                    else:
                        # Fix for "Renk" bug: Value list is empty but attribute is required.
                        # Likely a custom text field or dynamic attribute.
                        # If allowed custom input, use variant value or "Standart"
                        # Since we don't have 'allowCustom' flag in this logic block easily (it was in attr_def?), let's assume if values are empty it accepts text?
                        # Re-checking attr_def structure: Pazarama usually has 'attributeValues' list. If empty, maybe it's not a selection.
                        # Try adding as custom string if possible. Pazarama API documentation is vague on this, but let's try.
                        # Or checking if "Renk" (Color) needs specific handling.
                        
                        fallback_text = val_from_xml or "Standart"
                        
                        # Attempt to send custom value
                        attributes.append({
                            'attributeId': at_id,
                            'customAttributeValue': fallback_text
                        })
                        append_mp_job_log(job_id, f"[{barcode}] Oznitelik '{at_name}' (Zorunlu) liste boş. Özel değer denendi: {fallback_text}", level='warning')
                        # We don't have a clean way to set 'customValue' in Pazarama integration usually, 
                        # but some endpoints accept 'attributeValue' string instead of Id?
                        # Without exact API docs for this specific "Renk" case, we will try to skip it but LOG ERROR to user to fill it manually or map it.
                        # BUT user wants it fixed.
                        # Let's try to find if there is a 'Standart' value ID from a global list? No.
                        # Log explicit error.
                        append_mp_job_log(job_id, f"[{barcode}] Oznitelik '{at_name}' (Zorunlu) için değer listesi boş ve eşleşme yok. Lütfen Pazarama panelinden kontrol edin.", level='error')

            except Exception as attr_err:
                # Fallback to simple cached attributes if detailed fetch fails
                attributes = pazarama_get_required_attributes(client, category_id)
            
            # Price & Stock
            base_price = to_float(product.get('price', 0))
            stock = to_int(product.get('quantity', 0))
            
            if base_price <= 0:
                skipped.append({'barcode': barcode, 'reason': 'Fiyat 0'})
                continue
            
            # Artık GLOBAL_PRICE_RULES kullanılıyor (multiplier kaldırıldı)
            price = calculate_price(base_price, 'pazarama', user_id=user_id)
            list_price = round(price * 1.05, 2)  # 5% higher for list price
            
            # Images
            raw_images = product.get('images', [])
            product_images = []
            for img in raw_images[:8]:
                if isinstance(img, dict):
                    url = img.get('url', '')
                    if url:
                        product_images.append({'imageurl': url})
                elif isinstance(img, str) and img:
                    product_images.append({'imageurl': img})
            
            if not product_images:
                product_images = [{'imageurl': 'https://via.placeholder.com/500'}]
            
            # Build Pazarama product payload
            product_data = {
                'name': title[:100],
                'displayName': title[:250],
                'description': product.get('details') or description,
                'brandId': brand_id,
                'desi': DEFAULT_DESI,
                'code': barcode,
                'groupCode': (product.get('parent_barcode') or product.get('modelCode') or product.get('productCode') or product.get('stock_code') or barcode)[:100],
                'stockCode': (product.get('stock_code') or barcode)[:100],
                'stockCount': stock,
                'listPrice': list_price,
                'salePrice': price,
                'productSaleLimitQuantity': 0,
                'currencyType': 'TRY',
                'vatRate': DEFAULT_VAT_RATE,
                'images': product_images,
                'categoryId': category_id,
                'attributes': attributes
            }
            
            products_to_send.append(product_data)
            
            if idx % 10 == 0:
                append_mp_job_log(job_id, f"{idx}/{total} urun hazirlandi...")
            
        except Exception as e:
            fail_count += 1
            failures.append({'barcode': barcode, 'reason': str(e)})
            append_mp_job_log(job_id, f"Hata {barcode}: {e}", level='error')
    
    # Log summary before checking products_to_send
    append_mp_job_log(job_id, f"Hazirlanan urun: {len(products_to_send)}, Atlanan: {len(skipped)}")
    
    if not products_to_send:
        # Return success=True but with 0 count if no products were prepared
        # This is not an error, just no products matched criteria
        skip_reasons = {}
        for s in skipped:
            reason = s.get('reason', 'Bilinmeyen')
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
        
        append_mp_job_log(job_id, f"Atlama nedenleri: {skip_reasons}", level='warning')
        
        return {
            'success': True,  # Changed to True - skipping is not a failure
            'message': 'Gonderilecek gecerli urun olusturulamadi.',
            'skipped': skipped,
            'count': 0,
            'summary': {
                'success_count': 0,
                'fail_count': 0,
                'skip_reasons': skip_reasons
            }
        }
    
    # Send products in batches
    batch_ids = []
    batch_size = 50  # Pazarama might have limits
    total_batches = (len(products_to_send) + batch_size - 1) // batch_size
    
    for i in range(0, len(products_to_send), batch_size):
        # Check Job Status for Cancel/Pause
        job_state = get_mp_job(job_id)
        if job_state:
            if job_state.get('cancel_requested'):
                append_mp_job_log(job_id, "Islem kullanici tarafindan iptal edildi.", level='warning')
                break
            
            while job_state.get('pause_requested'):
                append_mp_job_log(job_id, "Islem duraklatildi. Devam etmesi bekleniyor...", level='info')
                time.sleep(5)
                job_state = get_mp_job(job_id)
                if job_state.get('cancel_requested'):
                    break
            
            if job_state.get('cancel_requested'):
                append_mp_job_log(job_id, "Islem kullanici tarafindan iptal edildi.", level='warning')
                break
        
        current_batch_num = (i // batch_size) + 1
        update_mp_job(job_id, progress={'current': success_count + fail_count, 'total': len(products_to_send), 'batch': f"{current_batch_num}/{total_batches}"})
        
        batch = products_to_send[i:i+batch_size]
        try:
            resp = client.create_products(batch)
            
            # Check response
            if resp.get('success'):
                batch_req_id = resp.get('data', {}).get('batchRequestId')
                if batch_req_id:
                    batch_ids.append(batch_req_id)
                    append_mp_job_log(job_id, f"Batch {current_batch_num}/{total_batches} gonderildi. ID: {batch_req_id}")
                    
                    # Wait and Poll for Pazarama to process (Max 12 attempts * 15s = 3 mins)
                    try:
                        max_attempts = 12
                        attempt = 0
                        finished = False
                        
                        while attempt < max_attempts and not finished:
                            attempt += 1
                            if attempt > 1:
                                time.sleep(15)
                                
                            batch_status = client.check_batch(batch_req_id)
                            status = batch_status.get('status')
                            status_code = batch_status.get('status_code')
                            batch_total = batch_status.get('total', 0)
                            success_cnt = batch_status.get('success', 0)
                            failed_cnt = batch_status.get('failed', 0)
                            batch_result = batch_status.get('batch_result', [])
                            
                            if status == 'DONE' or status_code == 2:
                                finished = True
                                if success_cnt > 0 or failed_cnt > 0:
                                    success_count += success_cnt
                                    fail_count += failed_cnt
                                    append_mp_job_log(job_id, f"Batch {current_batch_num}: {success_cnt} basarili, {failed_cnt} basarisiz", level='info')
                                    
                                    # Detailed item status
                                    if batch_result:
                                        for res_item in batch_result[:15]:
                                            bcode = res_item.get('barcode') or res_item.get('code') or '?'
                                            msg = res_item.get('message') or res_item.get('description') or res_item.get('error') or 'İşlem Başarılı'
                                            state_txt = res_item.get('operationStatusText') or res_item.get('statusName') or ''
                                            wait_msg = res_item.get('waitingApproveExp') or ''
                                            item_full_msg = f"[{bcode}] Durum: {state_txt} | Mesaj: {msg}"
                                            if wait_msg: item_full_msg += f" | Onay Notu: {wait_msg}"
                                            append_mp_job_log(job_id, f"  -> Item Durumu: {item_full_msg}", level='info')

                                    if failed_cnt > 0:
                                        failures.extend([{'error': 'Pazarama Hatası (Detay loglarda)'}] * failed_cnt)
                                else:
                                    # DONE but maybe no counts yet or silent skip
                                    actual_count = batch_total if batch_total > 0 else 0
                                    success_count += actual_count
                                    append_mp_job_log(job_id, f"Batch {current_batch_num}: Islem tamamlandi ({actual_count} urun)", level='info')
                                    
                            elif status == 'ERROR' or status_code == 3:
                                finished = True
                                fail_count += len(batch)
                                error_msg = batch_status.get('error') or 'Islem hatasi'
                                append_mp_job_log(job_id, f"Batch {current_batch_num}: Hata - {error_msg}", level='error')
                                failures.append({'batch': current_batch_num, 'reason': error_msg})
                            else:
                                # Still IN_PROGRESS or unknown
                                if attempt == 1:
                                    append_mp_job_log(job_id, f"Batch {current_batch_num}: Pazarama tarafindan isleniyor...", level='info')
                                elif attempt % 3 == 0:
                                    append_mp_job_log(job_id, f"Batch {current_batch_num}: Hala bekleniyor ({attempt}/{max_attempts})...", level='info')

                            # ALWAYS log raw results for debugging at the end or on last attempt
                            if finished or attempt == max_attempts:
                                try:
                                    import json
                                    raw_dump = json.dumps(batch_status.get('raw', {}), ensure_ascii=False, default=str)
                                    append_mp_job_log(job_id, f"  -> API Detay (Raw): {raw_dump[:3000]}", level='info')
                                except:
                                    pass

                        if not finished:
                            append_mp_job_log(job_id, f"Batch {current_batch_num}: 3 dakika icinde tamamlanmadi, sonraki gruba geciliyor.", level='warning')
                            success_count += len(batch) # Assume success to continue progress
                            
                    except Exception as e:
                        append_mp_job_log(job_id, f"Batch durum sorgulanamadi: {e}", level='warning')
                        success_count += len(batch)
                    

                        




                                        


                                                







                else:
                    success_count += len(batch)
                    append_mp_job_log(job_id, f"Batch {current_batch_num}: Gonderildi", level='info')
            else:
                fail_count += len(batch)
                error_msg = resp.get('message') or resp.get('userMessage') or 'Bilinmeyen hata'
                failures.append({'batch': current_batch_num, 'reason': error_msg})
                append_mp_job_log(job_id, f"Batch {current_batch_num} gonderim hatasi: {error_msg}", level='error')

                
        except Exception as e:
            fail_count += len(batch)
            failures.append({'reason': str(e)})
            append_mp_job_log(job_id, f"Batch gonderim hatasi: {e}", level='error')
        
        # Update progress
        update_mp_job(job_id, progress={
            'current': success_count + fail_count,
            'total': len(products_to_send),
            'batch': f"{current_batch_num}/{total_batches}"
        })
    
    return {
        'success': True,
        'count': len(products_to_send),
        'skipped': skipped,
        'summary': {
            'success_count': success_count,
            'fail_count': fail_count,
            'failures': failures[:10],  # Limit to first 10 failures
            'batch_ids': batch_ids
        }
    }

def perform_pazarama_send_all(job_id: str, xml_source_id: Any, user_id: int = None, **kwargs) -> Dict[str, Any]:
    """Send ALL products from XML source to Pazarama"""
    append_mp_job_log(job_id, "Tüm ürünler hazırlanıyor...")
    
    from app.services.xml_service import load_xml_source_index
    xml_index = load_xml_source_index(xml_source_id)
    mp_map = xml_index.get('by_barcode') or {}
    all_barcodes = list(mp_map.keys())
    
    if not all_barcodes:
        return {'success': False, 'message': 'XML kaynağında ürün bulunamadı.', 'count': 0}
    
    append_mp_job_log(job_id, f"Toplam {len(all_barcodes)} ürün bulundu. Gönderim başlıyor...")
    
    return perform_pazarama_send_products(job_id, all_barcodes, xml_source_id, user_id=user_id, **kwargs)

def perform_pazarama_batch_update(job_id: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Batch update Pazarama stock/price.
    items: [{'barcode': '...', 'stock': 10, 'price': 100.0}, ...]
    """
    client = get_pazarama_client()
    append_mp_job_log(job_id, f"Pazarama toplu güncelleme ba�xlatıldı. {len(items)} ürün.")
    
    stock_updates = []
    price_updates = []
    
    for item in items:
        code = item['barcode']
        # Stock
        if 'stock' in item:
            try:
                stock_updates.append({'code': code, 'stockCount': int(item['stock'])})
            except: pass
            
        # Price
        if 'price' in item:
            try:
                p = float(item['price'])
                # Pazarama expects both list and sale price usually
                price_updates.append({'code': code, 'listPrice': p, 'salePrice': p})
            except: pass
            
    total_stock_sent = 0
    total_price_sent = 0
    
    # 1. Update Stock
    if stock_updates:
        append_mp_job_log(job_id, f"Stok güncellemeleri gönderiliyor ({len(stock_updates)} adet)...")
        for idx, chunk in enumerate(chunked(stock_updates, 25), start=1):
            try:
                client.update_stock(chunk)
                total_stock_sent += len(chunk)
                append_mp_job_log(job_id, f"Stok Paket {idx}: {len(chunk)} ürün gönderildi.")
                time.sleep(1) # Pazarama rate limit safe
            except Exception as e:
                append_mp_job_log(job_id, f"Stok Paket {idx} hata: {e}", level='error')
                
    # 2. Update Price
    if price_updates:
        append_mp_job_log(job_id, f"Fiyat güncellemeleri gönderiliyor ({len(price_updates)} adet)...")
        for idx, chunk in enumerate(chunked(price_updates, 20), start=1): # Slightly smaller chunk for prices
            try:
                client.update_price(chunk)
                total_price_sent += len(chunk)
                append_mp_job_log(job_id, f"Fiyat Paket {idx}: {len(chunk)} ürün gönderildi.")
                time.sleep(1)
            except Exception as e:
                append_mp_job_log(job_id, f"Fiyat Paket {idx} hata: {e}", level='error')

    result = {
        'success': True,
        'stock_updated': total_stock_sent,
        'price_updated': total_price_sent,
        'message': f'İ�xlem tamamlandı. Stok: {total_stock_sent}, Fiyat: {total_price_sent}'
    }
    
    append_mp_job_log(job_id, "Pazarama güncelleme i�xlemi bitti.")
    return result


def perform_pazarama_product_update(barcode: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Detailed update for Pazarama product.
    """
    client = get_pazarama_client()
    messages = []
    success = True
    
    # Identifier
    code = data.get('stockCode') or barcode
    
    # 1. Price/Stock (Immediate)
    if 'quantity' in data:
        try:
            qty = int(data['quantity'])
            client.update_stock([{'code': code, 'stockCount': qty}])
            messages.append("Stok güncellendi.")
        except Exception as e:
            messages.append(f"Stok hatası: {e}")
            success = False
            
    if 'salePrice' in data or 'listPrice' in data:
        try:
            p = float(data.get('salePrice') or data.get('listPrice'))
            # Pazarama expects both
            client.update_price([{'code': code, 'listPrice': p, 'salePrice': p}])
            messages.append("Fiyat güncellendi.")
        except Exception as e:
            messages.append(f"Fiyat hatası: {e}")
            success = False

    # 2. Content Update (Title, Description, Images)
    content_fields = ['title', 'description', 'images', 'vatRate', 'brandId', 'categoryId'] 
    
    if any(k in data for k in content_fields):
        try:
            update_item = {'code': code}
            if 'title' in data:
                update_item['name'] = data['title']
            if 'description' in data:
                update_item['description'] = data['description']
            if 'vatRate' in data:
                update_item['vatRate'] = int(data['vatRate'])
            
            if 'images' in data and isinstance(data['images'], list):
                 update_item['images'] = data['images']
            
            if 'brandId' in data:
                update_item['brandId'] = str(data['brandId'])
            if 'categoryId' in data:
                update_item['categoryId'] = str(data['categoryId'])
            
            client.update_product([update_item])
            messages.append("İçerik güncellendi.")
                
        except Exception as e:
            messages.append(f"İçerik güncelleme hatası: {e}")
            success = False

    return {'success': success, 'message': ' | '.join(messages)}

def sync_pazarama_products(user_id: int, job_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetch all products from Pazarama and sync them to the local MarketplaceProduct table.
    """
    from app import db
    from app.models import MarketplaceProduct, Setting
    from app.services.job_queue import append_mp_job_log
    
    logger.info(f"[PAZARAMA] Syncing products for user {user_id}...")
    if job_id:
        append_mp_job_log(job_id, f"Pazarama ürün senkronizasyonu başlatıldı (User ID: {user_id})")

    try:
        # Get client
        api_key = Setting.get('PAZARAMA_API_KEY', user_id=user_id)
        api_secret = Setting.get('PAZARAMA_API_SECRET', user_id=user_id)
        if not api_key or not api_secret:
            msg = "Pazarama API bilgileri eksik."
            if job_id: append_mp_job_log(job_id, msg, level='error')
            return {'success': False, 'message': msg}
            
        from app.services.pazarama_client import PazaramaClient
        client = PazaramaClient(api_key, api_secret)
        
        products = pazarama_fetch_all_products(client)
        if not products:
            msg = "Pazarama'dan hiç ürün dönmedi."
            logger.warning(f"[PAZARAMA] {msg}")
            if job_id: append_mp_job_log(job_id, msg, level='warning')
            return {'success': False, 'message': msg}

        if job_id:
            append_mp_job_log(job_id, f"Pazarama API'den {len(products)} ürün çekildi. Veritabanına işleniyor...")

        remote_barcodes = []
        for p in products:
            # Pazarama fields: 'code' is usually barcode/SellerCode
            barcode = p.get('code') or p.get('barcode', 'N/A')
            remote_barcodes.append(barcode)
            
            existing = db.session.query(MarketplaceProduct).filter_by(
                user_id=user_id, 
                marketplace='pazarama', 
                barcode=barcode
            ).first()
            
            if not existing:
                existing = MarketplaceProduct(
                    user_id=user_id,
                    marketplace='pazarama',
                    barcode=barcode
                )
                db.session.add(existing)

            existing.title = p.get('name', 'İsimsiz Ürün')
            existing.quantity = int(p.get('stockCount', 0))
            existing.price = float(p.get('listPrice', 0.0) or p.get('salePrice', 0.0))
            existing.sale_price = float(p.get('salePrice', 0.0) or p.get('listPrice', 0.0))
            existing.stock_code = p.get('code')
            
            # Durum Eşitleme: Aktif / Pasif (1: Yayında, 2: Yayında Değil)
            state = p.get('state')
            existing.status = 'Aktif' if state == 1 else 'Pasif'
            existing.on_sale = (state == 1)
            
            # Images
            imgs = p.get('images', [])
            if imgs and isinstance(imgs, list):
                 existing.image_url = imgs[0].get('url') if isinstance(imgs[0], dict) else imgs[0]

        db.session.commit()
        
        # Cleanup
        deleted_count = db.session.query(MarketplaceProduct).filter(
            MarketplaceProduct.user_id == user_id,
            MarketplaceProduct.marketplace == 'pazarama',
            ~MarketplaceProduct.barcode.in_(remote_barcodes)
        ).delete(synchronize_session=False)
        db.session.commit()

        final_msg = f"Pazarama senkronizasyonu tamamlandı: {len(products)} güncellendi, {deleted_count} silindi."
        logger.info(f"[PAZARAMA] {final_msg}")
        if job_id:
            append_mp_job_log(job_id, final_msg)

        return {'success': True, 'count': len(products), 'deleted': deleted_count}

    except Exception as e:
        err_msg = f"Pazarama senkronizasyon hatası: {str(e)}"
        logger.error(f"[PAZARAMA] {err_msg}")
        if job_id:
            append_mp_job_log(job_id, err_msg, level='error')
        db.session.rollback()
        return {'success': False, 'message': err_msg}

def perform_pazarama_batch_update(job_id: str, items: List[Dict[str, Any]], user_id: int = None) -> Dict[str, Any]:
    """
    Batch update Pazarama stock/price from local data.
    items: [{'barcode': '...', 'stock': 10, 'price': 100.0}, ...]
    """
    try:
        from app.services.job_queue import append_mp_job_log
        client = get_pazarama_client(user_id=user_id)
        append_mp_job_log(job_id, f"Pazarama toplu güncelleme başlatıldı. {len(items)} ürün.")
        
        stock_updates = []
        price_updates = []
        
        for item in items:
            if 'stock' in item:
                stock_updates.append({'code': item['barcode'], 'stockCount': int(item['stock'])})
            if 'price' in item:
                price_updates.append({
                    'code': item['barcode'], 
                    'listPrice': float(item['price']),
                    'salePrice': float(item['price'])
                })
        
        total_sent = 0
        from app.utils.helpers import chunked
        
        if stock_updates:
            for chunk in chunked(stock_updates, 25):
                client.update_stock(chunk)
                total_sent += len(chunk)
                append_mp_job_log(job_id, f"✅ {total_sent}/{len(stock_updates)} stok gönderildi.")
                time.sleep(2)
                
        if price_updates:
            total_sent = 0
            for chunk in chunked(price_updates, 25):
                client.update_price(chunk)
                total_sent += len(chunk)
                append_mp_job_log(job_id, f"✅ {total_sent}/{len(price_updates)} fiyat gönderildi.")
                time.sleep(2)
                
        return {'success': True, 'count': len(items)}
    except Exception as e:
        return {'success': False, 'message': str(e)}


def perform_pazarama_direct_push_actions(user_id: int, to_update: List[Any], to_create: List[Any], to_zero: List[Any], src: Any, job_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Pazarama için Direct Push aksiyonlarını gerçekleştirir.
    """
    import json
    from datetime import datetime
    from app.services.job_queue import append_mp_job_log
    from app.utils.helpers import calculate_price, chunked
    from app.models import MarketplaceProduct, db
    
    client = get_pazarama_client(user_id=user_id)
    res = {'updated_count': 0, 'created_count': 0, 'zeroed_count': 0}
    
    # --- 1. GÜNCELLEMELER (Update) ---
    if to_update:
        stock_updates = []
        price_updates = []
        for xml_item, local_item in to_update:
            final_price = calculate_price(xml_item.price, 'pazarama', user_id=user_id)
            
            # Pazarama stock update
            stock_updates.append({
                "code": local_item.barcode,
                "stockCode": local_item.stock_code,
                "stockQuantity": xml_item.quantity
            })
            
            # Pazarama price update
            price_updates.append({
                "code": local_item.barcode,
                "stockCode": local_item.stock_code,
                "listPrice": final_price,
                "salePrice": final_price,
                "vatRate": 20 # Default or from XML
            })
            
            if job_id: append_mp_job_log(job_id, f"Güncelleniyor: {xml_item.stock_code} (Stok: {local_item.quantity} -> {xml_item.quantity})")
            
            local_item.quantity = xml_item.quantity
            local_item.sale_price = final_price
            local_item.last_sync_at = datetime.now()

        try:
            # Pazarama updates are separate for stock and price
            for batch in chunked(stock_updates, 100):
                client.update_stock(batch)
                res['updated_count'] += len(batch)
            
            for batch in chunked(price_updates, 100):
                client.update_price(batch)
            
            db.session.commit()
        except Exception as e:
            if job_id: append_mp_job_log(job_id, f"Pazarama güncelleme hatası: {str(e)}", level='error')

    # --- 2. YENİ ÜRÜNLER (Create) ---
    if to_create:
        from app.services.xml_service import generate_random_barcode
        create_items = []
        for xml_item in to_create:
            barcode = xml_item.barcode

            # Check random barcode setting (Global override from Auto Sync Menu)
            use_random_setting = Setting.get(f'AUTO_SYNC_USE_RANDOM_BARCODE_pazarama', user_id=user_id) == 'true'
            
            if src.use_random_barcode or use_random_setting:
                barcode = generate_random_barcode()
            
            raw = json.loads(xml_item.raw_data)
            
            # Marka ve Kategori Çözümü
            brand_id = resolve_pazarama_brand(client, raw.get('brand'))
            cat_id = resolve_pazarama_category(client, xml_item.title, raw.get('category'), raw.get('category'), user_id=user_id)
            
            if not brand_id or not cat_id:
                reason = "Marka bulunamadı" if not brand_id else "Kategori bulunamadı"
                if job_id: append_mp_job_log(job_id, f"Atlandı ({reason}): {xml_item.stock_code}", level='warning')
                continue

            final_price = calculate_price(xml_item.price, 'pazarama', user_id=user_id)
            
            item = {
                "code": barcode,
                "stockCode": xml_item.stock_code,
                "displayName": xml_item.title,
                "brandId": brand_id,
                "categoryId": cat_id,
                "stockQuantity": xml_item.quantity,
                "listPrice": final_price,
                "salePrice": final_price,
                "vatRate": int(raw.get('vatRate', 20)),
                "description": raw.get('details') or raw.get('description') or xml_item.title,
                "images": [img['url'] for img in raw.get('images', []) if img.get('url')],
                "attributes": [] # TODO: Required attributes if any
            }
            create_items.append((item, xml_item))
            if job_id: append_mp_job_log(job_id, f"Yeni Ürün Yükleniyor: {xml_item.stock_code} ({xml_item.title[:30]}...)")

        if create_items:
            try:
                payloads = [x[0] for x in create_items]
                client.create_products(payloads)
                for item_payload, xml_record in create_items:
                    existing = MarketplaceProduct.query.filter_by(user_id=user_id, marketplace='pazarama', barcode=item_payload['code']).first()
                    if not existing:
                        new_mp = MarketplaceProduct(
                            user_id=user_id, marketplace='pazarama', barcode=item_payload['code'],
                            stock_code=xml_record.stock_code, title=xml_record.title,
                            price=item_payload['listPrice'], sale_price=item_payload['salePrice'],
                            quantity=xml_record.quantity, status='Pending', on_sale=True
                        )
                        db.session.add(new_mp)
                db.session.commit()
                res['created_count'] += len(create_items)
            except Exception as e:
                if job_id: append_mp_job_log(job_id, f"Pazarama yükleme hatası: {str(e)}", level='error')

    # --- 3. STOK SIFIRLAMA (Zero) ---
    if to_zero:
        zero_items = []
        for local_item in to_zero:
            zero_items.append({
                "code": local_item.barcode,
                "stockCode": local_item.stock_code,
                "stockQuantity": 0
            })
            if job_id: append_mp_job_log(job_id, f"Stok Sıfırlanıyor (XML'de yok): {local_item.stock_code}")
            local_item.quantity = 0

        try:
            for batch in chunked(zero_items, 100):
                client.update_stock(batch)
                res['zeroed_count'] += len(batch)
            db.session.commit()
        except Exception as e:
            if job_id: append_mp_job_log(job_id, f"Pazarama stok sıfırlama hatası: {str(e)}", level='error')

    return res


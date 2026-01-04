import time
from datetime import datetime
import json
import copy
import requests
import logging
from typing import List, Dict, Any, Optional
from difflib import get_close_matches
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from app.models import Setting, Product, SupplierXML
from app.services.trendyol_client import TrendyolClient, build_attributes_payload
from app.services.xml_service import load_xml_source_index
from app.services.job_queue import append_mp_job_log, get_mp_job, update_mp_job
from app.utils.helpers import to_int, to_float, chunked, get_marketplace_multiplier, clean_forbidden_words, calculate_price, is_product_forbidden

_CAT_TFIDF = {
    "leaf": [],
    "names": [],
    "vectorizer": None,
    "matrix": None,
}

_BRAND_TFIDF = {
    "leaf": [],  # Store dicts {id, name}
    "names": [], # Store names
    "vectorizer": None,
    "matrix": None,
}

# Brand cache - stores Trendyol brand id-name mappings
_BRAND_CACHE = {
    "by_name": {},  # name.lower() -> {id, name}
    "count": 0,
    "loaded": False
}

# Category cache - stores Trendyol category tree
_CATEGORY_CACHE = {
    "by_name": {},  # name.lower() -> {id, name, path}
    "by_id": {},    # id -> {name, path}
    "list": [],     # flat list of all categories
    "count": 0,
    "loaded": False
}

def load_brand_cache_from_db() -> bool:
    """Load brand cache from database (Setting)."""
    try:
        cached_json = Setting.get("TRENDYOL_BRAND_CACHE", "")
        if cached_json:
            logging.info(f"Loading brand cache from DB, data size: {len(cached_json)} chars")
            data = json.loads(cached_json)
            _BRAND_CACHE["by_name"] = {k.lower(): v for k, v in data.get("by_name", {}).items()}
            _BRAND_CACHE["count"] = data.get("count", 0)
            _BRAND_CACHE["loaded"] = True
            logging.info(f"Brand cache loaded: {_BRAND_CACHE['count']} brands")
            return True
        else:
            logging.warning("Brand cache is empty in database. Run 'Markaları Çek' first.")
    except Exception as e:
        logging.exception(f"Failed to load brand cache: {e}")
    return False

def save_brand_cache_to_db() -> bool:
    """Save brand cache to database."""
    try:
        data = {
            "by_name": _BRAND_CACHE["by_name"],
            "count": _BRAND_CACHE["count"]
        }
        json_data = json.dumps(data, ensure_ascii=False)
        logging.info(f"Saving brand cache to DB: {_BRAND_CACHE['count']} brands, {len(json_data)} chars")
        Setting.set("TRENDYOL_BRAND_CACHE", json_data)
        logging.info("Brand cache saved successfully")
        return True
    except Exception as e:
        logging.exception(f"Failed to save brand cache: {e}")
        return False

def fetch_and_cache_brands(user_id: int = None) -> Dict[str, Any]:
    """Fetch all brands from Trendyol API and cache them."""
    try:
        from flask_login import current_user
        if user_id is None:
            user_id = current_user.id if current_user and current_user.is_authenticated else None
            
        client = get_trendyol_client(user_id=user_id)
        result = {"success": False, "count": 0, "message": ""}
        
        page = 0
        all_brands = []
        max_pages = 100  # Safety limit
        
        while page < max_pages:
            resp = client.get_all_brands(page=page, size=1500)
            
            # Debug: Log response structure for first page
            if page == 0:
                logging.info(f"Brand API response type: {type(resp)}")
                if isinstance(resp, dict):
                    logging.info(f"Brand API response keys: {list(resp.keys())}")
                    # Try to find brands in response
                    for key in resp.keys():
                        if isinstance(resp[key], list) and len(resp[key]) > 0:
                            logging.info(f"Key '{key}' contains list with {len(resp[key])} items")
                            if len(resp[key]) > 0:
                                logging.info(f"First item sample: {resp[key][0]}")
            
            # Handle different response formats
            brands = []
            if isinstance(resp, list):
                brands = resp
            elif isinstance(resp, dict):
                # Try common keys
                for key in ['brands', 'items', 'data', 'content']:
                    if key in resp and isinstance(resp[key], list):
                        brands = resp[key]
                        break
            
            logging.info(f"Brand fetch page {page}: got {len(brands)} brands")
            
            if not brands:
                break
                
            all_brands.extend(brands)
            
            if len(brands) < 1500:
                break
            page += 1
        
        logging.info(f"Total brands fetched: {len(all_brands)}")
        
        # Build cache
        _BRAND_CACHE["by_name"] = {}
        for b in all_brands:
            name = b.get("name", "").strip()
            brand_id = b.get("id")
            if name and brand_id:
                _BRAND_CACHE["by_name"][name.lower()] = {"id": brand_id, "name": name}
        
        _BRAND_CACHE["count"] = len(_BRAND_CACHE["by_name"])
        _BRAND_CACHE["loaded"] = True
        
        # Save to database
        saved = save_brand_cache_to_db()
        
        result["success"] = True
        result["count"] = _BRAND_CACHE["count"]
        result["message"] = f"{_BRAND_CACHE['count']} marka başarıyla çekildi ve kaydedildi." if saved else f"{_BRAND_CACHE['count']} marka çekildi ama kaydetme başarısız!"
        
        return result
        
    except Exception as e:
        logging.exception("Failed to fetch brands")
        return {"success": False, "count": 0, "message": str(e)}


def save_category_cache_to_db() -> bool:
    """Save category cache to database."""
    try:
        cache_data = {
            "by_name": _CATEGORY_CACHE.get("by_name", {}),
            "by_id": _CATEGORY_CACHE.get("by_id", {}),
            "list": _CATEGORY_CACHE.get("list", []),
            "count": _CATEGORY_CACHE.get("count", 0)
        }
        Setting.set('TRENDYOL_CATEGORY_CACHE', json.dumps(cache_data))
        logging.info(f"Saved category cache to DB: {cache_data['count']} categories")
        return True
    except Exception as e:
        logging.exception(f"Failed to save category cache: {e}")
        return False


def load_category_cache_from_db() -> bool:
    """Load category cache from database."""
    try:
        cache_json = Setting.get('TRENDYOL_CATEGORY_CACHE', '')
        if cache_json:
            cache_data = json.loads(cache_json)
            _CATEGORY_CACHE.update(cache_data)
            _CATEGORY_CACHE["loaded"] = True
            logging.info(f"Loaded category cache from DB: {cache_data.get('count', 0)} categories")
            return True
    except Exception as e:
        logging.exception(f"Failed to load category cache: {e}")
    return False


def fetch_and_cache_categories(user_id: int = None) -> Dict[str, Any]:
    """Fetch all categories from Trendyol API and cache them."""
    try:
        from flask_login import current_user
        if user_id is None:
            user_id = current_user.id if current_user and current_user.is_authenticated else None
            
        client = get_trendyol_client(user_id=user_id)
        result = {"success": False, "count": 0, "message": ""}
        
        # Get category tree
        logging.info("Fetching Trendyol category tree...")
        tree = client.get_category_tree()
        
        categories = tree.get("categories", [])
        logging.info(f"Got {len(categories)} root categories")
        
        # Flatten the tree
        flat_categories = []
        
        def flatten_tree(cats, path=""):
            for cat in cats:
                cat_id = cat.get("id")
                cat_name = cat.get("name", "")
                full_path = f"{path} > {cat_name}".strip(" > ") if path else cat_name
                
                subs = cat.get("subCategories", [])
                
                # Only add LEAF categories (no children) - Trendyol requires leaf categories
                if not subs:
                    flat_categories.append({
                        "id": cat_id,
                        "name": cat_name,
                        "path": full_path
                    })
                else:
                    # Recurse into subcategories
                    flatten_tree(subs, full_path)
        
        flatten_tree(categories)
        
        logging.info(f"Flattened to {len(flat_categories)} total categories")
        
        # Build cache
        _CATEGORY_CACHE["by_name"] = {}
        _CATEGORY_CACHE["by_id"] = {}
        _CATEGORY_CACHE["list"] = flat_categories
        
        for cat in flat_categories:
            name_key = cat["name"].lower().strip()
            path_key = cat["path"].lower().strip()
            
            # Store by name and by full path
            _CATEGORY_CACHE["by_name"][name_key] = {"id": cat["id"], "name": cat["name"], "path": cat["path"]}
            _CATEGORY_CACHE["by_name"][path_key] = {"id": cat["id"], "name": cat["name"], "path": cat["path"]}
            _CATEGORY_CACHE["by_id"][cat["id"]] = {"name": cat["name"], "path": cat["path"]}
        
        _CATEGORY_CACHE["count"] = len(flat_categories)
        _CATEGORY_CACHE["loaded"] = True
        
        # Save to database
        saved = save_category_cache_to_db()
        
        result["success"] = True
        result["count"] = _CATEGORY_CACHE["count"]
        result["message"] = f"{_CATEGORY_CACHE['count']} kategori başarıyla çekildi ve kaydedildi." if saved else f"{_CATEGORY_CACHE['count']} kategori çekildi ama kaydetme başarısız!"
        
        return result
        
    except Exception as e:
        logging.exception("Failed to fetch categories")
        return {"success": False, "count": 0, "message": str(e)}


def get_cached_category_id(category_name: str, default_id: int = 0) -> int:
    """Get category ID from cache. Returns default if not found."""
    if not _CATEGORY_CACHE.get("loaded"):
        load_category_cache_from_db()
    
    if not category_name:
        return default_id
    
    # Try exact match first
    key = category_name.lower().strip()
    cached = _CATEGORY_CACHE["by_name"].get(key)
    if cached:
        return cached["id"]
    
    # Try partial match
    for cache_key, cache_val in _CATEGORY_CACHE["by_name"].items():
        if key in cache_key or cache_key in key:
            return cache_val["id"]
    
    return default_id


def get_category_cache_stats() -> Dict[str, Any]:
    """Get category cache statistics."""
    if not _CATEGORY_CACHE.get("loaded"):
        load_category_cache_from_db()
    return {
        "loaded": _CATEGORY_CACHE.get("loaded", False),
        "count": _CATEGORY_CACHE.get("count", 0)
    }


def normalize_brand_name(name: str) -> str:
    """
    Normalize brand name for cache key (Legacy Style).
    1. Turkish chars -> English
    2. Lowercase
    3. Remove punctuation but KEEP SPACES
    Result: "Mavi Jeans" -> "mavi jeans"
    """
    if not name:
        return ""
    
    # 1. Turkish Map
    table = str.maketrans({
        "ğ": "g", "Ğ": "g",
        "ü": "u", "Ü": "u",
        "ş": "s", "Ş": "s",
        "ı": "i", "İ": "i",
        "ö": "o", "Ö": "o",
        "ç": "c", "Ç": "c",
        "I": "i" 
    })
    s = name.translate(table)
    
    # 2. Lowercase and Strip
    s = s.lower().strip()
    
    # 3. Remove punctuation but keep spaces
    import re
    s = re.sub(r'[^\w\s]', '', s) # Keep word chars and spaces
    s = re.sub(r'\s+', ' ', s)    # Collapse multiple spaces
    
    return s.strip()


# Cache for normalized brand names to speed up fuzzy matching
_NORMALIZED_BRAND_CACHE = {
    "list": [],
    "map": {}, # normalized -> original_key
    "loaded": False
}

def _refresh_normalized_cache():
    if not _BRAND_CACHE.get("loaded"):
        load_brand_cache_from_db()
    
    # Only rebuild if brand cache count changed or not loaded
    current_count = len(_BRAND_CACHE.get("by_name", {}))
    if _NORMALIZED_BRAND_CACHE["loaded"] and len(_NORMALIZED_BRAND_CACHE["list"]) == current_count:
        return

    logging.info(f"Building normalized brand cache for {current_count} brands...")
    norm_list = []
    norm_map = {}
    for key in _BRAND_CACHE.get("by_name", {}).keys():
        n = normalize_brand_name(key)
        norm_list.append(n)
        norm_map[n] = key
    
    _NORMALIZED_BRAND_CACHE["list"] = norm_list
    _NORMALIZED_BRAND_CACHE["map"] = norm_map
    _NORMALIZED_BRAND_CACHE["loaded"] = True
    logging.info("Normalized brand cache built.")

def match_brand_from_cache(brand_name: str) -> Optional[Dict[str, Any]]:
    """
    Find brand in cache using LEGACY logic (Exhaustive Search).
    Strategies:
    1. Exact Match
    2. Exact Normalized Match
    3. Containment (Norm)
    4. Word Subset (Norm)
    """
    if not _BRAND_CACHE.get("loaded"):
        load_brand_cache_from_db()

    if not _BRAND_CACHE.get("by_name"):
        logging.warning("Brand cache is EMPTY! Matching will fail.")
        return None
    
    if not brand_name:
        return None
    
    # 1. Exact match (fastest)
    key = brand_name.lower().strip()
    cached = _BRAND_CACHE["by_name"].get(key)
    if cached:
        return cached
    
    # Prepare normalized search
    normalized_search = normalize_brand_name(brand_name)
    search_words = set(normalized_search.split())
    
    # Iterate ALL brands (Legacy Style)
    # This might be O(N) but guarantees finding "Adidas Turkey" -> "Adidas"
    
    for cache_key, cache_val in _BRAND_CACHE["by_name"].items():
        normalized_cache = normalize_brand_name(cache_val["name"]) # Use actual name if possible or key
        
        # 2. Exact Normalized
        if normalized_search == normalized_cache:
            logging.info(f"Legacy Match (Exact Norm): '{brand_name}' -> '{cache_val['name']}'")
            return cache_val
            
        # 3. Containment
        # "Adidas" in "Adidas Türkiye"
        if normalized_search in normalized_cache or normalized_cache in normalized_search:
             # Sanity check: Don't match very short strings like "A" in "Apple"
            if len(normalized_search) >= 3 and len(normalized_cache) >= 3:
                logging.info(f"Legacy Match (Containment): '{brand_name}' <-> '{cache_val['name']}'")
                return cache_val
        
        # 4. Word Subset
        # "Adidas Sport" vs "Adidas"
        if search_words:
            cache_words = set(normalized_cache.split())
            if cache_words:
                 # Search words inside Cache words? ("Adidas" in "Adidas Sport")
                if search_words.issubset(cache_words):
                    logging.info(f"Legacy Match (Subset 1): '{brand_name}' -> '{cache_val['name']}'")
                    return cache_val
                # Cache words inside Search words? ("Adidas" in "Adidas Türkiye")
                if cache_words.issubset(search_words):
                    logging.info(f"Legacy Match (Subset 2): '{brand_name}' -> '{cache_val['name']}'")
                    return cache_val

    return None

def get_cached_brand_id(brand_name: str, default_id: int = 2770299) -> int:
    """Get brand ID from cache with legacy matching."""
    match = match_brand_from_cache(brand_name)
    return match["id"] if match else default_id

def get_brand_cache_stats() -> Dict[str, Any]:
    """Get brand cache statistics."""
    if not _BRAND_CACHE.get("loaded"):
        load_brand_cache_from_db()
    return {
        "loaded": _BRAND_CACHE.get("loaded", False),
        "count": _BRAND_CACHE.get("count", 0)
    }

def get_trendyol_client(user_id: int = None) -> TrendyolClient:
    """Get Trendyol client with user-specific credentials."""
    # Get user_id from current_user if not provided
    if user_id is None:
        try:
            from flask_login import current_user
            if current_user and current_user.is_authenticated:
                user_id = current_user.id
        except Exception:
            pass
    
    seller_id = Setting.get("SELLER_ID", "", user_id=user_id).strip()
    api_key = Setting.get("API_KEY", "", user_id=user_id).strip()
    api_secret = Setting.get("API_SECRET", "", user_id=user_id).strip()
    cookies_str = (Setting.get("TRENDYOL_COOKIES", "", user_id=user_id) or "").strip()
    if not (seller_id and api_key and api_secret):
        raise ValueError("Trendyol API bilgileri eksik. Ayarlar sayfasından SELLER_ID, API_KEY ve API_SECRET giriniz.")
    return TrendyolClient(seller_id=seller_id, api_key=api_key, api_secret=api_secret, cookies_str=cookies_str)

def fetch_trendyol_categories_flat(auth):
    try:
        url = "https://apigw.trendyol.com/integration/product/product-categories"
        resp = requests.get(url, auth=auth, timeout=60)
        resp.raise_for_status()
        cats = resp.json().get("categories", [])
        flat = []
        def _flatten(nodes, path=None):
            path = path or []
            for c in nodes or []:
                cur_path = path + [c.get("name", "")]
                flat.append({
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "path": " / ".join([p for p in cur_path if p]),
                    "subCategories": c.get("subCategories", []),
                })
                _flatten(c.get("subCategories", []), cur_path)
        _flatten(cats)
        # leafs only
        leafs = [c for c in flat if not c.get("subCategories")]
        return leafs
    except Exception:
        return []

def match_category_id_for_title(title: str, leaf_categories: List[Dict[str, Any]]):
    title = (title or "").strip()
    if not title or not leaf_categories:
        return 0
    names = [c.get("name", "") for c in leaf_categories]
    best = get_close_matches(title, names, n=1, cutoff=0.3)
    if not best:
        return 0
    try:
        idx = names.index(best[0])
        return int(leaf_categories[idx].get("id") or 0)
    except Exception:
        return 0

def prepare_tfidf(leaf_categories: List[Dict[str, Any]]):
    names = [c.get('name','') for c in leaf_categories]
    if not names:
        _CAT_TFIDF.update({"leaf": [], "names": [], "vectorizer": None, "matrix": None})
        return
    vec = TfidfVectorizer(analyzer='char_wb', ngram_range=(2,4))
    vec.fit(names)
    mat = vec.transform(names)
    _CAT_TFIDF.update({"leaf": leaf_categories, "names": names, "vectorizer": vec, "matrix": mat})

def match_category_id_for_title_tfidf(title: str) -> int:
    if not title or not _CAT_TFIDF.get('vectorizer'):
        return 0
    vec = _CAT_TFIDF['vectorizer']
    mat = _CAT_TFIDF['matrix']
    names = _CAT_TFIDF['names']
    leaf = _CAT_TFIDF['leaf']
    try:
        q = vec.transform([title])
        sims = cosine_similarity(q, mat)[0]
        idx = int(sims.argmax())
        score = float(sims[idx])
        if score >= 0.30:
            return int(leaf[idx].get('id') or 0)
        return 0
    except Exception:
        return 0

def prepare_brand_tfidf():
    """Builds TF-IDF matrix for brands from cache."""
    if not _BRAND_CACHE.get("loaded"):
        load_brand_cache_from_db()
    
    # Extract brands list
    brands = [] 
    for name, data in _BRAND_CACHE.get("by_name", {}).items():
        brands.append({"id": data["id"], "name": data.get("name", name)})
        
    names = [b.get('name','') for b in brands]
    
    if not names:
        _BRAND_TFIDF.update({"leaf": [], "names": [], "vectorizer": None, "matrix": None})
        return

    logging.info(f"Building Brand TF-IDF for {len(names)} brands...")
    # Use char_wb ngram similar to categories but maybe range 2-4 is good
    vec = TfidfVectorizer(analyzer='char_wb', ngram_range=(2, 4))
    vec.fit(names)
    mat = vec.transform(names)
    
    _BRAND_TFIDF.update({"leaf": brands, "names": names, "vectorizer": vec, "matrix": mat})
    logging.info("Brand TF-IDF built.")

def ensure_brand_tfidf_ready():
    """Ensures Brand TF-IDF is built if cache is loaded."""
    if not _BRAND_TFIDF.get("vectorizer"):
        prepare_brand_tfidf()

def match_brand_id_for_name_tfidf(name: str) -> int:
    """Match single brand name using TF-IDF. Returns Brand ID or 0."""
    if not name or not _BRAND_TFIDF.get('vectorizer'):
        return 0
        
    vec = _BRAND_TFIDF['vectorizer']
    mat = _BRAND_TFIDF['matrix']
    leaf = _BRAND_TFIDF['leaf']
    
    try:
        q = vec.transform([name])
        sims = cosine_similarity(q, mat) # returns (1, n_brands)
        # sims[0] is the array of similarities
        row = sims[0]
        idx = int(row.argmax())
        score = float(row[idx])
        
        if score >= 0.40: 
            return int(leaf[idx].get('id') or 0)
        return 0
    except Exception:
        return 0

def match_brands_tfidf_batch(names: list[str]) -> Dict[str, Optional[Dict[str, Any]]]:
    """
    Match multiple brands at once using vectorized TF-IDF.
    Returns dict: { 'search_name': {id, name} or None }
    """
    results = {name: None for name in names}
    if not names or not _BRAND_TFIDF.get('vectorizer'):
        return results

    vec = _BRAND_TFIDF['vectorizer']
    mat = _BRAND_TFIDF['matrix']
    leaf = _BRAND_TFIDF['leaf']
    
    # Filter empty names
    valid_names = [n for n in names if n and n.strip()]
    if not valid_names:
        return results

    try:
        # Transform all at once: (n_samples, n_features)
        Q = vec.transform(valid_names)
        
        # Compute similarity: (n_samples, n_brands)
        # This is much faster than loop
        sims = cosine_similarity(Q, mat)
        
        for i, name in enumerate(valid_names):
            row = sims[i]
            idx = int(row.argmax())
            score = float(row[idx])
            match_name = leaf[idx].get('name', '')
            
            # STRICTER LOGIC (TUNED)
            # 1. Base Threshold: 0.60 (Was 0.75, user said many missed)
            # 2. First Char Check: Required for 0.60 - 0.85 range
            #    If score > 0.85, we trust it even if first char differs (e.g. "The Nike" -> "Nike")
            
            is_match = False
            
            if score >= 0.85:
                is_match = True
            elif score >= 0.60:
                # Mid-confidence: require first letter match to avoid "Zetina"/"Betina"
                if name and match_name and name[0].lower() == match_name[0].lower():
                    is_match = True
                else:
                    logging.info(f"TF-IDF Rejected '{name}' -> '{match_name}' (score {score:.2f}) due to first char mismatch")

            if is_match:
                results[name] = leaf[idx]
                logging.info(f"TF-IDF Batch Match '{name}': '{match_name}' (score: {score:.4f})")
            else:
                logging.info(f"TF-IDF Batch No Match '{name}' (best: '{match_name}' score: {score:.4f}, rejected)")
                
    except Exception as e:
        logging.error(f"TF-IDF Batch error: {e}")
        
    return results

def load_trendyol_snapshot() -> Dict[str, Any]:
    cached = getattr(load_trendyol_snapshot, '_cache', None)
    cached_ts = getattr(load_trendyol_snapshot, '_cache_ts', 0)
    now = time.time()
    # Assuming Config is available or we pass it. Let's use Setting or hardcode default for now to avoid circular import with Config if not careful.
    # Actually Config is in config.py, safe to import.
    from config import Config
    ttl = Config.TRENDYOL_SNAPSHOT_TTL
    
    if cached and ttl and (now - cached_ts) <= ttl:
        return cached
    try:
        raw = Setting.get('TRENDYOL_EXPORT_SNAPSHOT', '') or ''
        if not raw:
            return {}
        data = json.loads(raw)
        items = data.get('items') or []
        by_barcode = {str((item or {}).get('barcode')): item for item in items if (item or {}).get('barcode')}
        payload = {
            'items': items,
            'by_barcode': by_barcode,
            'total': data.get('count') or data.get('total') or len(items),
            'saved_at': data.get('saved_at'),
        }
        load_trendyol_snapshot._cache = payload
        load_trendyol_snapshot._cache_ts = now
        return payload
    except Exception:
        return {}

def fetch_all_trendyol_products(user_id: int = None, job_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch ALL products from Trendyol API with pagination."""
    from app.services.job_queue import update_mp_job, get_mp_job
    
    try:
        client = get_trendyol_client(user_id=user_id)
    except ValueError as e:
        if job_id:
            append_mp_job_log(job_id, f"Hata: {str(e)}", level='error')
        logging.warning(f"Trendyol credentials missing for user {user_id}: {str(e)}")
        return []

    all_items = []
    page = 0
    size = 100 # Safe batch size
    total_elements = 0
    
    if job_id:
        append_mp_job_log(job_id, "Trendyol'dan güncel ürün listesi çekiliyor...")

    while True:
        # Check for pause/cancel (if job_id is provided)
        if job_id:
            job_state = get_mp_job(job_id)
            if job_state:
                if job_state.get('cancel_requested'):
                    append_mp_job_log(job_id, "İşlem kullanıcı tarafından iptal edildi.", level='warning')
                    break
                
                while job_state.get('pause_requested'):
                    append_mp_job_log(job_id, "İşlem duraklatıldı. Devam etmesi bekleniyor...", level='info')
                    time.sleep(5)
                    job_state = get_mp_job(job_id)
                    if job_state.get('cancel_requested'):
                        append_mp_job_log(job_id, "İşlem kullanıcı tarafından iptal edildi.", level='warning')
                        break
                
                if job_state.get('cancel_requested'):
                    break
        
        try:
            # Note: Trendyol API uses 0-based index for some, 1-based for others. 
            # list_products implementation in client seems to pass page directly.
            # Let's assume 0-based as per test script usage.
            resp = client.list_products(page=page, size=size)
            items = resp.get('content', [])
            if not items:
                break
            
            all_items.extend(items)
            total_elements = resp.get('totalElements', 0)
            
            # Update progress
            if job_id:
                update_mp_job(job_id, progress={
                    'current': len(all_items), 
                    'total': total_elements,
                    'message': f'{len(all_items)} / {total_elements} ürün çekildi'
                })
                
                if page % 5 == 0:
                     append_mp_job_log(job_id, f"{len(all_items)} / {total_elements} ürün çekildi...")

            if len(all_items) >= total_elements:
                break
                
            page += 1
            time.sleep(0.5) # Rate limit protection
            
        except Exception as e:
            if job_id:
                append_mp_job_log(job_id, f"Ürün çekme hatası (Sayfa {page}): {e}", level='error')
            logging.error(f"Error fetching trendyol products page {page}: {e}")
            break
            
    if job_id:
        append_mp_job_log(job_id, f"Toplam {len(all_items)} ürün başarıyla çekildi.")
        
    return all_items

def refresh_trendyol_cache(job_id: Optional[str] = None, user_id: int = None) -> Dict[str, Any]:
    """Fetch all products and sync to MarketplaceProduct table."""
    try:
        from app.models import MarketplaceProduct
        from app import db
        from flask_login import current_user
        
        if user_id is None:
            user_id = current_user.id if current_user and current_user.is_authenticated else None
            
        # If running from job, we might need a way to know the user. 
        # For now, if no user, we might fail or default to 1? 
        # But this function is usually called from UI (with user) or job (triggered by user).
        # Existing get_trendyol_client relies on context.
        
        if not user_id:
             # Try to guess from Settings if possible or fail
             # Usually background scheduler needs user context.
             # For Phase 1 we assume context is available or user is 1.
             # Or we skip user_id check and get_trendyol_client handles it.
             pass

        items = fetch_all_trendyol_products(user_id=user_id, job_id=job_id)
        
        if not user_id and items:
             # Fallback: if we have items but no user_id, 
             # we check if we can get user_id from existing context or fail.
             # However, get_trendyol_client succeeded, so we have credentials.
             # We should probably fetch user_id associated with those credentials? 
             # Impossible without reverse lookup.
             # We will rely on current_user being correctly set or passed.
             # If user_id is None, we can't save to DB properly if user_id is required.
             # Check Product model: user_id is nullable? Yes.
             pass

        count = 0
        batch_size = 100
        
        if job_id:
            append_mp_job_log(job_id, f"Veritabanına kaydediliyor ({len(items)} ürün)...")
            
        # Strategy: Mark all as 'sync_pending', upsert, then delete 'sync_pending'?
        # Or Just get all barcodes.
        
        remote_barcodes = set()
        
        for chunk in chunked(items, batch_size):
            for item in chunk:
                barcode = item.get('barcode', '')
                if not barcode: continue
                remote_barcodes.add(barcode)
                
                # Basic fields
                stock_code = item.get('stockCode') or item.get('productMainId') or ''
                title = item.get('title') or ''
                brand = item.get('brand') or ''
                if isinstance(brand, dict): brand = brand.get('name', '')
                category = item.get('categoryName') or ''
                
                # Status
                on_sale = item.get('onSale')
                approved = item.get('approved')
                # Map to status string
                # Status mapping: Standardize to Aktif/Pasif
                status_str = "Aktif" if on_sale else "Pasif"
                approval_str = "Onaylandı" if approved else ("Reddedildi" if item.get('rejected') else "Beklemede")
                
                if not on_sale and not approved:
                    status_str = "Arşivlenmiş"
                
                # Price/Qty
                list_price = float(item.get('listPrice', 0))
                sale_price = float(item.get('salePrice', 0))
                quantity = int(item.get('stock', 0) if 'stock' in item else item.get('quantity', 0))
                
                images = item.get('images', [])
                img_json = json.dumps([img['url'] for img in images if isinstance(img, dict) and 'url' in img])
                
                if not user_id:
                    continue
                    
                # Check existing
                existing = MarketplaceProduct.query.filter_by(
                    marketplace='trendyol', 
                    barcode=barcode,
                    user_id=user_id
                ).first()
                
                if existing:
                    existing.stock_code = stock_code
                    existing.title = title
                    existing.brand = brand
                    existing.category = category
                    existing.price = list_price
                    existing.sale_price = sale_price
                    existing.quantity = quantity
                    existing.status = status_str
                    existing.approval_status = approval_str
                    existing.on_sale = bool(on_sale)
                    existing.images_json = img_json
                    existing.raw_data = json.dumps(item)
                    existing.last_sync_at = datetime.now()
                else:
                    new_prod = MarketplaceProduct(
                        user_id=user_id,
                        marketplace='trendyol',
                        barcode=barcode,
                        stock_code=stock_code,
                        title=title,
                        price=list_price,
                        sale_price=sale_price,
                        quantity=quantity,
                        brand=brand,
                        category=category,
                        status=status_str,
                        approval_status=approval_str,
                        on_sale=bool(on_sale),
                        images_json=img_json,
                        raw_data=json.dumps(item)
                    )
                    db.session.add(new_prod)
                
                count += 1
            
            db.session.commit()
            
        # Delete items not in remote (Sync)
        # Only for this user and marketplace
        if user_id:
            try:
                db.session.query(MarketplaceProduct).filter(
                    MarketplaceProduct.user_id == user_id,
                    MarketplaceProduct.marketplace == 'trendyol',
                    ~MarketplaceProduct.barcode.in_(remote_barcodes)
                ).delete(synchronize_session=False)
                db.session.commit()
            except Exception as e:
                logging.error(f"Error cleaning up old products: {e}")
                db.session.rollback()

        # Update JSON Setting for backward compatibility (optional, but requested C implies using DB)
        # We can disable the JSON blob if it's too large.
        # But `load_trendyol_snapshot` uses it. We should update that too later.
        
        return {'success': True, 'count': count}
    except Exception as e:
        logging.exception("Error refreshing trendyol cache")
        return {'success': False, 'error': str(e)}


def perform_trendyol_sync_stock(job_id: str, xml_source_id: Any, user_id: int = None) -> Dict[str, Any]:
    client = get_trendyol_client(user_id=user_id)
    append_mp_job_log(job_id, "Trendyol istemcisi hazır")
    xml_index = load_xml_source_index(xml_source_id)
    mp_map = xml_index.get('by_barcode') or {}
    multiplier = get_marketplace_multiplier('trendyol')

    if not mp_map:
        append_mp_job_log(job_id, "XML kaynak haritası boş", level='warning')
        return {'success': False, 'message': 'XML kaynağında uygun ürün bulunamadı.', 'updated_count': 0}

    snapshot_local = load_trendyol_snapshot()
    local_by_barcode = snapshot_local.get('by_barcode') if snapshot_local else {}

    updates: List[Dict[str, Any]] = []
    missing_codes: List[str] = [] # Changed to list for JSON serialization safety if needed, but set is fine for logic
    missing_codes_set = set()
    changed_samples: List[Dict[str, Any]] = []

    for barcode, info in mp_map.items():
        qty = to_int(info.get('quantity'))
        if qty < 0:
            qty = 0
        updates.append({
            'barcode': barcode,
            'quantity': qty,
            'currencyType': 'TRY'
        })

        local_item = local_by_barcode.get(barcode) if local_by_barcode else None
        if not local_item:
            missing_codes_set.add(barcode)
        else:
            if len(changed_samples) < 5:
                changed_samples.append({
                    'barcode': barcode,
                    'prev_quantity': local_item.get('quantity'),
                    'new_quantity': qty
                })
    
    missing_codes = list(missing_codes_set)

    if not updates:
        return {'success': False, 'message': 'Güncellenecek stok bulunamadı.', 'updated_count': 0}

    summary = {
        'success': True,
        'updated_count': len(updates),
        'missing_codes': missing_codes[:20],
        'samples': changed_samples,
        'multiplier': multiplier,
        'job_id': job_id,
    }

    total_sent = 0
    for idx, chunk in enumerate(chunked(updates, 100), start=1):
        resp = client.update_price_inventory(chunk)
        total_sent += len(chunk)
        append_mp_job_log(job_id, f"{len(chunk)} ürüne stok güncellemesi gönderildi (paket {idx})")

    summary.update({
        'message': f'{total_sent} ürün için stok güncellemesi gönderildi.',
        'updated_count': total_sent,
    })
    return summary

def perform_trendyol_sync_prices(job_id: str, xml_source_id: Any, match_by: str = 'barcode', user_id: int = None) -> Dict[str, Any]:
    client = get_trendyol_client(user_id=user_id)
    append_mp_job_log(job_id, "Trendyol istemcisi hazır")
    xml_index = load_xml_source_index(xml_source_id)
    # If match_by is stock_code, we rely on lookup_xml_record, not direct map iteration?
    # BUT, to sync prices we usually iterate what we have in XML and send update.
    # OR we iterate what we have in Local/Trendyol and find in XML?
    # Standard logic: Iterate XML items -> Send update to MP if MP has it.
    
    mp_map = xml_index.get('by_barcode') or {}
    multiplier = get_marketplace_multiplier('trendyol')

    if not mp_map:
        append_mp_job_log(job_id, "XML kaynak haritası boş", level='warning')
        return {'success': False, 'message': 'XML kaynağında uygun ürün bulunamadı.', 'updated_count': 0}

    # Load local valid products to know what to update?
    # Ideally we should only update products that exist on Trendyol.
    # Current implementation loads 'snapshot_local' (from previous fetch).
    snapshot_local = load_trendyol_snapshot()
    local_by_barcode = snapshot_local.get('by_barcode') if snapshot_local else {}
    # Also index local by stock code if needed
    local_by_stock = {}
    if match_by == 'stock_code' and local_by_barcode:
         for b, d in local_by_barcode.items():
             sc = d.get('stockCode') or d.get('productCode')
             if sc: local_by_stock[sc] = d

    updates: List[Dict[str, Any]] = []
    missing_codes_set = set()
    skipped_zero_price: List[str] = []
    changed_samples: List[Dict[str, Any]] = []

    # Strategy: Iterate XML items
    for xml_barcode, info in mp_map.items():
        # Find corresponding local/Marketplace item
        found_local = None
        target_barcode = xml_barcode
        
        if match_by == 'stock_code':
            sc = info.get('stockCode')
            if sc and sc in local_by_stock:
                found_local = local_by_stock[sc]
                # We must send update for the MARKETPLACE barcode, not XML barcode
                target_barcode = found_local.get('barcode') 
            # If not found by stock code, maybe fallback to barcode?
            elif xml_barcode in local_by_barcode:
                 found_local = local_by_barcode[xml_barcode]
        else:
            if xml_barcode in local_by_barcode:
                found_local = local_by_barcode[xml_barcode]
        
        if not found_local:
             missing_codes_set.add(xml_barcode)
             continue

        base_price = to_float(info.get('price'))
        if base_price <= 0:
            skipped_zero_price.append(xml_barcode)
            continue
            
        # Artık GLOBAL_PRICE_RULES kullanılıyor (multiplier kaldırıldı)
        price = calculate_price(base_price, 'trendyol', user_id=user_id)
        qty = to_int(info.get('quantity'))
        
        updates.append({
            'barcode': target_barcode, # Important: Update correct MP barcode
            'salePrice': price,
            'listPrice': price,
            'currencyType': 'TRY',
            'quantity': qty if qty >= 0 else 0,
        })

        if len(changed_samples) < 5:
            changed_samples.append({
                'barcode': target_barcode,
                'prev_price': found_local.get('listPrice'),
                'new_price': price,
                'prev_quantity': found_local.get('quantity'),
                'new_quantity': qty,
                'match_type': match_by
            })
    
    missing_codes = list(missing_codes_set)
    
    if not updates:
        return {'success': False, 'message': 'Güncellenecek fiyat bulunamadı.', 'updated_count': 0, 'missing_codes': missing_codes[:20]}

    summary = {
        'success': True,
        'updated_count': len(updates),
        'missing_codes': missing_codes[:20],
        'skipped_zero_price': skipped_zero_price[:20],
        'samples': changed_samples,
        'multiplier': multiplier,
        'job_id': job_id,
    }

    total_sent = 0
    for idx, chunk in enumerate(chunked(updates, 100), start=1):
        resp = client.update_price_inventory(chunk)
        total_sent += len(chunk)
        append_mp_job_log(job_id, f"{len(chunk)} ürüne fiyat güncellemesi gönderildi (paket {idx})")

    summary.update({
        'message': f'{total_sent} ürün için fiyat güncellemesi gönderildi.',
        'updated_count': total_sent,
    })
    return summary





def ensure_tfidf_ready():
    if _CAT_TFIDF.get('vectorizer'):
        return
    
    # Check if cache is already loaded in memory
    if not _CATEGORY_CACHE.get("loaded"):
        load_category_cache_from_db()
    
    if _CATEGORY_CACHE.get("loaded") and _CATEGORY_CACHE.get("list"):
        logging.info(f"Preparing TF-IDF with {_CATEGORY_CACHE['count']} categories from memory cache.")
        prepare_tfidf(_CATEGORY_CACHE["list"])
        return

    # Fallback to direct DB fetch if memory cache is empty
    raw = Setting.get("TRENDYOL_CATEGORY_CACHE", "")
    if raw:
        try:
            data = json.loads(raw)
            leafs = data.get('list', [])
            if not leafs and isinstance(data, list):
                leafs = data
            
            if leafs:
                prepare_tfidf(leafs)
                logging.info(f"TF-IDF prepared from DB fallback ({len(leafs)} categories).")
        except Exception as e:
            logging.error(f"Error preparing TF-IDF: {e}")

def perform_trendyol_send_products(job_id: str, barcodes: List[str], xml_source_id: Any, auto_match: bool = False, match_by: str = 'barcode', title_prefix: str = None, user_id: int = None, **kwargs) -> Dict[str, Any]:
    # Resolve User ID from XML Source if not provided
    if not user_id and xml_source_id:
        try:
             s_id = str(xml_source_id)
             if s_id.isdigit():
                 src = SupplierXML.query.get(int(s_id))
                 if src: user_id = src.user_id
        except Exception as e:
             logging.warning(f"Failed to resolve user_id: {e}")

    client = get_trendyol_client(user_id=user_id)
    append_mp_job_log(job_id, f"Trendyol istemcisi başlatıldı (User ID: {user_id}).")

    # Extract options from kwargs or defaults
    zero_stock_as_one = kwargs.get('zero_stock_as_one', False)
    skip_no_image = kwargs.get('skip_no_image', False)
    skip_no_barcode = kwargs.get('skip_no_barcode', False)
    default_price = to_float(kwargs.get('default_price', 0))
    price_multiplier = to_float(kwargs.get('price_multiplier', 1.0))
    
    append_mp_job_log(job_id, f"Seçenekler: Stok0→1={zero_stock_as_one}, Görselsiz atla={skip_no_image}, Barkodsuz atla={skip_no_barcode}")
    
    xml_index = load_xml_source_index(xml_source_id)
    mp_map = xml_index.get('by_barcode') or {}
    
    # We use price_multiplier directly if provided, otherwise fallback to settings multiplier if apply_multiplier was intended
    # But now we passed it explicitly from UI.
    multiplier = price_multiplier
    
    # Debug logging
    append_mp_job_log(job_id, f"Kaynak tipi: {str(xml_source_id)[:20]}...")
    append_mp_job_log(job_id, f"Yüklenen ürün sayısı: {len(mp_map)}")
    if barcodes and len(barcodes) > 0:
        first_barcode = barcodes[0]
        found = mp_map.get(first_barcode)
        append_mp_job_log(job_id, f"İlk barkod ({first_barcode[:15]}): {'BULUNDU' if found else 'BULUNAMADI'}")
    
    if auto_match:
        append_mp_job_log(job_id, "Kategori ağacı yükleniyor...")
        ensure_tfidf_ready()
        if not _CAT_TFIDF.get('vectorizer'):
             append_mp_job_log(job_id, "Kategori ağacı bulunamadı! Ayarlardan 'Kategorileri Çek' işlemini yapınız.", level='error')
             return {'success': False, 'message': 'Kategori verisi eksik.', 'count': 0}

    items_to_send = []
    skipped = []
    matched_count = 0
    
    # Brand resolution - API-first approach, local cache to avoid duplicate API calls
    local_brand_cache = {}
    
    # Local Stock Code Index for Matching
    local_by_stock = {}
    if match_by == 'stock_code':
        try:
            snap = load_trendyol_snapshot()
            local_list = snap.get('by_barcode', {})
            for b, d in local_list.items():
                sc = d.get('stockCode') or d.get('productCode')
                if sc: local_by_stock[sc] = d
            append_mp_job_log(job_id, f"Stok kodu eşleşmesi için {len(local_by_stock)} yerel ürün indekslendi.")
        except Exception as e:
            append_mp_job_log(job_id, f"Snaphot yükleme hatası: {e}", level='warning')

    def resolve_brand_id(brand_name: str) -> int:
        """Resolve brand name to Trendyol brand ID using Trendyol API directly. Returns 0 if not found."""
        
        # Helper to get default brand
        def get_default_brand_id_safe():
            if global_default_brand > 0:
                return global_default_brand
            return 0

        # Handle empty or ignored brand names
        if not brand_name or brand_name.lower().strip() in ['glowify store', 'glowify', '']:
            default_id = get_default_brand_id_safe()
            if default_id > 0:
                 append_mp_job_log(job_id, f"Marka boş/geçersiz, varsayılan marka ID kullanılıyor: {default_id}", level='info')
                 return default_id
            return 0  # No default brand either
        
        brand_key = brand_name.lower().strip()
        
        # Check local session cache first (to avoid duplicate API calls in same job)
        if brand_key in local_brand_cache:
            return local_brand_cache[brand_key]
        
        # 0. Check Smart Matching (Database Confirmed)
        from app.services.smart_match_service import SmartMatchService
        sm_brand_id, sm_brand_name = SmartMatchService.get_brand_match(brand_name, 'trendyol')
        if sm_brand_id:
            local_brand_cache[brand_key] = sm_brand_id
            append_mp_job_log(job_id, f"Marka DB Eşleşmesi: '{brand_name}' -> {sm_brand_id} ({sm_brand_name})")
            return sm_brand_id

        # Directly call Trendyol API for brand resolution
        try:
            brands = client.get_brands_by_name(brand_name[:50])
            if brands:
                # Try exact case-insensitive match first
                for b in brands:
                    if b.get('name', '').lower().strip() == brand_key:
                        local_brand_cache[brand_key] = b['id']
                        append_mp_job_log(job_id, f"Marka '{brand_name}' API ile bulundu: {b['id']} ({b.get('name', '')})")
                        return b['id']
                
                # If exact match not found, use first result (partial match)
                first_brand = brands[0]
                local_brand_cache[brand_key] = first_brand['id']
                append_mp_job_log(job_id, f"Marka '{brand_name}' API ile kısmi eşleşme: {first_brand['id']} ({first_brand.get('name', '')})")
                return first_brand['id']
            else:
                append_mp_job_log(job_id, f"Marka '{brand_name}' API'de bulunamadı", level='warning')
        except Exception as e:
            append_mp_job_log(job_id, f"Marka arama hatası ({brand_name}): {e}", level='warning')
        
        # Fallback to default brand from settings if configured
        # Fallback to default brand if configured (using prefetched global_default_brand)
        if global_default_brand > 0:
            local_brand_cache[brand_key] = global_default_brand
            append_mp_job_log(job_id, f"Marka '{brand_name}' bulunamadı, varsayılan marka ID kullanılıyor: {global_default_brand}", level='info')
            return global_default_brand
        
        # Cache the failure to avoid repeated API calls for same brand
        local_brand_cache[brand_key] = 0
        return 0  # Return 0 if not found - product will be skipped

    total_items = len(barcodes)
    processed = 0
    append_mp_job_log(job_id, f"İşlenecek barkod sayısı: {total_items}")
    
    # Simplified default attributes
    DEFAULT_ATTRS = {
        "Renk": "Belirtilmemiş",
        "Menşei": "TR",
        "Web Color": "Belirtilmemiş"
    }

    def build_simple_attributes(category_id: int, variant_attributes: List[dict] = None, product_title: str = "") -> List[dict]:
        """Build minimal required attributes for a category, integrating variant attributes if provided"""
        try:
            attrs = client.get_category_attributes(category_id)
            payload = []
            
            # Synonym mapping for attribute names
            SYNONYMS = {
                "beden": ["numara", "size", "ölçü", "ebat"],
                "renk": ["color", "colour"],
                "numara": ["beden", "size"]
            }
            
            # Helper to find a value for an attribute in variant_attributes
            def get_variant_value(attr_name):
                if not variant_attributes: return None
                attr_name_lower = attr_name.lower()
                
                # Direct match
                for va in variant_attributes:
                    v_name = va['name'].lower()
                    if v_name in attr_name_lower or attr_name_lower in v_name:
                        return va['value']
                
                # Synonym match
                for standard_name, variants in SYNONYMS.items():
                    if standard_name in attr_name_lower:
                        for syn in variants:
                            for va in variant_attributes:
                                if syn in va['name'].lower():
                                    return va['value']
                return None

            for attr_def in attrs.get("categoryAttributes", []):
                attr_info = attr_def.get("attribute", {})
                attr_id = attr_info.get("id")
                attr_name = attr_info.get("name", "")
                required = attr_def.get("required", False)
                allow_custom = attr_def.get("allowCustom", False)
                attr_values = attr_def.get("attributeValues", [])
                
                if not attr_id or not required:
                    continue
                
                item = {"attributeId": attr_id}
                
                # Try to get value from variant_attributes
                val_from_xml = get_variant_value(attr_name)
                
                # Search for the value in Trendyol's attribute values
                matched_val_id = None
                if val_from_xml and attr_values:
                    val_from_xml_lower = val_from_xml.lower()
                    for v in attr_values:
                        if v['name'].lower() == val_from_xml_lower:
                            matched_val_id = v['id']
                            break
                    # Fuzzy match if exact fails
                    if not matched_val_id:
                        for v in attr_values:
                            if v['name'].lower() in val_from_xml_lower or val_from_xml_lower in v['name'].lower():
                                matched_val_id = v['id']
                                break

                if matched_val_id:
                    item["attributeValueId"] = matched_val_id
                elif allow_custom and val_from_xml:
                    item["customAttributeValue"] = str(val_from_xml)
                elif attr_values:
                    # Fallback: check if we can match from title if not in variant_attributes
                    for v in attr_values:
                        if v['name'].lower() in product_title.lower():
                            matched_val_id = v['id']
                            break
                    
                    if matched_val_id:
                        item["attributeValueId"] = matched_val_id
                    else:
                        # Final resort: use first value
                        item["attributeValueId"] = attr_values[0]['id']
                elif allow_custom:
                    item["customAttributeValue"] = DEFAULT_ATTRS.get(attr_name, "Bilinmiyor")
                else:
                    continue
                
                payload.append(item)
            
            # Debug log for first few products
            if variant_attributes:
                append_mp_job_log(job_id, f"VARYANT ATTR: {variant_attributes} -> {len(payload)} attr gönderilecek", level='debug')
            
            return payload
        except Exception as e:
            append_mp_job_log(job_id, f"Öznitelik hatası (Cat: {category_id}): {e}", level='warning')
            return []

    for barcode in barcodes:
        # Check for cancel request
        job_state = get_mp_job(job_id)
        if job_state and job_state.get('cancel_requested'):
            append_mp_job_log(job_id, f"İşlem iptal edildi. {processed}/{total_items} ürün işlendi.", level='warning')
            break
        
        processed += 1

        product = mp_map.get(barcode)
        if not product:
            skipped.append({'barcode': barcode, 'reason': 'XML verisi yok'})
            continue

        target_barcode = barcode
        if match_by == 'stock_code':
            sc = product.get('stockCode')
            if sc and sc in local_by_stock:
                matched_local = local_by_stock[sc]
                target_barcode = matched_local.get('barcode')
                append_mp_job_log(job_id, f"Stok Kodu Eşleşmesi: XML({barcode}) -> MP({target_barcode})")
            
        # Blacklist check (Forbidden words/brands/categories)
        forbidden_reason = is_product_forbidden(user_id, title=product.get('title'), brand=product.get('brand'), category=product.get('category'))
        if forbidden_reason:
            skipped.append({'barcode': barcode, 'reason': f"Yasaklı Liste: {forbidden_reason}"})
            continue
        title = clean_forbidden_words(product.get('title', ''))
        if title_prefix:
             title = f"{title_prefix} {title}"
        desc = clean_forbidden_words(product.get('description', '') or title)
        
        # Debug: Log first few titles to verify prefix
        if processed <= 3:
            append_mp_job_log(job_id, f"DEBUG Title for {barcode}: '{title[:80]}...'")
        
        # ========================================
        # BRAND RESOLUTION - Priority: Settings → Excel
        # ========================================
        brand_id = 0
        brand_name = ""
        
        # 1. FIRST: Check Settings for default brand (fastest, no API call)
        settings_brand_id = Setting.get("TRENDYOL_BRAND_ID", user_id=user_id)
        settings_brand_name = Setting.get("TRENDYOL_BRAND_NAME", user_id=user_id)
        
        if settings_brand_id and settings_brand_id.strip():
            try:
                brand_id = int(settings_brand_id)
                brand_name = settings_brand_name or "Ayarlardaki Marka"
                if processed <= 3:
                    append_mp_job_log(job_id, f"Settings marka kullanıldı: '{brand_name}' (ID: {brand_id})")
            except:
                pass
        
        # 2. FALLBACK: Use Excel/XML brand if Settings not configured
        if not brand_id:
            excel_brand = product.get('brand', '') or product.get('vendor', '')
            if excel_brand:
                append_mp_job_log(job_id, f"Excel marka: '{excel_brand}' - barcode: {barcode}")
                brand_name = excel_brand
                
                # Check if pre-resolved brand_id from Excel index
                brand_id = product.get('brand_id') or product.get('brandId') or 0
                
                # If not pre-resolved, use resolve_brand_id (handles API lookup)
                if not brand_id:
                    brand_id = resolve_brand_id(excel_brand)
        
        # 3. ERROR: No brand found
        if not brand_id:
            skipped.append({'barcode': barcode, 'reason': f'Marka bulunamadı: {brand_name or "boş"} (Ayarlarda varsayılan marka tanımlayın)'})
            continue

        # Resolve Category ID
        category_id = 0
        excel_category = product.get('category', '')
        
        # 1. First check for pre-resolved category_id from Excel index
        category_id = product.get('category_id') or product.get('categoryId') or 0
        
        
        # 2. If not pre-resolved, try Smart Match DB (Confirmed Mappings)
        if not category_id and excel_category:
            from app.services.smart_match_service import SmartMatchService
            sm_cat_id, sm_cat_path = SmartMatchService.get_category_match(excel_category, 'trendyol')
            if sm_cat_id:
                category_id = sm_cat_id
                append_mp_job_log(job_id, f"Kategori DB Eşleşmesi: '{excel_category}' -> {category_id} ({sm_cat_path})")
        
        # 3. If not in DB, try exact/partial cache match
        if not category_id and excel_category:
            category_id = get_cached_category_id(excel_category, default_id=0)
            if category_id:
                append_mp_job_log(job_id, f"Kategori cache'den eşleşti: '{excel_category}' -> {category_id}")
        
        # 3. If still no match, try TF-IDF with Excel category name
        if not category_id and excel_category and auto_match:
            category_id = match_category_id_for_title_tfidf(excel_category)
            if category_id:
                append_mp_job_log(job_id, f"Kategori TF-IDF (kategori ismi): '{excel_category}' -> {category_id}")
        
        # 4. Last resort: TF-IDF with product title
        if not category_id and auto_match:
            category_id = match_category_id_for_title_tfidf(title)
            if category_id:
                matched_count += 1
                append_mp_job_log(job_id, f"Kategori TF-IDF (ürün başlığı): '{title[:50]}...' -> {category_id}")
        
        if not category_id:
            skipped.append({'barcode': barcode, 'reason': f'Kategori eşleşmedi: {excel_category or "boş"}'})
            continue

        # Price & Stock
        try:
            # Artık GLOBAL_PRICE_RULES kullanılıyor (multiplier kaldırıldı)
            base_price = calculate_price(float(product.get('price', 0)), 'trendyol', user_id=user_id)
            stock = int(product.get('quantity', 0))
        except:
            base_price = 0
            stock = 0
        
        # Apply zero_stock_as_one option
        if stock <= 0 and zero_stock_as_one:
            stock = 1
            append_mp_job_log(job_id, f"Stok 0→1 uygulandı: {barcode}")
        
        # Apply default_price if product price is 0
        if base_price <= 0 and default_price > 0:
            # Artık GLOBAL_PRICE_RULES kullanılıyor (multiplier kaldırıldı)
            base_price = calculate_price(default_price, 'trendyol', user_id=user_id)
            append_mp_job_log(job_id, f"Varsayılan fiyat uygulandı: {barcode} → {base_price}")
            
        if base_price <= 0:
            skipped.append({'barcode': barcode, 'reason': 'Fiyat 0 (varsayılan fiyat da girilmemiş)'})
            continue

        # CRITICAL: listPrice must be > salePrice
        salePrice = round(base_price, 2)
        listPrice = round(salePrice * 1.05, 2)  # 5% higher

        # Get product images and normalize format
        raw_images = product.get('images', [])
        product_images = []
        for img in raw_images[:8]:
            if isinstance(img, dict):
                # If it's a dict, extract the 'url' key
                url = img.get('url', '')
                if url:
                    product_images.append(url)
            elif isinstance(img, str) and img:
                # If it's already a string, use it directly
                product_images.append(img)
        
        # Skip products without images if option enabled
        if not product_images and skip_no_image:
            skipped.append({'barcode': barcode, 'reason': 'Görsel yok (atlandı)'})
            continue
        
        if not product_images:
            product_images = ["https://via.placeholder.com/500"]

        # Build minimal required attributes
        attributes_payload = build_simple_attributes(category_id, variant_attributes=product.get('variant_attributes'), product_title=title)

        # Determine Product Main ID (Crucial for variant grouping on Trendyol)
        # Fixed priority: modelCode > parent_barcode > productCode > current barcode
        # parent_barcode is preferred over productCode as productCode often includes size (SKU)
        pm_id = product.get('modelCode') or product.get('parent_barcode') or product.get('productCode') or barcode
        
        # Determine Description (Prefer HTML 'details' if available)
        final_desc = clean_forbidden_words(product.get('details') or product.get('description') or title)
        
        # VAT Rate (0, 1, 10, 20)
        raw_vat = int(product.get('vatRate', 20))
        item_vat = raw_vat if raw_vat in (0, 1, 10, 20) else 20

        # Build V2 Payload Item
        item = {
            "barcode": barcode,
            "title": title[:100],
            "productMainId": pm_id,
            "brandId": brand_id,
            "categoryId": category_id,
            "quantity": stock,
            "stockCode": product.get('stock_code') or barcode,
            "dimensionalWeight": 2,
            "description": final_desc,
            "currencyType": "TRY",
            "listPrice": listPrice,
            "salePrice": salePrice,
            "vatRate": item_vat,
            "cargoCompanyId": 10,
            "images": [{"url": url} for url in product_images],
            "attributes": attributes_payload
        }
        items_to_send.append(item)
        
        # Debug log for first few products to verify variant grouping
        if processed <= 5:
            append_mp_job_log(job_id, f"DEBUG [{barcode}]: productMainId={pm_id}, parent_barcode={product.get('parent_barcode')}, variant_attrs={product.get('variant_attributes')}", level='debug')
        
        if processed % 10 == 0:
             append_mp_job_log(job_id, f"{processed}/{total_items} ürün işlendi...")

    # Log skip reason summary
    if skipped:
        reason_counts = {}
        for s in skipped:
            reason = s.get('reason', 'Bilinmeyen')
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        for reason, count in reason_counts.items():
            append_mp_job_log(job_id, f"Atlanan: {count} ürün - {reason}", level='warning')
    
    append_mp_job_log(job_id, f"Gönderilecek ürün sayısı: {len(items_to_send)}")

    if not items_to_send:
        return {
            'success': False, 
            'message': 'Gönderilecek geçerli ürün oluşturulamadı.',
            'skipped': skipped,
            'count': 0
        }

    # Send in batches
    success_count = 0
    fail_count = 0
    failures = []
    batch_ids = []
    


    batch_size = 50
    total_batches = (len(items_to_send) + batch_size - 1) // batch_size
    
    for i in range(0, len(items_to_send), batch_size):
        # Check Job Status for Cancel/Pause
        job_state = get_mp_job(job_id)
        if job_state:
            if job_state.get('cancel_requested'):
                append_mp_job_log(job_id, "İşlem kullanıcı tarafından iptal edildi.", level='warning')
                break
            
            while job_state.get('pause_requested'):
                append_mp_job_log(job_id, "İşlem duraklatıldı. Devam etmesi bekleniyor...", level='info')
                time.sleep(5)
                job_state = get_mp_job(job_id)
                if job_state.get('cancel_requested'):
                    break
            
            if job_state.get('cancel_requested'):
                append_mp_job_log(job_id, "İşlem kullanıcı tarafından iptal edildi.", level='warning')
                break

        current_batch_num = (i // batch_size) + 1
        update_mp_job(job_id, progress={'current': success_count + fail_count, 'total': len(items_to_send), 'batch': f"{current_batch_num}/{total_batches}"})

        batch = items_to_send[i:i+batch_size]
        try:
            resp = client.create_products(batch)
            batch_req_id = resp.get('batchRequestId')
            batch_ids.append(batch_req_id)
            append_mp_job_log(job_id, f"Batch {current_batch_num}/{total_batches} gönderildi. ID: {batch_req_id}")
            
            # Wait a bit for Trendyol to process
            time.sleep(3)
            
            # Check batch status for detailed results
            try:
                batch_status = client.check_batch_status(batch_req_id)
                # append_mp_job_log(job_id, f"Batch durumu: {batch_status.get('status', 'UNKNOWN')}")
                
                # Get item-level details
                items_detail = batch_status.get('items', [])
                for item_detail in items_detail:
                    item_status = item_detail.get('status', '')
                    barcode = item_detail.get('barcode', '')
                    
                    if item_status == 'SUCCESS':
                        success_count += 1
                    else:
                        fail_count += 1
                        errors = item_detail.get('failureReasons', [])
                        error_msg = '; '.join(errors) if errors else 'Bilinmeyen hata'
                        failures.append({
                            'barcode': barcode,
                            'reason': error_msg
                        })
                        append_mp_job_log(job_id, f"❌ {barcode}: {error_msg}", level='warning')
                
                # Update progress after batch check
                update_mp_job(job_id, progress={'current': success_count + fail_count, 'total': len(items_to_send), 'batch': f"{current_batch_num}/{total_batches}"})

            except Exception as e:
                append_mp_job_log(job_id, f"Batch durum sorgulanamadı: {e}", level='warning')
                success_count += len(batch) # Assume success if check fails? Or fail? Let's assume success to not block flow, but log warning.
                
        except Exception as e:
            fail_count += len(batch)
            failures.append({'reason': str(e)})
            append_mp_job_log(job_id, f"Batch gönderim hatası: {e}", level='error')

    return {
        'success': True,
        'count': len(items_to_send),
        'matched': [{'barcode': i['barcode']} for i in items_to_send],
        'skipped': skipped,
        'batch_ids': batch_ids,
        'summary': {
            'success_count': success_count,
            'fail_count': fail_count,
            'failures': failures
        }
    }

def perform_trendyol_send_all(job_id: str, xml_source_id: Any, auto_match: bool = False, user_id: int = None, **kwargs) -> Dict[str, Any]:
    """Send ALL products from XML source to Trendyol"""
    append_mp_job_log(job_id, "Tüm ürünler hazırlanıyor...")
    
    xml_index = load_xml_source_index(xml_source_id)
    mp_map = xml_index.get('by_barcode') or {}
    all_barcodes = list(mp_map.keys())
    
    if not all_barcodes:
        return {'success': False, 'message': 'XML kaynağında ürün bulunamadı.', 'count': 0}
    
    append_mp_job_log(job_id, f"Toplam {len(all_barcodes)} ürün bulundu. Gönderim başlıyor...")
    
    return perform_trendyol_send_products(job_id, all_barcodes, xml_source_id, auto_match=auto_match, user_id=user_id, **kwargs)


def perform_trendyol_batch_update(job_id: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Directly update stock/price for a list of items (from Excel Bulk Update).
    items: [{'barcode': '...', 'stock': 10, 'price': 100.0}, ...]
    """
    client = get_trendyol_client()
    append_mp_job_log(job_id, f"Trendyol toplu güncelleme ba�xlatıldı. {len(items)} ürün.")
    
    updates = []
    
    for item in items:
        # Trendyol payload structure
        payload = {
            'barcode': item['barcode'],
            'currencyType': 'TRY'
        }
        
        # Add stock if present
        if 'stock' in item:
            payload['quantity'] = int(item['stock'])
            
        # Add price if present
        if 'price' in item:
            p = float(item['price'])
            payload['salePrice'] = p
            payload['listPrice'] = p
            
        updates.append(payload)
        
    total_sent = 0
    # Batch send
    for idx, chunk in enumerate(chunked(updates, 100), start=1):
        try:
            client.update_price_inventory(chunk)
            total_sent += len(chunk)
            append_mp_job_log(job_id, f"Paket {idx}: {len(chunk)} ürün gönderildi.")
        except Exception as e:
            append_mp_job_log(job_id, f"Paket {idx} hatası: {e}", level='error')

    result = {
        'success': True,
        'updated_count': total_sent,
        'message': f'{total_sent} ürün için güncelleme iste�xi gönderildi.'
    }
    
    append_mp_job_log(job_id, "İ�xlem tamamlandı.")
    return result


def fetch_all_trendyol_inventory(user_id: int, job_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Fetch all products from Trendyol to get current inventory state.
    Returns: List of dicts {barcode: str, stock: int}
    """
    client = get_trendyol_client(user_id=user_id)
    all_remote = []
    page = 1
    size = 100
    
    if job_id:
        append_mp_job_log(job_id, "Trendyol üzerindeki tüm ürünleriniz çekiliyor...")

    while True:
        try:
            res = client.list_products(page=page, size=size)
            items = res.get('content', []) or res.get('items', [])
            if not items:
                break
            
            for item in items:
                all_remote.append({
                    'barcode': item.get('barcode'),
                    'stock': item.get('quantity', 0),
                    'on_sale': item.get('onSale', False)
                })
            
            if job_id:
                update_mp_job(job_id, progress={'current': len(all_remote), 'total': res.get('totalElements', len(all_remote)), 'message': f"Trendyol'dan {len(all_remote)} ürün alındı..."})

            if len(items) < size:
                break
            page += 1
            if page > 500: break # Safety limit
        except Exception as e:
            if job_id:
                append_mp_job_log(job_id, f"Trendyol envanter çekme hatası (Sayfa {page}): {e}", level='error')
            break
            
    return all_remote

def sync_trendyol_with_xml_diff(job_id: str, xml_source_id: Any, user_id: int = None, **kwargs) -> Dict[str, Any]:
    """
    Smart Sync: 
    1. Fetch ALL Trendyol products.
    2. Check which Trendyol products are NOT in the XML.
    3. Set stock to 0 for those missing products.
    4. Update existing products from XML.
    """
    startTime = time.time()
    append_mp_job_log(job_id, "Trendyol Akıllı Senkronizasyon (Diff Sync) başlatıldı.")
    
    # Updated to use Stock Code & Exclusion List
    
    # 1. Fetch Remote Inventory
    update_mp_job(job_id, progress={'current': 5, 'total': 100, 'message': 'Trendyol ürünleri çekiliyor...'})
    remote_items = fetch_all_trendyol_products(job_id=job_id, user_id=user_id)
    # Map STOCK CODE -> Item (Trendyol 'stockCode' is the vendor stock code)
    remote_stock_map = {}
    for item in remote_items:
        sc = item.get('stockCode')
        if sc:
            remote_stock_map[sc.strip()] = item
            
    remote_stock_codes = set(remote_stock_map.keys())
    append_mp_job_log(job_id, f"Trendyol hesabınızda {len(remote_stock_codes)} stok kodlu ürün bulundu.")

    # 2. Load XML
    update_mp_job(job_id, progress={'current': 20, 'total': 100, 'message': 'XML verisi analiz ediliyor...'})
    from app.services.xml_service import load_xml_source_index
    xml_index = load_xml_source_index(xml_source_id)
    # Use the new by_stock_code index
    xml_map = xml_index.get('by_stock_code') or {}
    xml_stock_codes = set(xml_map.keys()) # XML Stock Codes
    
    # Fallback map: by_barcode
    xml_barcode_map = xml_index.get('by_barcode') or {}
    
    append_mp_job_log(job_id, f"XML kaynağında {len(xml_stock_codes)} stok kodlu ürün bulundu.")

    # Load Exclusions
    from app.models.sync_exception import SyncException
    exclusions = SyncException.query.filter_by(user_id=user_id).all()
    excluded_values = {e.value.strip() for e in exclusions}
    if excluded_values:
        append_mp_job_log(job_id, f"⚠️ {len(excluded_values)} ürün 'Hariç Listesi'nde, işlem yapılmayacak.")

    # 3. Find Diff & Fallback Matching
    to_zero_stock_codes = []
    matched_stock_codes = []
    
    processed_remotes = set()
    
    for remote_sc in remote_stock_codes:
        if remote_sc in excluded_values:
            continue
            
        # Priority 1: Stock Code Match
        if remote_sc in xml_map:
            matched_stock_codes.append(remote_sc)
            processed_remotes.add(remote_sc)
            continue
            
        # Priority 2: Fallback Match (Stock Code matches XML Barcode)
        if remote_sc in xml_barcode_map:
            # Fallback match found!
            matched_stock_codes.append(remote_sc)
            processed_remotes.add(remote_sc)
            continue
            
        # If neither, it's a candidate for zeroing
        to_zero_stock_codes.append(remote_sc)
    
    append_mp_job_log(job_id, f"XML'de bulunmayan {len(to_zero_stock_codes)} ürün tespit edildi. Stokları 0 yapılacaktır.")

    # 4. Zero out missing products
    client = get_trendyol_client(user_id=user_id)
    zeroed_count = 0
    if to_zero_stock_codes:
        zero_payload = []
        for sc in to_zero_stock_codes:
            # We need the barcode for the update payload ideally, but fetch_all_trendyol_products items have 'barcode' too.
            # Let's retrieve barcode from our map
            item = remote_stock_map.get(sc)
            barcode = item.get('barcode') if item else sc # Fallback to sc if barcode missing (unlikely if fetched)
            
            zero_payload.append({
                'barcode': barcode,
                'quantity': 0,
                'currencyType': 'TRY'
            })
        
        # Batch send 0 stock updates
        total_to_zero = len(to_zero_stock_codes)
        for chunk in chunked(zero_payload, 100):
            try:
                update_mp_job(job_id, progress={
                    'current': zeroed_count,
                    'total': total_to_zero,
                    'message': f'Stok sıfırlanıyor: {zeroed_count}/{total_to_zero}'
                })
                client.update_price_inventory(chunk)
                zeroed_count += len(chunk)
                append_mp_job_log(job_id, f"✅ {zeroed_count}/{total_to_zero} ürün stoğu sıfırlandı.")
                time.sleep(1)
            except Exception as e:
                append_mp_job_log(job_id, f"Sıfırlama hatası (chunk): {e}", level='error')

    # 5. Lightweight Sync for Matched Products (Stock Code Match)
    matched_stock_codes = remote_stock_codes & xml_stock_codes
    
    # Filter Exclusions from Updates
    final_matched = []
    skipped_update_count = 0
    for sc in matched_stock_codes:
        if sc in excluded_values:
            skipped_update_count += 1
            continue
        final_matched.append(sc)

    append_mp_job_log(job_id, f"Eşleşen {len(final_matched)} ürün için fiyat/stok güncellemesi yapılıyor...")
    if skipped_update_count > 0:
        append_mp_job_log(job_id, f"🛡️ {skipped_update_count} ürün harici listede olduğu için GÜNCELLENMEDİ.")
    
    updated_count = 0
    if final_matched:
        items_to_update = []
        for sc in final_matched:
            # Try Primary Match
            xml_info = xml_map.get(sc)
            
            # Try Fallback Match
            if not xml_info:
                xml_info = xml_barcode_map.get(sc)
            
            if not xml_info: continue
            
            # Stock
            qty = to_int(xml_info.get('quantity'), 0)
            
            # Price
            base_price = to_float(xml_info.get('price'), 0.0)
            final_price = calculate_price(base_price, 'trendyol', user_id=user_id) # Using trendyol specific price logic
            
            # Get Remote Barcode (for API call)
            # Trendyol API needs "barcode" to update
            remote_item = remote_stock_map.get(sc)
            barcode = remote_item.get('barcode') if remote_item else sc
            
            items_to_update.append({
                'barcode': barcode,
                'quantity': qty,
                'salePrice': final_price,
                'listPrice': final_price,
                'currencyType': 'TRY'
            })
            
        # Batch send updates
        total_to_update = len(final_matched)
        for chunk in chunked(items_to_update, 100):
            try:
                update_mp_job(job_id, progress={
                    'current': updated_count,
                    'total': total_to_update,
                    'message': f'Fiyat/Stok güncelleniyor: {updated_count}/{total_to_update}'
                })
                client.update_price_inventory(chunk)
                updated_count += len(chunk)
                append_mp_job_log(job_id, f"✅ {updated_count}/{total_to_update} eşleşen ürün güncellendi.")
                time.sleep(0.5)
            except Exception as e:
                append_mp_job_log(job_id, f"Güncelleme hatası (chunk): {e}", level='error')

    sync_res = {
        'success': True,
        'updated_count': updated_count,
        'zeroed_count': zeroed_count,
        'total_xml': len(xml_stock_codes),
        'total_remote': len(remote_stock_codes)
    }
    
    sync_res['zeroed_count'] = zeroed_count
    totalTime = time.time() - startTime
    append_mp_job_log(job_id, f"Akıllı senkronizasyon tamamlandı. (Süre: {totalTime:.1f}s)")
    
    return sync_res

def perform_trendyol_sync_all(job_id: str, xml_source_id: Any, match_by: str = 'barcode', user_id: int = None) -> Dict[str, Any]:
    """
    Main entry point for auto-sync for Trendyol.
    Now uses the Diff Sync logic.
    """
    return sync_trendyol_with_xml_diff(job_id, xml_source_id, user_id=user_id)


def perform_trendyol_direct_push_actions(user_id: int, to_update: List[Any], to_create: List[Any], to_zero: List[Any], src: Any, job_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Trendyol için Direct Push aksiyonlarını gerçekleştirir.
    """
    from app.services.job_queue import append_mp_job_log, get_mp_job, update_mp_job
    from app.utils.helpers import calculate_price
    from app.models import MarketplaceProduct, Setting
    from app import db
    
    client = get_trendyol_client(user_id=user_id)
    res = {'updated_count': 0, 'created_count': 0, 'zeroed_count': 0}
    
    total_ops = len(to_update or []) + len(to_create or []) + len(to_zero or [])
    completed_ops = 0
    
    if job_id:
        update_mp_job(job_id, progress={'current': 0, 'total': total_ops, 'message': 'İşlemler başlatılıyor...'})

    # Ensure TF-IDF is ready for matching (required for to_create items)
    if to_create:
        ensure_brand_tfidf_ready()
        ensure_tfidf_ready()
    
    # --- 1. GÜNCELLEMELER (Update) ---
    if to_update:
        if job_id: update_mp_job(job_id, progress={'message': f'Güncellemeler hazırlanıyor ({len(to_update)} ürün)...'})
        update_items = []
        for xml_item, local_item in to_update:
            if job_id:
                js = get_mp_job(job_id)
                if js and js.get('cancel_requested'):
                    append_mp_job_log(job_id, "İşlem kullanıcı tarafından iptal edildi.", level='warning')
                    return res
            final_price = calculate_price(xml_item.price, 'trendyol', user_id=user_id)
            update_items.append({
                'barcode': local_item.barcode,
                'quantity': xml_item.quantity,
                'listPrice': final_price,
                'salePrice': final_price
            })
            
            if job_id: append_mp_job_log(job_id, f"Güncelleniyor: {xml_item.stock_code} (Stok: {local_item.quantity} -> {xml_item.quantity})")
            
            local_item.quantity = xml_item.quantity
            local_item.sale_price = final_price
            local_item.last_sync_at = datetime.now()

        try:
            for batch in chunked(update_items, 100):
                if job_id:
                    js = get_mp_job(job_id)
                    if js and js.get('cancel_requested'):
                        append_mp_job_log(job_id, "İşlem kullanıcı tarafından iptal edildi.", level='warning')
                        return res
                
                client.update_price_inventory(batch)
                res['updated_count'] += len(batch)
                
                completed_ops += len(batch)
                if job_id:
                    update_mp_job(job_id, progress={
                        'current': completed_ops,
                        'total': total_ops,
                        'message': f"Güncelleniyor ({completed_ops}/{total_ops})..."
                    })
            db.session.commit()
        except Exception as e:
            if job_id: append_mp_job_log(job_id, f"Trendyol güncelleme hatası: {str(e)}", level='error')

    # --- 2. YENİ ÜRÜNLER (Create) ---
    if to_create:
        if job_id: update_mp_job(job_id, progress={'message': f'Yeni ürünler hazırlanıyor ({len(to_create)} ürün)...'})
        from app.services.xml_service import generate_random_barcode
        create_items = []
        
        # Get default brand from settings
        default_brand_id = 0
        settings_brand_id = Setting.get("TRENDYOL_BRAND_ID", user_id=user_id)
        if settings_brand_id and settings_brand_id.strip():
            try: default_brand_id = int(settings_brand_id)
            except: pass

        for xml_item in to_create:
            if job_id:
                js = get_mp_job(job_id)
                if js and js.get('cancel_requested'):
                    append_mp_job_log(job_id, "İşlem kullanıcı tarafından iptal edildi.", level='warning')
                    return res
            barcode = xml_item.barcode

            # Check random barcode settings (Global overrides from Auto Sync Menu)
            use_random_setting = Setting.get(f'AUTO_SYNC_USE_RANDOM_BARCODE_trendyol', user_id=user_id) == 'true'
            use_override_setting = Setting.get(f'AUTO_SYNC_USE_OVERRIDE_BARCODE_trendyol', user_id=user_id) == 'true'

            if use_override_setting or (not barcode and (src.use_random_barcode or use_random_setting)):
                barcode = generate_random_barcode()
            
            raw = json.loads(xml_item.raw_data)
            
            # --- Marka ve Kategori Çözümü ---
            # 1. Marka: Ayarlar (Varsayılan) -> XML (Eşleştirme)
            brand_id = default_brand_id
            if not brand_id:
                brand_id = match_brand_id_for_name_tfidf(raw.get('brand'))
            
            # 2. Kategori: XML Ürün Başlığı (Eşleştirme)
            cat_id = match_category_id_for_title_tfidf(raw.get('title'))
            
            if not brand_id or not cat_id:
                reason = "Marka bulunamadı" if not brand_id else "Kategori bulunamadı"
                if job_id: append_mp_job_log(job_id, f"Atlandı ({reason}): {xml_item.stock_code}", level='warning')
                continue

            final_price = calculate_price(xml_item.price, 'trendyol', user_id=user_id)
            
            # Title Validation (Trendyol Limit: 3-100 chars)
            safe_title = (xml_item.title or "").strip()
            if len(safe_title) < 3:
                safe_title = f"{safe_title} - Ürün" if safe_title else f"Ürün - {barcode}"
            if len(safe_title) > 100:
                safe_title = safe_title[:100]

            item = {
                "barcode": barcode,
                "title": safe_title,
                "productMainId": raw.get('modelCode') or raw.get('parent_barcode') or xml_item.stock_code,
                "brandId": int(brand_id),
                "categoryId": int(cat_id),
                "quantity": xml_item.quantity,
                "stockCode": xml_item.stock_code,
                "description": raw.get('details') or raw.get('description') or xml_item.title,
                "currencyType": "TRY",
                "listPrice": final_price,
                "salePrice": final_price,
                "vatRate": int(raw.get('vatRate', 20)),
                "cargoCompanyId": int(Setting.get("TRENDYOL_CARGO_ID", "1", user_id=user_id)),
                "images": [{"url": img['url']} for img in raw.get('images', []) if img.get('url')],
                "attributes": [] # TODO: Zorunlu nitelikler
            }
            create_items.append((item, xml_item))
            if job_id: append_mp_job_log(job_id, f"Yeni Ürün Yükleniyor: {xml_item.stock_code} ({xml_item.title[:30]}...)")

        if create_items:
            try:
                for batch in chunked(create_items, 50):
                    if job_id:
                        js = get_mp_job(job_id)
                        if js and js.get('cancel_requested'):
                            append_mp_job_log(job_id, "İşlem kullanıcı tarafından iptal edildi.", level='warning')
                            return res

                    payloads = [x[0] for x in batch]
                    client.create_products(payloads)
                    for item_payload, xml_record in batch:
                        existing = MarketplaceProduct.query.filter_by(user_id=user_id, marketplace='trendyol', barcode=item_payload['barcode']).first()
                        if not existing:
                            new_mp = MarketplaceProduct(
                                user_id=user_id, marketplace='trendyol', barcode=item_payload['barcode'],
                                stock_code=xml_record.stock_code, title=xml_record.title,
                                price=item_payload['listPrice'], sale_price=item_payload['salePrice'],
                                quantity=xml_record.quantity, status='Pending', on_sale=True,
                                xml_source_id=src.id
                            )
                            db.session.add(new_mp)
                    db.session.commit()
                    res['created_count'] += len(batch)
                    
                    completed_ops += len(batch)
                    if job_id:
                        update_mp_job(job_id, progress={
                            'current': completed_ops,
                            'total': total_ops,
                            'message': f"Yeni Ürünler Ekleniyor ({completed_ops}/{total_ops})..."
                        })

            except Exception as e:
                if job_id: append_mp_job_log(job_id, f"Trendyol yükleme hatası: {str(e)}", level='error')

    # --- 3. STOK SIFIRLAMA (Zero) ---
    if to_zero:
        if job_id: update_mp_job(job_id, progress={'message': f'Stok sıfırlama hazırlanıyor ({len(to_zero)} ürün)...'})
        zero_items = []
        for local_item in to_zero:
            if job_id:
                js = get_mp_job(job_id)
                if js and js.get('cancel_requested'):
                    append_mp_job_log(job_id, "İşlem kullanıcı tarafından iptal edildi.", level='warning')
                    return res
            zero_items.append({
                'barcode': local_item.barcode,
                'quantity': 0,
                'listPrice': local_item.sale_price,
                'salePrice': local_item.sale_price
            })
            if job_id: append_mp_job_log(job_id, f"Stok Sıfırlanıyor (XML'de yok): {local_item.stock_code}")
            local_item.quantity = 0

        try:
            for batch in chunked(zero_items, 100):
                if job_id:
                    js = get_mp_job(job_id)
                    if js and js.get('cancel_requested'):
                        append_mp_job_log(job_id, "İşlem kullanıcı tarafından iptal edildi.", level='warning')
                        return res
                client.update_price_inventory(batch)
                res['zeroed_count'] += len(batch)
                
                completed_ops += len(batch)
                if job_id:
                    update_mp_job(job_id, progress={
                        'current': completed_ops,
                        'total': total_ops,
                        'message': f"Stoklar Sıfırlanıyor ({completed_ops}/{total_ops})..."
                    })
            db.session.commit()
        except Exception as e:
            if job_id: append_mp_job_log(job_id, f"Trendyol stok sıfırlama hatası: {str(e)}", level='error')

    return res


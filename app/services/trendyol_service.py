import time
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
from app.utils.helpers import to_int, to_float, chunked, get_marketplace_multiplier, clean_forbidden_words

# User-specific caches for Trendyol
_TRENDYOL_USER_CACHES: Dict[int, Dict[str, Any]] = {}

def get_trendyol_cache(user_id: int) -> Dict[str, Any]:
    if user_id not in _TRENDYOL_USER_CACHES:
        _TRENDYOL_USER_CACHES[user_id] = {
            "cat_tfidf": {"leaf": [], "names": [], "vectorizer": None, "matrix": None},
            "brand_tfidf": {"leaf": [], "names": [], "vectorizer": None, "matrix": None},
            "brand_cache": {"by_name": {}, "count": 0, "loaded": False},
            "category_cache": {"by_name": {}, "by_id": {}, "list": [], "count": 0, "loaded": False}
        }
    return _TRENDYOL_USER_CACHES[user_id]

def load_brand_cache_from_db(user_id: int) -> bool:
    """Load brand cache from database (Setting) for specific user."""
    try:
        cache = get_trendyol_cache(user_id)["brand_cache"]
        cached_json = Setting.get("TRENDYOL_BRAND_CACHE", "", user_id=user_id)
        if cached_json:
            logging.info(f"Loading brand cache from DB for user {user_id}, data size: {len(cached_json)} chars")
            data = json.loads(cached_json)
            cache["by_name"] = {k.lower(): v for k, v in data.get("by_name", {}).items()}
            cache["count"] = data.get("count", 0)
            cache["loaded"] = True
            logging.info(f"Brand cache loaded for user {user_id}: {cache['count']} brands")
            return True
        else:
            logging.warning(f"Brand cache is empty for user {user_id}. Run 'Markaları Çek' first.")
    except Exception as e:
        logging.exception(f"Failed to load brand cache for user {user_id}: {e}")
    return False

def save_brand_cache_to_db(user_id: int) -> bool:
    """Save brand cache to database for specific user."""
    try:
        cache = get_trendyol_cache(user_id)["brand_cache"]
        data = {
            "by_name": cache["by_name"],
            "count": cache["count"]
        }
        json_data = json.dumps(data, ensure_ascii=False)
        logging.info(f"Saving brand cache to DB for user {user_id}: {cache['count']} brands, {len(json_data)} chars")
        Setting.set("TRENDYOL_BRAND_CACHE", json_data, user_id=user_id)
        logging.info(f"Brand cache saved successfully for user {user_id}")
        return True
    except Exception as e:
        logging.exception(f"Failed to save brand cache for user {user_id}: {e}")
        return False

def fetch_and_cache_brands(user_id: int) -> Dict[str, Any]:
    """Fetch all brands from Trendyol API and cache them - USER ISOLATED."""
    try:
        from app.services.trendyol_service import get_trendyol_client
        client = get_trendyol_client(user_id=user_id)
        result = {"success": False, "count": 0, "message": ""}
        
        page = 0
        all_brands = []
        max_pages = 100  # Safety limit
        
        while page < max_pages:
            resp = client.get_all_brands(page=page, size=1500)
            
            # Debug: Log response structure for first page
            if page == 0:
                logging.info(f"Brand API response type for user {user_id}: {type(resp)}")
            
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
            
            logging.info(f"Brand fetch page {page} for user {user_id}: got {len(brands)} brands")
            
            if not brands:
                break
                
            all_brands.extend(brands)
            
            if len(brands) < 1500:
                break
            page += 1
        
        logging.info(f"Total brands fetched for user {user_id}: {len(all_brands)}")
        
        # Build cache
        cache = get_trendyol_cache(user_id)
        cache["brand_cache"]["by_name"] = {}
        for b in all_brands:
            name = b.get("name", "").strip()
            brand_id = b.get("id")
            if name and brand_id:
                cache["brand_cache"]["by_name"][name.lower()] = {"id": brand_id, "name": name}
        
        cache["brand_cache"]["count"] = len(cache["brand_cache"]["by_name"])
        cache["brand_cache"]["loaded"] = True
        
        # Save to database
        saved = save_brand_cache_to_db(user_id)
        
        result["success"] = True
        result["count"] = cache["brand_cache"]["count"]
        result["message"] = f"{cache['brand_cache']['count']} marka başarıyla çekildi ve kaydedildi." if saved else f"{cache['brand_cache']['count']} marka çekildi ama kaydetme başarısız!"
        
        return result
        
    except Exception as e:
        logging.exception(f"Failed to fetch brands for user {user_id}")
        return {"success": False, "count": 0, "message": str(e)}


def save_category_cache_to_db(user_id: int) -> bool:
    """Save category cache to database - USER ISOLATED."""
    try:
        cache = get_trendyol_cache(user_id)["category_cache"]
        cache_data = {
            "by_name": cache.get("by_name", {}),
            "by_id": cache.get("by_id", {}),
            "list": cache.get("list", []),
            "count": cache.get("count", 0)
        }
        Setting.set('TRENDYOL_CATEGORY_CACHE', json.dumps(cache_data), user_id=user_id)
        logging.info(f"Saved category cache to DB for user {user_id}: {cache_data['count']} categories")
        return True
    except Exception as e:
        logging.exception(f"Failed to save category cache for user {user_id}: {e}")
        return False


def load_category_cache_from_db(user_id: int) -> bool:
    """Load category cache from database - USER ISOLATED."""
    try:
        cache_json = Setting.get('TRENDYOL_CATEGORY_CACHE', '', user_id=user_id)
        if cache_json:
            cache_data = json.loads(cache_json)
            cache = get_trendyol_cache(user_id)["category_cache"]
            cache.update(cache_data)
            cache["loaded"] = True
            logging.info(f"Loaded category cache from DB for user {user_id}: {cache_data.get('count', 0)} categories")
            return True
    except Exception as e:
        logging.exception(f"Failed to load category cache for user {user_id}: {e}")
    return False


def fetch_and_cache_categories(user_id: int) -> Dict[str, Any]:
    """Fetch all categories from Trendyol API and cache them - USER ISOLATED."""
    try:
        from app.services.trendyol_service import get_trendyol_client
        client = get_trendyol_client(user_id=user_id)
        result = {"success": False, "count": 0, "message": ""}
        
        # Get category tree
        logging.info(f"Fetching Trendyol category tree for user {user_id}...")
        tree = client.get_category_tree()
        
        categories = tree.get("categories", [])
        logging.info(f"Got {len(categories)} root categories for user {user_id}")
        
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
        
        logging.info(f"Flattened to {len(flat_categories)} total categories for user {user_id}")
        
        # Build cache
        cache = get_trendyol_cache(user_id)
        cache["category_cache"]["by_name"] = {}
        cache["category_cache"]["by_id"] = {}
        cache["category_cache"]["list"] = flat_categories
        
        for cat in flat_categories:
            name_key = cat["name"].lower().strip()
            path_key = cat["path"].lower().strip()
            
            # Store by name and by full path
            cache["category_cache"]["by_name"][name_key] = {"id": cat["id"], "name": cat["name"], "path": cat["path"]}
            cache["category_cache"]["by_name"][path_key] = {"id": cat["id"], "name": cat["name"], "path": cat["path"]}
            cache["category_cache"]["by_id"][cat["id"]] = {"name": cat["name"], "path": cat["path"]}
        
        cache["category_cache"]["count"] = len(flat_categories)
        cache["category_cache"]["loaded"] = True
        
        # Save to database
        saved = save_category_cache_to_db(user_id)
        
        result["success"] = True
        result["count"] = cache["category_cache"]["count"]
        result["message"] = f"{cache['category_cache']['count']} kategori başarıyla çekildi ve kaydedildi." if saved else f"{cache['category_cache']['count']} kategori çekildi ama kaydetme başarısız!"
        
        return result
        
    except Exception as e:
        logging.exception(f"Failed to fetch categories for user {user_id}")
        return {"success": False, "count": 0, "message": str(e)}


def get_cached_category_id(category_name: str, user_id: int, default_id: int = 0) -> int:
    """Get category ID from cache - USER ISOLATED."""
    cache = get_trendyol_cache(user_id)["category_cache"]
    if not cache.get("loaded"):
        load_category_cache_from_db(user_id)
    
    if not category_name:
        return default_id
    
    # Try exact match first
    key = category_name.lower().strip()
    cached = cache["by_name"].get(key)
    if cached:
        return cached["id"]
    
    # Try partial match
    for cache_key, cache_val in cache["by_name"].items():
        if key in cache_key or cache_key in key:
            return cache_val["id"]
    
    return default_id


def get_category_cache_stats(user_id: int) -> Dict[str, Any]:
    """Get category cache statistics - USER ISOLATED."""
    cache = get_trendyol_cache(user_id)["category_cache"]
    if not cache.get("loaded"):
        load_category_cache_from_db(user_id)
    return {
        "loaded": cache.get("loaded", False),
        "count": cache.get("count", 0)
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

def _refresh_trendyol_normalized_cache(user_id: int):
    """Refreshes the normalized brand cache for a specific user."""
    cache_main = get_trendyol_cache(user_id)
    brand_cache = cache_main["brand_cache"]
    norm_cache = cache_main["norm_brand"]
    
    if not brand_cache.get("loaded"):
        load_brand_cache_from_db(user_id)
    
    # Only rebuild if brand cache count changed or not loaded
    current_count = len(brand_cache.get("by_name", {}))
    if norm_cache["loaded"] and len(norm_cache["list"]) == current_count:
        return

    logging.info(f"Building normalized brand cache for user {user_id} ({current_count} brands)...")
    norm_list = []
    norm_map = {}
    for key in brand_cache.get("by_name", {}).keys():
        n = normalize_brand_name(key)
        norm_list.append(n)
        norm_map[n] = key
    
    norm_cache["list"] = norm_list
    norm_cache["map"] = norm_map
    norm_cache["loaded"] = True
    logging.info(f"Normalized brand cache built for user {user_id}.")

def match_brand_from_cache(brand_name: str, user_id: int) -> Optional[Dict[str, Any]]:
    """
    Find brand in cache using LEGACY logic (Exhaustive Search) - USER ISOLATED.
    """
    cache_main = get_trendyol_cache(user_id)
    brand_cache = cache_main["brand_cache"]
    
    if not brand_cache.get("loaded"):
        load_brand_cache_from_db(user_id)

    if not brand_cache.get("by_name"):
        logging.warning(f"Brand cache is EMPTY for user {user_id}! Matching will fail.")
        return None
    
    if not brand_name:
        return None
    
    # 1. Exact match (fastest)
    key = brand_name.lower().strip()
    cached = brand_cache["by_name"].get(key)
    if cached:
        return cached
    
    # Prepare normalized search
    normalized_search = normalize_brand_name(brand_name)
    search_words = set(normalized_search.split())
    
    # Iterate ALL brands (Legacy Style)
    for cache_key, cache_val in brand_cache["by_name"].items():
        normalized_cache = normalize_brand_name(cache_val["name"])
        
        # 2. Exact Normalized
        if normalized_search == normalized_cache:
            return cache_val
            
        # 3. Containment
        if normalized_search in normalized_cache or normalized_cache in normalized_search:
            if len(normalized_search) >= 3 and len(normalized_cache) >= 3:
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

def get_cached_brand_id(brand_name: str, user_id: int, default_id: int = 2770299) -> int:
    """Get brand ID from cache with legacy matching - USER ISOLATED."""
    match = match_brand_from_cache(brand_name, user_id=user_id)
    return match["id"] if match else default_id

def get_brand_cache_stats(user_id: int) -> Dict[str, Any]:
    """Get brand cache statistics - USER ISOLATED."""
    cache = get_trendyol_cache(user_id)["brand_cache"]
    if not cache.get("loaded"):
        load_brand_cache_from_db(user_id)
    return {
        "loaded": cache.get("loaded", False),
        "count": cache.get("count", 0)
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

def prepare_tfidf(user_id: int, leaf_categories: List[Dict[str, Any]]):
    """Builds TF-IDF for categories for specific user - USER ISOLATED."""
    names = [c.get('name','') for c in leaf_categories]
    cache = get_trendyol_cache(user_id)
    if not names:
        cache["cat_tfidf"].update({"leaf": [], "names": [], "vectorizer": None, "matrix": None})
        return
    vec = TfidfVectorizer(analyzer='char_wb', ngram_range=(2,4))
    vec.fit(names)
    mat = vec.transform(names)
    cache["cat_tfidf"].update({"leaf": leaf_categories, "names": names, "vectorizer": vec, "matrix": mat})

def match_category_id_for_title_tfidf(title: str, user_id: int) -> int:
    """Match category using TF-IDF for specific user - USER ISOLATED."""
    cache = get_trendyol_cache(user_id)["cat_tfidf"]
    if not title or not cache.get('vectorizer'):
        return 0
    vec = cache['vectorizer']
    mat = cache['matrix']
    names = cache['names']
    leaf = cache['leaf']
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

def prepare_brand_tfidf(user_id: int):
    """Builds TF-IDF matrix for brands from cache - USER ISOLATED."""
    cache = get_trendyol_cache(user_id)
    brand_cache = cache["brand_cache"]
    if not brand_cache.get("loaded"):
        load_brand_cache_from_db(user_id)
    
    # Extract brands list
    brands = [] 
    for name, data in brand_cache.get("by_name", {}).items():
        brands.append({"id": data["id"], "name": data.get("name", name)})
        
    names = [b.get('name','') for b in brands]
    
    if not names:
        cache["brand_tfidf"].update({"leaf": [], "names": [], "vectorizer": None, "matrix": None})
        return

    logging.info(f"Building Brand TF-IDF for user {user_id} ({len(names)} brands)...")
    vec = TfidfVectorizer(analyzer='char_wb', ngram_range=(2, 4))
    vec.fit(names)
    mat = vec.transform(names)
    
    cache["brand_tfidf"].update({"leaf": brands, "names": names, "vectorizer": vec, "matrix": mat})
    logging.info(f"Brand TF-IDF built for user {user_id}.")

def ensure_brand_tfidf_ready(user_id: int):
    """Ensures Brand TF-IDF is built if cache is loaded - USER ISOLATED."""
    cache = get_trendyol_cache(user_id)
    if not cache["brand_tfidf"].get("vectorizer"):
        prepare_brand_tfidf(user_id)

def match_brand_id_for_name_tfidf(name: str, user_id: int) -> Optional[Dict[str, Any]]:
    """Match single brand name using TF-IDF - USER ISOLATED."""
    cache = get_trendyol_cache(user_id)["brand_tfidf"]
    if not name or not cache.get('vectorizer'):
        return None
        
    vec = cache['vectorizer']
    mat = cache['matrix']
    leaf = cache['leaf']
    
    try:
        q = vec.transform([name])
        sims = cosine_similarity(q, mat) 
        row = sims[0]
        idx = int(row.argmax())
        score = float(row[idx])
        
        if score >= 0.45: 
            return leaf[idx]
        return None
    except Exception:
        return None

def match_brands_tfidf_batch(names: list[str], user_id: int) -> Dict[str, Optional[Dict[str, Any]]]:
    """
    Match multiple brands at once using vectorized TF-IDF - USER ISOLATED.
    """
    results = {name: None for name in names}
    cache = get_trendyol_cache(user_id)["brand_tfidf"]
    if not names or not cache.get('vectorizer'):
        return results

    vec = cache['vectorizer']
    mat = cache['matrix']
    leaf = cache['leaf']
    
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

def fetch_all_trendyol_products(job_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch ALL products from Trendyol API with pagination."""
    from app.services.job_queue import update_mp_job, get_mp_job
    
    client = get_trendyol_client()
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
    """Fetch all products and sync to MarketplaceProduct table - USER ISOLATED."""
    try:
        from app.models import MarketplaceProduct
        from app import db
        from flask_login import current_user
        
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

        items = fetch_all_trendyol_products(job_id)
        
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
                # Logic: If onSale=True -> Active, else Passive?
                # Also check 'rejected'.
                status_str = "Active" if on_sale else "Passive"
                approval_str = "Approved" if approved else ("Rejected" if item.get('rejected') else "Pending")
                if not on_sale and not approved:
                    status_str = "Archived" # Example
                
                # Price/Qty
                list_price = float(item.get('listPrice', 0))
                sale_price = float(item.get('salePrice', 0))
                quantity = int(item.get('stock', 0) if 'stock' in item else item.get('quantity', 0))
                
                images = item.get('images', [])
                img_json = json.dumps([img['url'] for img in images if isinstance(img, dict) and 'url' in img])
                
                # Check existing
                existing = MarketplaceProduct.query.filter_by(
                    marketplace='trendyol', 
                    barcode=barcode
                ).filter(
                    (MarketplaceProduct.user_id == user_id) if user_id else True
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
    u_id = user_id or (current_user.id if current_user and current_user.is_authenticated else None)
    client = get_trendyol_client(user_id=u_id)
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
    u_id = user_id or (current_user.id if current_user and current_user.is_authenticated else None)
    client = get_trendyol_client(user_id=u_id)
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
            
        price = round(base_price * multiplier, 2)
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


def perform_trendyol_sync_all(job_id: str, xml_source_id: Any, match_by: str = 'barcode', user_id: int = None) -> Dict[str, Any]:
    """
    Trendyol için hem stok hem fiyat eşitleme (birleşik)
    Trendyol'da aslında update_price_inventory tek çağrıda hem stok hem fiyat günceller,
    bu yüzden tekli sync fonksiyonları zaten her ikisini de yapıyor.
    Bu fonksiyon sadece UI tutarlılığı için.
    """
    append_mp_job_log(job_id, "Stok ve fiyat eşitleme başlatılıyor...")
    
    # Trendyol uses same endpoint for both, so just call sync_prices which includes quantity
    append_mp_job_log(job_id, ">>> STOK VE FİYAT EŞITLEME BAŞLADI <<<")
    result = {}
    try:
        result = perform_trendyol_sync_prices(job_id, xml_source_id, match_by=match_by, user_id=user_id)
        append_mp_job_log(job_id, f"Eşitleme tamamlandı: {result.get('updated_count', 0)} güncellendi")
    except Exception as e:
        append_mp_job_log(job_id, f"Eşitleme hatası: {str(e)}", level='error')
        result = {'success': False, 'error': str(e), 'updated_count': 0}
    
    # Add combined info to result
    result['message'] = f"Stok ve fiyat: {result.get('updated_count', 0)} ürün güncellendi"
    
    append_mp_job_log(job_id, "Stok ve fiyat eşitleme tamamlandı.")
    return result


def ensure_tfidf_ready():
    if _CAT_TFIDF.get('vectorizer'):
        return
    u_id = None
    from flask_login import current_user
    if current_user and current_user.is_authenticated:
        u_id = current_user.id
        
    raw = Setting.get("TRENDYOL_CATEGORY_TREE", "", user_id=u_id)
    if raw:
        try:
            leafs = json.loads(raw)
            prepare_tfidf(leafs)
        except Exception:
            pass

def perform_trendyol_send_products(job_id: str, barcodes: List[str], xml_source_id: Any = None, auto_match: bool = False, send_options: Dict[str, Any] = None, match_by: str = 'barcode', title_prefix: str = None, is_manual: bool = False) -> Dict[str, Any]:
    client = get_trendyol_client()
    append_mp_job_log(job_id, "Trendyol istemcisi başlatıldı.")
    
    # Resolve User ID
    user_id = None
    try:
        from app.services.job_queue import get_mp_job
        job_data = get_mp_job(job_id)
        user_id = job_data.get('params', {}).get('_user_id')
    except:
        pass

    if not user_id and xml_source_id:
        try:
             # Handle "excel:123" or just "123"
             s_id = str(xml_source_id)
             if ':' in s_id and s_id.startswith('excel'):
                 pass 
             elif s_id.isdigit():
                 src = SupplierXML.query.get(int(s_id))
                 if src:
                     user_id = src.user_id
        except Exception as e:
             logging.warning(f"Failed to resolve user_id from xml_source_id {xml_source_id}: {e}")

    # Pre-fetch Global Default Brand ID to ensure proper fallback
    global_default_brand = 0
    try:
        from app.models import Setting
        val = Setting.get('TRENDYOL_BRAND_ID', '', user_id=user_id)
        if val and str(val).isdigit():
            global_default_brand = int(val)
            append_mp_job_log(job_id, f"Varsayılan Marka ID aktif: {global_default_brand}")
        else:
            append_mp_job_log(job_id, "⚠️ Varsayılan Marka ID ayarlanmamış!", level='warning')
    except Exception as e:
        logging.warning(f"Error pre-fetching default brand: {e}")
    
    # Extract send options
    if send_options is None:
        send_options = {}
    zero_stock_as_one = send_options.get('zero_stock_as_one', False)
    skip_no_image = send_options.get('skip_no_image', False)
    apply_multiplier = send_options.get('apply_multiplier', False)
    skip_no_barcode = send_options.get('skip_no_barcode', False)
    default_price = send_options.get('default_price', 0)
    
    append_mp_job_log(job_id, f"Seçenekler: Stok0→1={zero_stock_as_one}, Görselsiz atla={skip_no_image}, Çarpan={apply_multiplier}, Varsayılan fiyat={default_price}")
    
    mp_map = {}
    if is_manual:
        append_mp_job_log(job_id, "Manuel ürün gönderimi aktif, veritabanından okunuyor...")
        from app.models.product import Product
        prods = Product.query.filter(Product.barcode.in_(barcodes), Product.user_id == user_id).all()
        for p in prods:
            # Map model to dict compatible with the sync logic
            mp_map[p.barcode] = {
                'barcode': p.barcode,
                'title': p.title,
                'description': p.description,
                'price': p.listPrice,
                'quantity': p.quantity,
                'stockCode': p.stockCode,
                'brand': p.brand,
                'category': p.top_category,
                'images': p.get_images,
                'marketplace_category_id': p.marketplace_category_id,
                'marketplace_attributes_json': p.marketplace_attributes_json,
                'is_manual': True
            }
    else:
        xml_index = load_xml_source_index(xml_source_id)
        mp_map = xml_index.get('by_barcode') or {}
    
    # Only apply multiplier if option is enabled
    multiplier = get_marketplace_multiplier('trendyol') if apply_multiplier else 1.0
    
    # Debug logging
    append_mp_job_log(job_id, f"Kaynak tipi: {str(xml_source_id)[:20]}...")
    append_mp_job_log(job_id, f"Yüklenen ürün sayısı: {len(mp_map)}")
    if barcodes and len(barcodes) > 0:
        first_barcode = barcodes[0]
        found = mp_map.get(first_barcode)
        append_mp_job_log(job_id, f"İlk barkod ({first_barcode[:15]}): {'BULUNDU' if found else 'BULUNAMADI'}")
    
    if auto_match:
        append_mp_job_log(job_id, "Kategori ağacı yükleniyor...")
        ensure_tfidf_ready(target_user_id)
        cache = get_trendyol_cache(target_user_id)["cat_tfidf"]
        if not cache.get('vectorizer'):
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

    def resolve_brand_id(brand_name: str, user_id: int = None) -> int:
        """Resolve brand name to Trendyol brand ID using Trendyol API directly - USER ISOLATED."""
        
        # Get target user_id
        u_id = user_id or (current_user.id if current_user and current_user.is_authenticated else None)

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
        u_id = user_id or (current_user.id if current_user and current_user.is_authenticated else None)
        sm_brand_id, sm_brand_name = SmartMatchService.get_brand_match(brand_name, 'trendyol', user_id=u_id)
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

    def build_simple_attributes(category_id: int) -> List[dict]:
        """Build minimal required attributes for a category"""
        try:
            attrs = client.get_category_attributes(category_id)
            payload = []
            
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
                default_value = DEFAULT_ATTRS.get(attr_name, "Bilinmiyor")
                
                if attr_values and not allow_custom:
                    # Use first available value for required attributes
                    item["attributeValueId"] = attr_values[0]['id']
                elif allow_custom:
                    item["customAttributeValue"] = str(default_value)
                else:
                    continue
                
                payload.append(item)
            
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
            
        title = clean_forbidden_words(product.get('title', ''))
        if title_prefix:
             title = f"{title_prefix} {title}"
        desc = clean_forbidden_words(product.get('description', '') or title)
        brand_name = product.get('brand', '') or product.get('vendor', '')
        
        # Debug: Log first few titles to verify prefix
        if processed <= 3:
            append_mp_job_log(job_id, f"DEBUG Title for {barcode}: '{title[:80]}...'")
        
        # Log original brand from Excel
        if brand_name:
            append_mp_job_log(job_id, f"Excel marka: '{brand_name}' - barcode: {barcode}")
        
        # Brand Override Logic
        if global_default_brand > 0:
            brand_id = global_default_brand
        else:
            # First check for pre-resolved brand_id from Excel index
            brand_id = product.get('brand_id') or product.get('brandId') or 0
            
            # If not pre-resolved, use resolve_brand_id (handles empty brand with fallback)
            if not brand_id:
                brand_id = resolve_brand_id(brand_name, user_id=target_user_id)
        
        if not brand_id:
            skipped.append({'barcode': barcode, 'reason': f'Marka bulunamadı: {brand_name or "boş"} (Ayarlarda varsayılan marka tanımlayın)'})
            continue

        # Resolve Category ID
        category_id = 0
        excel_category = product.get('category', '') or ''
        
        # 0. Check for manual marketplace category
        if product.get('marketplace_category_id'):
            try:
                category_id = int(product.get('marketplace_category_id'))
                append_mp_job_log(job_id, f"Manuel kategori seçimi kullanılıyor: {category_id}")
            except: pass

        if not category_id:
            # 1. First check for pre-resolved category_id from Excel index
            category_id = product.get('category_id') or product.get('categoryId') or 0
        
        # 2. If not pre-resolved, try Smart Match DB (Confirmed Mappings)
        if not category_id and excel_category:
            from app.services.smart_match_service import SmartMatchService
            u_id = user_id or (current_user.id if current_user and current_user.is_authenticated else None)
            sm_cat_id, sm_cat_path = SmartMatchService.get_category_match(excel_category, 'trendyol', user_id=u_id)
            if sm_cat_id:
                category_id = sm_cat_id
                append_mp_job_log(job_id, f"Kategori DB Eşleşmesi: '{excel_category}' -> {category_id} ({sm_cat_path})")
        
        # 3. If not in DB, try exact/partial cache match
        if not category_id and excel_category:
            category_id = get_cached_category_id(excel_category, user_id=target_user_id, default_id=0)
            if category_id:
                append_mp_job_log(job_id, f"Kategori cache'den eşleşti: '{excel_category}' -> {category_id}")
        
        # 3. If still no match, try TF-IDF with Excel category name
        if not category_id and excel_category and auto_match:
            category_id = match_category_id_for_title_tfidf(excel_category, user_id=target_user_id)
            if category_id:
                append_mp_job_log(job_id, f"Kategori TF-IDF (kategori ismi): '{excel_category}' -> {category_id}")
        
        # 4. Last resort: TF-IDF with product title
        if not category_id and auto_match:
            category_id = match_category_id_for_title_tfidf(title, user_id=target_user_id)
            if category_id:
                matched_count += 1
                append_mp_job_log(job_id, f"Kategori TF-IDF (ürün başlığı): '{title[:50]}...' -> {category_id}")
        
        if not category_id:
            skipped.append({'barcode': barcode, 'reason': f'Kategori eşleşmedi: {excel_category or "boş"}'})
            continue

        # Price & Stock
        try:
            base_price = float(product.get('price', 0)) * multiplier
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
            base_price = default_price * multiplier
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

        # Build attributes
        if product.get('is_manual') and product.get('marketplace_attributes_json'):
            try:
                mp_attrs = json.loads(product.get('marketplace_attributes_json'))
                attributes_payload = []
                for a in mp_attrs:
                    val = a.get('value')
                    attr_id = int(a.get('id'))
                    # For Trendyol, we try to send as custom if possible, 
                    # but if it was a selection we might need valueId.
                    # Simplified: if value is digits, assume it might be an ID? No, safer to send as custom or resolve.
                    attributes_payload.append({
                        "attributeId": attr_id,
                        "customAttributeValue": val
                    })
                append_mp_job_log(job_id, f"Manuel özellikler kullanılıyor ({len(attributes_payload)} adet)")
            except:
                attributes_payload = build_simple_attributes(category_id)
        else:
            # Build minimal required attributes
            attributes_payload = build_simple_attributes(category_id)

        # Build V2 Payload Item
        item = {
            "barcode": barcode,
            "title": title[:100],
            "productMainId": barcode,
            "brandId": brand_id,
            "categoryId": category_id,
            "quantity": stock,
            "stockCode": product.get('stock_code') or barcode,
            "dimensionalWeight": 2,
            "description": desc,
            "currencyType": "TRY",
            "listPrice": listPrice,
            "salePrice": salePrice,
            "vatRate": 20,
            "cargoCompanyId": 10,
            "images": [{"url": url} for url in product_images],
            "attributes": attributes_payload
        }
        items_to_send.append(item)

        # LOCAL DATABASE SYNC: Create or update local product
        try:
            from app.utils.helpers import sync_product_to_local
            from app import db
            
            target_user_id = user_id
            if not target_user_id:
                from flask_login import current_user
                if current_user and current_user.is_authenticated:
                    target_user_id = current_user.id
            
            if target_user_id:
                # Use centralized helper
                sync_product_to_local(
                    user_id=target_user_id,
                    barcode=barcode,
                    product_data=product,
                    xml_source_id=xml_source_id
                )
                
                # Commit every 25 products to avoid long transaction but keep it efficient
                if processed % 25 == 0:
                    db.session.commit()
            
        except Exception as db_err:
            logging.error(f"Error syncing {barcode} to local DB: {db_err}")
        
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

    # Final commit for remaining products
    try:
        from app import db
        db.session.commit()
    except:
        pass

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

def perform_trendyol_send_all(job_id: str, xml_source_id: Any, auto_match: bool = False) -> Dict[str, Any]:
    """Send ALL products from XML source to Trendyol"""
    append_mp_job_log(job_id, "Tüm ürünler hazırlanıyor...")
    
    xml_index = load_xml_source_index(xml_source_id)
    mp_map = xml_index.get('by_barcode') or {}
    all_barcodes = list(mp_map.keys())
    
    if not all_barcodes:
        return {'success': False, 'message': 'XML kaynağında ürün bulunamadı.', 'count': 0}
    
    append_mp_job_log(job_id, f"Toplam {len(all_barcodes)} ürün bulundu. Gönderim başlıyor...")
    
    return perform_trendyol_send_products(job_id, all_barcodes, xml_source_id, auto_match)


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


def perform_trendyol_product_update(barcode: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Detailed product update for Trendyol.
    Handles Price/Inventory and Content updates separately.
    """
    client = get_trendyol_client()
    messages = []
    success = True
    
    # 1. Price/Inventory Update
    if 'salePrice' in data or 'listPrice' in data or 'quantity' in data:
        try:
            payload = {'barcode': barcode, 'currencyType': 'TRY'}
            if 'quantity' in data:
                payload['quantity'] = int(data['quantity'])
            if 'salePrice' in data:
                 payload['salePrice'] = float(data['salePrice'])
            if 'listPrice' in data:
                 payload['listPrice'] = float(data['listPrice'])
            
            # Using list wrapper as expected by client
            client.update_price_inventory([payload])
            messages.append("Fiyat/Stok güncellendi.")
        except Exception as e:
            # Don't mark as fail yet, try content
            messages.append(f"Fiyat/Stok hatası: {e}")
            if not any(k in data for k in ['title', 'description', 'vatRate', 'stockCode', 'images']):
                success = False

    # 2. Content Update (Title, Description, VAT, StockCode, Images)
    content_fields = ['title', 'description', 'vatRate', 'stockCode', 'images']
    if any(k in data for k in content_fields):
        try:
            # Fetch current product info to ensure we have mandatory fields like CategoryId, BrandId
            current_resp = client.list_products(barcode=barcode, size=1)
            current_content = current_resp.get('content', []) or current_resp.get('items', [])
            
            if not current_content:
                 messages.append("Ürün Trendyol'da bulunamadı, içerik güncellenemedi.")
                 success = False
            else:
                curr = current_content[0]
                
                # Careful: Trendyol update often replaces the whole object.
                # Must map 'productMainId', 'brandId', 'categoryId' etc. correctly.
                
                update_item = {
                    'barcode': barcode,
                    'title': data.get('title', curr.get('title')),
                    'description': data.get('description', curr.get('description')),
                    'vatRate': int(data.get('vatRate')) if data.get('vatRate') else curr.get('vatRate'),
                    'stockCode': data.get('stockCode', curr.get('stockCode')),
                    'categoryId': curr.get('categoryId'),
                    'brandId': curr.get('brandId'),
                    'quantity': int(data.get('quantity')) if data.get('quantity') else curr.get('quantity'),
                    'salePrice': float(data.get('salePrice')) if data.get('salePrice') else curr.get('salePrice'),
                    'listPrice': float(data.get('listPrice')) if data.get('listPrice') else curr.get('listPrice'),
                    'attributes': curr.get('attributes', []), # Preserve attributes
                    'currencyType': 'TRY'
                }
                
                # Handle images if provided
                if 'images' in data and isinstance(data['images'], list):
                     update_item['images'] = [{'url': url} for url in data['images']]
                else:
                    # Keep existing images but strip extra fields if needed or just pass as is
                    update_item['images'] = curr.get('images', [])

                client.update_product([update_item])
                messages.append("İçerik güncellendi (Onay gerekebilir).")
                
        except Exception as e:
            success = False
            messages.append(f"İçerik güncelleme hatası: {str(e)}")
            
    return {'success': success, 'message': ' | '.join(messages)}


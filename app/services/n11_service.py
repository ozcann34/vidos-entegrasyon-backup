import logging
logger = logging.getLogger(__name__)
import time
import json
from datetime import datetime
from typing import List, Dict, Any, Optional
from difflib import get_close_matches
# TF-IDF imports (optional, graceful fallback)
try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    logger.warning("sklearn yüklü değil, N11 TF-IDF kategori eşleştirme çalışmayacak")

from app import db
from app.models import Product, Setting, MarketplaceProduct
from flask_login import current_user
from app.services.n11_client import get_n11_client
from app.services.job_queue import append_mp_job_log
from app.utils.helpers import clean_forbidden_words, to_int, to_float, is_product_forbidden, calculate_price, chunked

# ---------------------------------------------------
# N11 Category Caching & Auto Match Globals
# ---------------------------------------------------
_N11_CATEGORY_CACHE = {
    "by_id": {}, 
    "list": [], 
    "loaded": False,
    "timestamp": 0
}

_N11_CAT_TFIDF = {
    "leaf": [],
    "names": [],
    "vectorizer": None,
    "matrix": None,
}

def load_n11_categories_from_db(user_id: int = None) -> bool:
    try:
        data = Setting.get("N11_CATEGORY_CACHE", "", user_id=user_id)
        if data:
            import json
            j = json.loads(data)
            _N11_CATEGORY_CACHE["by_id"] = {int(k): v for k,v in j.get("by_id", {}).items()}
            _N11_CATEGORY_CACHE["list"] = j.get("list", [])
            _N11_CATEGORY_CACHE["loaded"] = True
            _N11_CATEGORY_CACHE["timestamp"] = j.get("timestamp", 0)
            return True
    except Exception as e:
        logging.warning(f"Failed to load N11 categories from DB: {e}")
    return False

def save_n11_categories_to_db(user_id: int = None):
    try:
        payload = {
            "by_id": _N11_CATEGORY_CACHE["by_id"],
            "list": _N11_CATEGORY_CACHE["list"],
            "timestamp": time.time()
        }
        Setting.set("N11_CATEGORY_CACHE", json.dumps(payload), user_id=user_id)
    except Exception as e:
        logging.error(f"Failed to save N11 categories to DB: {e}")

def fetch_and_cache_n11_categories(force=False, user_id: int = None):
    """Fetch all N11 categories and build cache."""
    if not force and _N11_CATEGORY_CACHE["loaded"] and _N11_CATEGORY_CACHE["list"]:
        return True
    
    if not force and load_n11_categories_from_db(user_id=user_id):
        if not _N11_CAT_TFIDF["vectorizer"]:
            _build_n11_tfidf()
        return True

    client = get_n11_client(user_id=user_id)
    if not client:
        return False

    try:
        cats = client.get_categories() or []
        
        flat_list = []
        by_id = {}

        def _recurse(node, parent_path=""):
            cid = node.get('id')
            name = node.get('name')
            
            current_path = f"{parent_path} > {name}" if parent_path else name
            
            sub_cats = node.get('subCategories', [])
            if not sub_cats:
                 # Leaf node
                 item = {'id': cid, 'name': name, 'path': current_path}
                 flat_list.append(item)
                 by_id[cid] = item
            else:
                 for sub in sub_cats:
                     _recurse(sub, current_path)

        # Handling API structure variations
        if isinstance(cats, dict):
            # API returns {'categories': [...]}
            category_list = cats.get('categories') or cats.get('category') or []
        else:
            category_list = cats
        
        if not isinstance(category_list, list):
            category_list = [category_list]

        for c in category_list:
            _recurse(c)

        _N11_CATEGORY_CACHE["by_id"] = by_id
        _N11_CATEGORY_CACHE["list"] = flat_list
        _N11_CATEGORY_CACHE["loaded"] = True
        
        save_n11_categories_to_db(user_id=user_id)
        _build_n11_tfidf()
        logging.info(f"N11 Categories cached: {len(flat_list)} leaf categories.")
        return True

    except Exception as e:
        logging.error(f"Error fetching N11 categories: {e}")
        return False

def _build_n11_tfidf():
    """Build TF-IDF matrix for N11 categories"""
    try:
        leafs = _N11_CATEGORY_CACHE["list"]
        if not leafs:
            return
        
        names = [f"{c['name']} {c['path']}" for c in leafs]
        
        vec = TfidfVectorizer(analyzer='char', ngram_range=(3, 5))
        matrix = vec.fit_transform(names)
        
        _N11_CAT_TFIDF["leaf"] = leafs
        _N11_CAT_TFIDF["names"] = names
        _N11_CAT_TFIDF["vectorizer"] = vec
        _N11_CAT_TFIDF["matrix"] = matrix
        
    except Exception as e:
        logging.error(f"N11 TF-IDF build error: {e}")

def find_matching_n11_category(query: str, user_id: int = None, job_id: str = None) -> Optional[Dict[str, Any]]:
    # Cleanup query: Remove redundant symbols often found in XML paths
    query_clean = query.replace('>>>', ' ').replace('>', ' ').strip()
    
    # 1. PRIORITY: Check manual mapping or previously saved matches in DB
    from app.services.smart_match_service import SmartMatchService
    db_cat_id, db_cat_path = SmartMatchService.get_category_match(query, 'n11')
    if db_cat_id:
        if job_id:
            append_mp_job_log(job_id, f"Veritabanından eşleşme bulundu: '{query}' -> ID={db_cat_id} ({db_cat_path})", level='info')
        if db_cat_id in _N11_CATEGORY_CACHE["by_id"]:
            return _N11_CATEGORY_CACHE["by_id"][db_cat_id]
        return {'id': db_cat_id, 'name': 'Kayıtlı Eşleşme', 'path': db_cat_path or 'Bilinmiyor'}

    # 2. Fallback to TF-IDF auto-matching
    if not _N11_CATEGORY_CACHE["loaded"] or not _N11_CATEGORY_CACHE["list"]:
        fetch_and_cache_n11_categories(user_id=user_id)
        
    if not _N11_CAT_TFIDF["vectorizer"] and _N11_CATEGORY_CACHE["list"]:
        _build_n11_tfidf()
    
    if not _N11_CAT_TFIDF["vectorizer"]:
        if job_id: append_mp_job_log(job_id, "Kategori listesi yüklenemedi, eşleşme yapılamıyor.", level='warning')
        return None

    try:
        vec = _N11_CAT_TFIDF["vectorizer"]
        mat = _N11_CAT_TFIDF["matrix"]
        
        q_vec = vec.transform([query_clean])
        sims = cosine_similarity(q_vec, mat).flatten()
        
        best_idx = sims.argmax()
        score = sims[best_idx]
        
        match = _N11_CAT_TFIDF["leaf"][best_idx]
        
        # User requested "complete" matching, so we lower threshold and always pick best candidate if not 0
        if score > 0.05:
            if score < 0.15 and job_id:
                append_mp_job_log(job_id, f"Düşük puanlı ancak en yakın aday seçildi ({score:.2f}): {match['path']}", level='info')
            
            # Save this match to DB for future use
            SmartMatchService.save_category_match(query, 'n11', match['id'], match['path'])
            return match
        else:
            if job_id:
                append_mp_job_log(job_id, f"Eşleşme bulunamadı (Puan: {score:.2f})", level='warning')
    except Exception as e:
        logging.error(f"Match error: {e}")
    
    return None

# ---------------------------------------------------
# Attribute & Brand Matching Support
# ---------------------------------------------------
_N11_ATTR_CACHE = {} # cat_id -> [attributes] with values

def get_n11_category_attributes(category_id: int, user_id: int = None):
    """Fetch attributes for a category (Cached in memory)."""
    if category_id in _N11_ATTR_CACHE:
        return _N11_ATTR_CACHE[category_id]
        
    client = get_n11_client(user_id=user_id)
    if not client: return []
    
    # Call the client method which is implemented in n11_client.py
    try:
        attrs = client.get_category_attributes(category_id)
        if attrs:
             _N11_ATTR_CACHE[category_id] = attrs
             return attrs
        return []
    except Exception as e:
        import logging
        logging.error(f"Error fetching N11 attributes for {category_id}: {e}")
        return []


def search_n11_brand(name: str, user_id: int = None, cat_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Search for a brand in N11 via Category Attributes (Attribute ID 1).
    """
    if not name: return None
    
    name = name.lower().strip()
    
    client = get_n11_client(user_id=user_id)
    if not client: return None

    # 1. If cat_id provided, search there first (Most accurate)
    target_cats = []
    if cat_id:
        target_cats.append(cat_id)
    
    # 2. Add common fallback categories
    fallback_cats = [1000476, 1000482, 1000273, 1002571]
    for fc in fallback_cats:
        if fc not in target_cats:
            target_cats.append(fc)
    
    found_brand = None
    for cid in target_cats:
        try:
            attrs = get_n11_category_attributes(cid, user_id=user_id)
            for attr in attrs:
                 if str(attr.get('id')) == '1': # Brand Attribute
                      values = attr.get('values') or attr.get('valueList') or []
                      for v in values:
                           v_name = v.get('name') or v.get('value') or ''
                           if v_name.lower().strip() == name:
                                return {'id': v.get('id'), 'name': v_name}
                           
                           # Partial/Loose match check
                           if name in v_name.lower() or v_name.lower() in name:
                                if not found_brand: found_brand = {'id': v.get('id'), 'name': v_name}
        except Exception:
            continue
    
    return found_brand
    
    return found_brand

# ---------------------------------------------------
# Product Operations
# ---------------------------------------------------

def fetch_all_n11_products(job_id: Optional[str] = None, user_id: int = None) -> List[Dict[str, Any]]:
    client = get_n11_client(user_id=user_id)
    if not client:
        return []

    all_products = []
    page = 0
    size = 100
    total_elements = -1
    total_pages = -1
    
    while True:
        try:
            response = client.get_products(page=page, size=size)
            if not response or 'content' not in response:
                logger.warning(f"[N11] Sayfa {page} cevabında 'content' bulunamadı.")
                break
                
            products = response['content']
            
            # First page: capture totals
            if page == 0:
                total_elements = int(response.get('totalElements', -1))
                total_pages = int(response.get('totalPages', -1))
                if job_id: append_mp_job_log(job_id, f"N11'de toplam {total_elements} ürün bulundu ({total_pages} sayfa).")
                logger.info(f"[N11] Total items: {total_elements}, Pages: {total_pages}")

            if not products:
                logger.info(f"[N11] Sayfa {page} boş, döngü sonlandırılıyor.")
                break
                
            all_products.extend(products)
            logger.info(f"[N11] Page {page} fetched: {len(products)} items. Total so far: {len(all_products)}")
            
            if len(products) < size: 
                break
                
            page += 1
            if total_pages > 0 and page >= total_pages:
                break
                
            time.sleep(0.2)
        except Exception as e:
            msg = f"N11 ürün çekme hatası (Sayfa {page}): {str(e)}"
            logger.error(msg)
            if job_id: append_mp_job_log(job_id, msg, level='error')
            # If it's the first page, we stop. If later, we might have partial results.
            break
            
    # Verification
    if total_elements > 0 and len(all_products) < total_elements:
        warn_msg = f"DİKKAT: N11'de {total_elements} ürün var denildi ancak {len(all_products)} ürün çekilebildi!"
        logger.warning(warn_msg)
        if job_id: append_mp_job_log(job_id, warn_msg, level='warning')
            
    return all_products

def refresh_n11_cache(job_id: Optional[str] = None, user_id: int = None) -> Dict[str, Any]:
    try:
        if job_id: append_mp_job_log(job_id, "N11 ürünleri çekiliyor (Snapshot)...")
        items = fetch_all_n11_products(job_id, user_id=user_id)
        
        payload = {
            'items': items,
            'total': len(items),
            'saved_at': time.time()
        }
        Setting.set('N11_EXPORT_SNAPSHOT', json.dumps(payload), user_id=user_id)
        
        if job_id: append_mp_job_log(job_id, f"Toplam {len(items)} N11 ürünü önbelleğe alındı.")
        return {'success': True, 'count': len(items), 'total': len(items)}

    except Exception as e:
        return {'success': False, 'message': str(e)}

def load_n11_snapshot() -> Dict[str, Any]:
    try:
        raw = Setting.get('N11_EXPORT_SNAPSHOT', '') or ''
        if raw: return json.loads(raw)
    except: pass
    return {}

# ---------------------------------------------------
# Sending Logic
# ---------------------------------------------------

def perform_n11_send_products(job_id: str, barcodes: List[str], xml_source_id: Any, auto_match: bool = False, match_by: str = 'barcode', title_prefix: str = None, user_id: int = None, **kwargs) -> Dict[str, Any]:
    from app.services.xml_service import load_xml_source_index
    from app.utils.helpers import get_marketplace_multiplier
    
    # Extract options from kwargs
    price_multiplier = to_float(kwargs.get('price_multiplier', 0))
    
    # If multiplier not provided or is 0/1, try to get from settings
    if price_multiplier <= 0 or price_multiplier == 1.0:
        setting_multiplier = Setting.get("N11_PRICE_MULTIPLIER", user_id=user_id)
        if setting_multiplier:
            price_multiplier = to_float(setting_multiplier)
    
    # Final fallback to 1.0
    if price_multiplier <= 0:
        price_multiplier = 1.0
    
    default_price_val = to_float(kwargs.get('default_price', 0.0))
    skip_no_barcode = kwargs.get('skip_no_barcode', False)
    skip_no_image = kwargs.get('skip_no_image', False)
    zero_stock_as_one = kwargs.get('zero_stock_as_one', False)
    
    src = None
    if xml_source_id:
        try:
            from app.models import SupplierXML
            s_id = str(xml_source_id)
            if s_id.isdigit():
                src = SupplierXML.query.get(int(s_id))
                if src and not user_id: user_id = src.user_id
        except Exception as e:
            logging.warning(f"Failed to resolve user_id or src: {e}")

    client = get_n11_client(user_id=user_id)
    if not client:
        return {'success': False, 'message': 'N11 API bilgileri eksik.'}
        
    append_mp_job_log(job_id, f"N11 gönderimi başlatılıyor... (User ID: {user_id}) Seçenekler: Çarpan={price_multiplier}, Barkodsuz Atla={skip_no_barcode}")
    
    # 1. Load Categories if needed
    if auto_match:
        append_mp_job_log(job_id, "Kategoriler yükleniyor ve kontrol ediliyor...")
        fetch_and_cache_n11_categories(user_id=user_id)

    xml_index = load_xml_source_index(xml_source_id)
    mp_map = xml_index.get('by_barcode') or {}
    
    # Use price_multiplier directly
    multiplier = price_multiplier
    shipment_template = Setting.get("N11_DEFAULT_SHIPMENT_TEMPLATE", "Standart", user_id=user_id)
    # Barcode Settings (Sync Page & General)
    auto_gen_empty = Setting.get("N11_AUTO_GENERATE_BARCODE", "0", user_id=user_id) == "1" or \
                     Setting.get("AUTO_SYNC_USE_RANDOM_BARCODE_n11", "false", user_id=user_id) == "true" or \
                     (src and src.use_random_barcode)
    auto_gen_all = Setting.get("N11_OVERWRITE_BARCODE_ALL", "0", user_id=user_id) == "1" or \
                   Setting.get("AUTO_SYNC_USE_OVERRIDE_BARCODE_n11", "false", user_id=user_id) == "true"
    from app.services.xml_service import generate_random_barcode

    items_to_send = []
    skipped = []
    failures_list = []
    success_count = 0
    fail_count = 0

    # 2. Match Categories First (Collect IDs to fetch attributes)
    matched_products = [] # list of (barcode, product_data, cat_id)

    # Local Stock Code Index for Matching
    local_by_stock = {}
    if match_by == 'stock_code':
        try:
            snap = load_n11_snapshot()
            local_list = snap.get('items', [])
            for p in local_list:
                sc = p.get('stockCode')
                if sc: local_by_stock[sc] = p
            append_mp_job_log(job_id, f"Stok kodu eşleşmesi için {len(local_by_stock)} yerel ürün indekslendi.")
        except Exception as e:
            append_mp_job_log(job_id, f"Snapshot yükleme hatası: {e}", level='warning')
    
    append_mp_job_log(job_id, f"{len(barcodes)} ürün hazırlanıyor...")
    
    # SPEED OPTIMIZATION: Cache category matches by path
    cat_path_cache = {}
    
    total_to_process = len(barcodes)
    for idx, barcode in enumerate(barcodes, start=1):
        if idx % 10 == 0 or idx == total_to_process:
             update_mp_job(job_id, progress={
                 'current': idx,
                 'total': total_to_process,
                 'message': f"Ürünler hazırlanıyor: {idx}/{total_to_process}"
             })
    
    for barcode in barcodes:
        product = mp_map.get(barcode)
        if not product:
            skipped.append({'barcode': barcode, 'reason': 'XML verisi yok'})
            continue

        target_barcode = barcode
        if match_by == 'stock_code':
            sc = product.get('stockCode')
            if sc and sc in local_by_stock:
                matched_local = local_by_stock[sc]
                target_barcode = matched_local.get('stockCode') # N11 uses StockCode as main ID usually
                # Wait, if we matched, we use XML data to update N11 product which has THIS stock code.
                # So target_barcode = sc is correct?
                # Actually perform_n11_send_products uses 'stockCode': item['barcode'] in payload (line 519)
                # and 'barcode': item['barcode'] (line 520).
                # If we change 'barcode' variable here to be the stock code?
                # If target_barcode is used as the key for 'barcode' in payload?
                # Let's override 'barcode' variable effectively for downstream usage?
                # But downstream uses 'barcode' as key to access 'mp_map' again? No, 'product' is already retrieved.
                # Downstream uses 'barcode' for reporting and for payload construction.
                # If we are UPDATING an existing product found by stock code, we should probably use THAT stock code as the ID.
                # If match found: target_barcode = sc.
                # NOTE: N11 uses 'productSellerCode' (stockCode) as primary key often.
                # If local matching found, we proceed.
                append_mp_job_log(job_id, f"Stok Kodu Eşleşmesi: XML({barcode}) -> MP({sc})")
                # We can update the 'barcode' loop variable? No, keep it for reference but use target in matched_products?
                # matched_products stores 'barcode' as key. 
                # Let's store target_barcode in matched_products dict.
            else:
                 # No match found locally. If match_by='stock_code', should we fail or create new?
                 # User intent: "Match if exists, otherwise create new" usually.
                 pass
            
        # Blacklist check
        forbidden_reason = is_product_forbidden(user_id, title=product.get('title'), brand=product.get('brand'), category=product.get('category'))
        if forbidden_reason:
            skipped.append({'barcode': barcode, 'reason': f"Yasaklı Liste: {forbidden_reason}"})
            continue

        title = clean_forbidden_words(product.get('title', ''))
        if title_prefix:
            title = f"{title_prefix} {title}"
        desc = clean_forbidden_words(product.get('description', '') or title)
        category_path = product.get('category', '')
        
        # Price/Qty Calc
        try:
            raw_price = float(product.get('price', 0))
            # If default price option set and price is 0
            if raw_price <= 0 and send_options.get('default_price'):
                raw_price = float(send_options.get('default_price'))
            
            # Apply multiplier check (Excel UI has a checkbox for it too, usually we honor global setting but check if redundant)
            # Typically XML service always applies multiplier, but here user might want explicit control.
            # Assuming global multiplier is always active, but if unchecked in Excel, maybe 1.0? 
            # For simplicity, we stick to global multiplier.
            # Artık GLOBAL_PRICE_RULES kullanılıyor (multiplier kaldırıldı)
            price = calculate_price(raw_price, 'n11', user_id=user_id)
            price = round(price, 2)
            
            quantity = int(product.get('quantity', 0))
            if quantity <= 0 and kwargs.get('zero_stock_as_one'):
                quantity = 1
        except:
            price = 0; quantity = 0
            
        if price <= 0:
            skipped.append({'barcode': barcode, 'reason': 'Fiyat 0'})
            continue

        # Match Category
        cat_id = None
        if category_path in cat_path_cache:
            cat_id = cat_path_cache[category_path]
        elif auto_match:
            match = find_matching_n11_category(f"{title} {category_path}", user_id=user_id, job_id=job_id)
            if match: 
                cat_id = match['id']
                cat_path_cache[category_path] = cat_id
            
        if not cat_id:
             skipped.append({'barcode': barcode, 'reason': f"Kategori Eşleşmedi ({category_path}). N11 Kategori Eşleştirme ayarlarını yapınız."})
             continue
             
        # Application of Barcode Rules
        target_barcode = barcode
        if auto_gen_all:
            target_barcode = generate_random_barcode()
        elif auto_gen_empty and (not barcode or barcode.strip() == "" or barcode == "0" or barcode.lower() == "bgz0"):
            target_barcode = generate_random_barcode()

        matched_products.append({
            'barcode': barcode, # Original identifier
            'target_barcode': target_barcode, 
            'product': product,
            'cat_id': cat_id,
            'price': price,
            'quantity': quantity,
            'title': title,
            'description': desc
        })

    # 3. Match Attributes (Including Brand) per Category
    # Group by category to optimize attribute fetching if we were to fetch them
    # For now, since we don't have robust attribute fetching implemented in client,
    # we will skip the "Deep Brand Match" via API to avoid breaking execution,
    # BUT we will try to look for "Marka" in the product data and send it if possible.
    # If the user insists on "Matching", we act as if we matched or use local "Marka" field.
    
    if auto_match and matched_products:
         append_mp_job_log(job_id, f"{len(matched_products)} ürün için marka/özellik eşleştirmesi yapılıyor...")
    
    for item in matched_products:
        p = item['product']
        
        # Images
        images = []
        raw_imgs = p.get('images', [])
        for img in raw_imgs:
            if isinstance(img, dict): images.append(img.get('url'))
            elif isinstance(img, str): images.append(img)
            
        # 3. Match Attributes (Including Brand)
        attributes = []
        
        # Add default brand if exists and not already present
        brand_added = False
        
        # --- PREPARE VARIANT ATTRIBUTE MATCHING ---
        variant_attributes = p.get('variant_attributes', [])
        
        def get_variant_value(attr_name):
            attr_name_lower = attr_name.lower()
            for va in variant_attributes:
                v_name = va['name'].lower()
                if v_name in attr_name_lower or attr_name_lower in v_name:
                    return va['value']
            return None
        
        # --- AUTO-MATCH MANDATORY ATTRIBUTES ---
        try:
             # Fetch attributes for this category
             # using the client method we validated
             cat_attrs = get_n11_category_attributes(item['cat_id'], user_id=user_id)
             
             for cat_attr in cat_attrs:
                 # FIX: N11 CDN fields are different (attributeId, attributeName, isMandatory)
                 attr_id = cat_attr.get('id') or cat_attr.get('attributeId')
                 mandatory = cat_attr.get('mandatory') or cat_attr.get('isMandatory') or False
                 attr_name = cat_attr.get('name') or cat_attr.get('attributeName') or ''
                 
                 # Brand Mapping (ID 1)
                 if str(attr_id) == '1':
                     # PRIORITY: 1. Default Setting, 2. XML Brand
                     brand_name_to_use = default_brand or p.get('brand')
                     
                     if brand_name_to_use:
                         # Attempt to resolve brand to N11 ID
                         n11_brand = search_n11_brand(brand_name_to_use, user_id=user_id, cat_id=item['cat_id'])
                         if n11_brand:
                             attributes.append({
                                 "id": 1,
                                 "valueId": n11_brand['id']
                             })
                             append_mp_job_log(job_id, f"MARKA EŞLEŞTİ: {brand_name_to_use} -> ID:{n11_brand['id']}")
                             brand_added = True
                         else:
                             # Fallback to customValue if not found in list
                             attributes.append({
                                 "id": 1,
                                 "valueId": None,
                                 "customValue": brand_name_to_use
                             })
                             append_mp_job_log(job_id, f"MARKA ÖZEL DEĞER: {brand_name_to_use} (N11 listesinde bulunamadı)")
                             brand_added = True
                     continue

                 # Ensure mandatory attributes are handled
                 if mandatory:
                     # Try to match from variant_attributes first
                     val_from_xml = get_variant_value(attr_name)
                     
                     # Search for the value in N11's attribute values
                     matched_value_id = None
                     values = cat_attr.get('values') or cat_attr.get('valueList') or cat_attr.get('attributeValues') or []

                     if val_from_xml and values:
                         val_from_xml_lower = val_from_xml.lower()
                         for v in values:
                              v_opt_name = (v.get('name') or v.get('value', '')).lower()
                              if v_opt_name == val_from_xml_lower:
                                  matched_value_id = v.get('id')
                                  break
                         # Fuzzy match
                         if not matched_value_id:
                             for v in values:
                                 v_opt_name = (v.get('name') or v.get('value', '')).lower()
                                 if v_opt_name in val_from_xml_lower or val_from_xml_lower in v_opt_name:
                                     matched_value_id = v.get('id')
                                     break
                     
                     # Fallback to title matching if no variant attribute or match
                     if not matched_value_id and values:
                         # Sort values by length descending to match "iPhone 13 Pro Max" before "iPhone 13"
                         values.sort(key=lambda x: len(x.get('name') or x.get('value', '')), reverse=True)
                         
                         for val in values:
                             v_opt_name = (val.get('name') or val.get('value', '')).lower()
                             if v_opt_name and v_opt_name in item['title'].lower():
                                 matched_value_id = val.get('id')
                                 break
                     
                     if matched_value_id:
                         attributes.append({
                             "id": attr_id,
                             "valueId": matched_value_id
                         })
                         append_mp_job_log(job_id, f"OTOMATİK EŞLEŞME: {attr_name} ({attr_id}) -> {val_from_xml or 'Başlıktan'}")
                     elif val_from_xml:
                         # If no ID found but we have a value, try customValue
                         attributes.append({
                             "id": attr_id,
                             "valueId": None,
                             "customValue": val_from_xml
                         })
                         append_mp_job_log(job_id, f"ÖZEL DEĞER: {attr_name} ({attr_id}) -> {val_from_xml}")
                     elif values:
                         # Final resort: first value
                         attributes.append({
                             "id": attr_id,
                             "valueId": values[0].get('id')
                         })
                         append_mp_job_log(job_id, f"VARSAYILAN: {attr_name} ({attr_id}) için ilk değer kullanıldı.", level='info')
                     else:
                         append_mp_job_log(job_id, f"UYARI: Zorunlu özellik '{attr_name}' ({attr_id}) için eşleşme bulunamadı.", level='warning')
                         
        except Exception as e:
            append_mp_job_log(job_id, f"Özellik eşleştirme hatası: {e}", level='error')

        # Final Fallback for Brand if not added via loop (Category might not have attribute 1 but N11 might need it?)
        if not brand_added:
             brand_val = default_brand or p.get('brand')
             if brand_val:
                attributes.append({
                    "id": 1,
                    "valueId": None,
                    "customValue": brand_val
                })

        payload_item = {
            "title": item['title'][:200],
            "description": p.get('details') or item['description'],
            "categoryId": int(item['cat_id']), # FLAT ID
            "salePrice": float(f"{item['price']:.2f}"),
            "listPrice": float(f"{item['price']:.2f}"),
            "vatRate": 20, # Mandatory. Defaulting to 20%
            "currencyType": "TL",
            "images": [{"url": u, "order": i+1} for i, u in enumerate(images[:8])],
            "quantity": item['quantity'],
            "stockCode": p.get('stockCode') or item['target_barcode'], # XML Stok Kodu
            "barcode": item['target_barcode'], # Final Barcode
            "productMainId": item['target_barcode'], # Model Kodu = Barkod (Kullanıcı talebi)
            "shipmentTemplate": shipment_template,
            "preparingDay": 3,
            "maxPurchaseQuantity": 50, # Optional
            "attributes": attributes # List of attributes [ {attributeId, valueId} ]
        }
        
        
        # --- LOGGING & VALIDATION ---
        validation_errors = []
        if not payload_item.get("shipmentTemplate") or payload_item["shipmentTemplate"] == "Standart":
             validation_errors.append("UYARI: Kargo şablonu 'Standart' (N11 panelinde yoksa hata verir).")
        if not payload_item.get("vatRate"):
             validation_errors.append("HATA: KDV oranı (vatRate) eksik.")
        if not payload_item.get("images"):
             validation_errors.append("HATA: Ürün görseli yok.")
        if not payload_item.get("stockCode"):
             validation_errors.append("HATA: Stok Kodu (stockCode) yok.")
        
        # Check Attributes
        has_brand = any(a.get('id') == 1 for a in attributes)
        if not has_brand:
             validation_errors.append("UYARI: Marka (Attribute ID 1) bulunamadı. (Ayarlardan varsayılan marka giriniz).")

        if validation_errors:
             err_msg = f"{item['barcode']} için eksikler: " + ", ".join(validation_errors)
             append_mp_job_log(job_id, err_msg, level='warning')
        
        # --- PERSIST TO LOCAL DB ---
        try:
            from app.models.product import MarketplaceProduct
            from app import db
            mp_p = MarketplaceProduct.query.filter_by(user_id=user_id, marketplace='n11', barcode=payload_item['barcode']).first()
            if not mp_p:
                mp_p = MarketplaceProduct(user_id=user_id, marketplace='n11', barcode=payload_item['barcode'])
                db.session.add(mp_p)
            
            mp_p.stock_code = payload_item['stockCode']
            mp_p.title = payload_item['title']
            mp_p.price = payload_item['listPrice']
            mp_p.sale_price = payload_item['salePrice']
            mp_p.quantity = payload_item['quantity']
            mp_p.category = category_path # XML Path
            # We can also store the N11 category ID in a field if MarketplaceProduct had it, 
            # but for now we use 'category' string.
            mp_p.last_sync_at = datetime.now()
            db.session.commit()
        except Exception as e:
            logging.error(f"Error saving MarketplaceProduct: {e}")

        # Log First Payload for Debugging
        if len(items_to_send) == 0:
             import json
             debug_pl = json.dumps(payload_item, indent=2, ensure_ascii=False)
             append_mp_job_log(job_id, f"DEBUG - İlk Ürün Verisi:\n{debug_pl}")
        # ----------------------------

        items_to_send.append(payload_item)

    if not items_to_send:
        msg = 'Gönderilecek ürün oluşturulamadı.'
        if skipped:
            msg += f" ({len(skipped)} atlandı). İlk 3 sebep: " + ", ".join([s['reason'] for s in skipped[:3]])
        append_mp_job_log(job_id, msg, level='error')
        return {'success': False, 'message': msg, 'skipped': skipped}

    # 4. Send
    chunk_size = 100
    total_sent = 0
    main_task_id = None
    
    import math
    chunks = [items_to_send[i:i + chunk_size] for i in range(0, len(items_to_send), chunk_size)]
    
    for idx, chunk in enumerate(chunks):
        append_mp_job_log(job_id, f"Part {idx+1}/{len(chunks)} gönderiliyor ({len(chunk)} ürün)...")
        
        # Retry loop for API Limits
        max_retries = 3
        retry_delay = 300 # 5 minutes
        
        for attempt in range(max_retries):
            try:
                update_mp_job(job_id, progress={
                    'current': total_sent,
                    'total': len(items_to_send),
                    'message': f"Part {idx+1}/{len(chunks)} N11'e iletiliyor..."
                })
                resp = client.create_products(chunk)
                
                # Check for limit error in response
                err_msg = str(resp.get('result', {}).get('errorMessage', ''))
                if "talep limitiniz dolmuştur" in err_msg.lower():
                    append_mp_job_log(job_id, f"⚠️ N11 API Limiti doldu. {retry_delay} saniye bekleniyor... (Deneme {attempt+1}/{max_retries})", level='warning')
                    update_mp_job(job_id, progress={
                        'current': total_sent,
                        'total': len(items_to_send),
                        'message': "API Limiti Bekleniyor (5 dk)..."
                    })
                    time.sleep(retry_delay)
                    continue  # Retry this chunk

                task_id = resp.get('taskId') or resp.get('id')
                if task_id:
                    if not main_task_id: main_task_id = task_id
                    append_mp_job_log(job_id, f"Part {idx+1} Başarılı. Task ID: {task_id}")
                
                    # --- STATUS CHECK LOOP (IMPROVED) ---
                    append_mp_job_log(job_id, f"Task {task_id} onay durumu kontrol ediliyor (maks 45sn)...", level='info')
                    
                    final_results_received = False
                    for _check in range(15):  # 15 * 3s = 45s
                        time.sleep(3)
                        try:
                            t_status = client.check_task_status(task_id)
                            content = t_status.get('content', [])
                            
                            if not content:
                                continue
                                
                            # Check if any item is still non-final
                            # Statuses: WAITING, DOING, DONE, ERROR, REJECTED, REJECT
                            pending_count = 0
                            batch_done = 0
                            batch_fail = 0
                            
                            for task_info in content:
                                st = task_info.get('status', '').upper()
                                if st in ['WAITING', 'DOING']:
                                    pending_count += 1
                                elif st == 'DONE':
                                    batch_done += 1
                                else:
                                    batch_fail += 1
                                    bc = task_info.get('sellerStockCode') or task_info.get('barcode') or 'Bilinmeyen'
                                    e_msg = task_info.get('statusDescription') or task_info.get('message') or ""
                                    if not e_msg and task_info.get('reasons'):
                                        e_msg = ", ".join(task_info['reasons'])
                                    failures_list.append({'barcode': bc, 'reason': e_msg or 'N11 Red/Hata'})
                                    if len(failures_list) <= 15:
                                         append_mp_job_log(job_id, f"   ❌ {bc}: {e_msg}", level='error')
                            
                            if pending_count == 0:
                                # All items reached a final state
                                success_count += batch_done
                                fail_count += batch_fail
                                if batch_fail == 0:
                                    append_mp_job_log(job_id, f"✅ Task {task_id}: Tüm ürünler ({batch_done}) başarıyla işlendi.", level='info')
                                else:
                                    append_mp_job_log(job_id, f"⚠️ Task {task_id}: {batch_done} başarılı, {batch_fail} HATALI ürün.", level='warning')
                                
                                final_results_received = True
                                break
                            else:
                                if _check % 3 == 0:
                                    append_mp_job_log(job_id, f"⏳ Task {task_id}: {pending_count}/{len(content)} ürün hala işleniyor...", level='info')
                                    
                        except Exception as chk_err:
                            append_mp_job_log(job_id, f"Task durum kontrol hatası: {chk_err}", level='warning')
                            break
                    
                    if not final_results_received:
                        append_mp_job_log(job_id, f"🕒 Task {task_id}: İşlem henüz tamamlanmadı, kontrol zaman aşımına uğradı.", level='warning')
                    # -------------------------
                else:
                    # Check for immediate errors
                    err = resp.get('result', {}).get('errorMessage') or str(resp)
                    append_mp_job_log(job_id, f"Part {idx+1} Hata: {err}", level='error')
                    break  # Non-retryable error

                total_sent += len(chunk)
                break  # Success, exit retry loop
                
            except Exception as e:
                if "talep limitiniz dolmuştur" in str(e).lower():
                    append_mp_job_log(job_id, f"⚠️ N11 API Limiti doldu (Exception). {retry_delay} saniye bekleniyor...", level='warning')
                    time.sleep(retry_delay)
                    continue
                append_mp_job_log(job_id, f"Part {idx+1} Exception: {e}", level='error')
                break
            
    return {
        'success': True,
        'success_count': success_count if 'success_count' in locals() else total_sent, 
        'fail_count': (fail_count if 'fail_count' in locals() else 0) + len(skipped),
        'count': total_sent,
        'batch_id': main_task_id,
        'skipped': skipped,
        'message': f"{total_sent} ürün N11'e iletildi.",
        'summary': {
            'success_count': success_count if 'success_count' in locals() else total_sent,
            'fail_count': (fail_count if 'fail_count' in locals() else 0) + len(skipped),
            'failures': failures_list if 'failures_list' in locals() else []
        }
    }

def perform_n11_send_all(job_id: str, xml_source_id: Any, auto_match: bool = False, user_id: int = None, **kwargs) -> Dict[str, Any]:
    from app.services.xml_service import load_xml_source_index
    
    append_mp_job_log(job_id, "Tüm ürünler hazırlanıyor...")
    xml_index = load_xml_source_index(xml_source_id)
    all_barcodes = list((xml_index.get('by_barcode') or {}).keys())
    
    if not all_barcodes:
        return {'success': False, 'message': 'XML kaynağında ürün bulunamadı.'}
        
    return perform_n11_send_products(job_id, all_barcodes, xml_source_id, auto_match, user_id=user_id, **kwargs)

def delete_n11_product(barcode: str) -> Dict[str, Any]:
    try:
        client = get_n11_client()
        if not client: return {'success': False, 'message': 'API Key eksik.'}
        result = client.delete_product_by_seller_code(barcode)
        
        task_id = result.get('id') or result.get('taskId')
        
        # Local delete
        try:
            Product.query.filter_by(user_id=current_user.id, marketplace='n11', barcode=barcode).delete()
            db.session.commit()
        except: pass
        
        if result.get('status') == 'REJECT':
             return {'success': False, 'message': f"N11 Reddeti: {result.get('reasons')}"}
             
        msg = f"Silme (Satışa Kapatma) kuyruğa alındı (Task: {task_id})" if task_id else "İşlem başarılı."
        return {'success': True, 'message': msg, 'details': result}
    except Exception as e:
        return {'success': False, 'message': str(e)}

def update_n11_stock_price(barcode: str, stock: int = None, price: float = None) -> Dict[str, Any]:
    client = get_n11_client()
    if not client: return {'success': False, 'message': 'API Key eksik.'}
    try:
        item = {"stockCode": barcode, "currencyType": "TL"}
        if stock is not None: item["quantity"] = int(stock)
        if price is not None:
            item["salePrice"] = float(price)
            item["listPrice"] = float(price)
            
        resp = client.update_products_price_and_stock([item])
        task_id = resp.get('taskId') or resp.get('id')
        
        if task_id: return {'success': True, 'message': f"Kuyruğa alındı (Task: {task_id})"}
        elif resp.get('status') == 'REJECT': return {'success': False, 'message': f"Reddedildi: {resp.get('reasons')}"}
        
        return {'success': True, 'message': "İletildi.", "debug": resp}
    except Exception as e:
        return {'success': False, 'message': str(e)}

def bulk_update_n11_stock_price(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    items: list of dicts with keys: barcode, quantity, salePrice (optional)
    """
    if not items: return {'success': True, 'updated': 0}
    
    client = get_n11_client()
    if not client: return {'success': False, 'message': 'API Key eksik.'}
    
    try:
        n11_items = []
        for it in items:
            obj = {"stockCode": it.get('barcode'), "currencyType": "TL"}
            if it.get('quantity') is not None:
                obj["quantity"] = int(it.get('quantity'))
            if it.get('salePrice') is not None:
                p = float(it.get('salePrice'))
                obj["salePrice"] = p
                obj["listPrice"] = p
            n11_items.append(obj)
            
        # N11 might have a limit per request? Doc says 1000? 
        # For safety, let's chunk if needed, but client might handle it.
        # Client just posts. Let's send directly for now.
        
        resp = client.update_products_price_and_stock(n11_items)
        task_id = resp.get('taskId') or resp.get('id')
        
        if task_id: 
            return {'success': True, 'message': f"Toplu işlem kuyruğa alındı (Task: {task_id})", 'updated': len(items)}
            
        return {'success': False, 'message': f"Hata: {resp.get('status')}", 'details': resp}
    except Exception as e:
        return {'success': False, 'message': str(e)}

def perform_n11_batch_update(job_id: str, items: List[Dict[str, Any]], user_id: int = None) -> Dict[str, Any]:
    """
    Batch update N11 stock/price.
    items: [{'barcode': '...', 'stock': 10, 'price': 100.0}, ...]
    """
    from app.services.n11_client import get_n11_client
    # Import chunked if not available, usually in utils or just implement it
    def _chunks(lst, n):
        for i in range(0, len(lst), n):
            yield lst[i:i + n]

    client = get_n11_client(user_id=user_id)
    append_mp_job_log(job_id, f"N11 toplu güncelleme başlatıldı. {len(items)} ürün.")
    
    # Map to N11 format
    n11_items = []
    for item in items:
        obj = {"stockCode": item['barcode'], "currencyType": "TL"}
        if 'stock' in item:
            obj["quantity"] = int(item['stock'])
        if 'price' in item:
            p = float(item['price'])
            obj["salePrice"] = p
            obj["listPrice"] = p
        n11_items.append(obj)
        
    total_sent = 0
    # N11 limit is often 100
    chunks = list(chunked(n11_items, 100))
    total_chunks = len(chunks)
    
    for idx, chunk in enumerate(chunks, start=1):
        try:
            from app.services.job_queue import update_mp_job
            progress_percent = int((idx / total_chunks) * 100)
            update_mp_job(job_id, progress={
                'current': total_sent,
                'total': len(n11_items),
                'message': f'N11 güncelleniyor: {total_sent}/{len(n11_items)}'
            })

            resp = client.update_products_price_and_stock(chunk)
            # Response usually contains 'taskId' if async, or result list if sync.
            # Assuming standard behavior update_products_price_and_stock returns info.
            # Actually N11 API documentation says up to 1000 items? Safest is 100.
            
            # Log
            msg = f"Paket {idx}: {len(chunk)} ürün gönderildi."
            if resp and (resp.get('id') or resp.get('taskId')):
                 msg += f" (Task ID: {resp.get('id') or resp.get('taskId')})"
            
            append_mp_job_log(job_id, msg)
            total_sent += len(chunk)
            time.sleep(0.1)
        except Exception as e:
            append_mp_job_log(job_id, f"Paket {idx} hatası: {e}", level='error')
            
    result = {
        'success': True,
        'updated_count': total_sent,
        'success_count': total_sent,
        'fail_count': 0,
        'message': f'{total_sent} ürün için güncelleme isteği gönderildi.',
        'summary': {
            'success_count': total_sent,
            'fail_count': 0
        }
    }
    append_mp_job_log(job_id, "İşlem tamamlandı.")
    return result


def sync_n11_with_xml_diff(job_id: str, xml_source_id: Any, user_id: int = None, **kwargs) -> Dict[str, Any]:
    """Smart Sync for N11 (Diff Logic)"""
    startTime = time.time()
    append_mp_job_log(job_id, "N11 Akıllı Senkronizasyon (Diff Sync) başlatıldı.")
    
    client = get_n11_client(user_id=user_id)
    
    # Updated to use Stock Code & Exclusion List
    
    # 1. Fetch Remote Inventory
    from app.services.job_queue import update_mp_job
    update_mp_job(job_id, progress={'current': 5, 'total': 100, 'message': 'N11 ürünleri çekiliyor...'})
    remote_items = fetch_all_n11_products(job_id=job_id, user_id=user_id)
    # Map STOCK CODE -> Item (because matching is now based on Stock Code)
    remote_stock_map = {}
    for item in remote_items:
        sc = item.get('sellerCode')
        if sc:
            remote_stock_map[sc.strip()] = item
            
    remote_stock_codes = set(remote_stock_map.keys())
    append_mp_job_log(job_id, f"N11 hesabınızda toplam {len(remote_stock_codes)} stok kodlu ürün tespit edildi.")

    # 2. Load XML
    update_mp_job(job_id, progress={'current': 20, 'total': 100, 'message': 'XML verisi analiz ediliyor...'})
    from app.services.xml_service import load_xml_source_index
    xml_index = load_xml_source_index(xml_source_id)
    # Use the new by_stock_code index
    xml_map = xml_index.get('by_stock_code') or {}
    xml_stock_codes = set(xml_map.keys())
    
    # Fallback map: by_barcode
    xml_barcode_map = xml_index.get('by_barcode') or {}
    
    # Load Exclusions
    from app.models.sync_exception import SyncException
    exclusions = SyncException.query.filter_by(user_id=user_id).all()
    excluded_values = {e.value.strip() for e in exclusions}
    if excluded_values:
        append_mp_job_log(job_id, f"⚠️ {len(excluded_values)} ürün 'Hariç Listesi'nde, işlem yapılmayacak.")

    # 3. Find Diff & Fallback Matching
    to_zero_candidates = []
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
            # This is a fallback match!
            matched_stock_codes.append(remote_sc)
            # We must ensure we use the barcode-mapped item later for updates
            # (The update loop will need to handle this lookup)
            processed_remotes.add(remote_sc)
            continue
            
        # If neither, it's a candidate for zeroing
        to_zero_candidates.append(remote_sc)

    # Filter Exclusions from Zeroing (Already handled by loop above effectively, but let's be safe)
    # Actually, the loop skips exclusions completely, so they are neither matched nor zeroed. Correct.
    
    items_to_zero = []
    for sc in to_zero_candidates:
        # One last check just in case logic changes
        if sc not in excluded_values:
             items_to_zero.append({
                'barcode': sc, 
                'stock': 0
            })

    append_mp_job_log(job_id, f"XML'de OLMAYAN {len(items_to_zero)} ürün N11'de sıfırlanıyor.")
    
    zeroed_count = 0
    if items_to_zero:
        # Use perform_n11_batch_update for zeroing
        z_res = perform_n11_batch_update(job_id, items_to_zero, user_id=user_id)
        zeroed_count = z_res.get('updated_count', 0)
        append_mp_job_log(job_id, f"✅ {zeroed_count} ürün başarıyla sıfırlandı.")

    # 4. Lightweight Sync for Matched Products
    # matched_stock_codes contains SCs that exist in XML (either as SC or Barcode)
    
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
            final_price = calculate_price(base_price, 'n11', user_id=user_id)
            
            items_to_update.append({
                'barcode': sc, # Using Stock Code as Identifier for N11 Update
                'stock': qty,
                'price': final_price
            })
        
        if items_to_update:
            u_res = perform_n11_batch_update(job_id, items_to_update, user_id=user_id)
            updated_count = u_res.get('updated_count', 0)
            append_mp_job_log(job_id, f"✅ {updated_count} eşleşen ürün güncellendi.")

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


def perform_n11_sync_stock(job_id: str, xml_source_id: Any, user_id: int = None) -> Dict[str, Any]:
    """
    Sync ONLY stock from XML to N11.
    """
    client = get_n11_client(user_id=user_id)
    from app.services.xml_service import load_xml_source_index
    from app.utils.helpers import to_int
    
    append_mp_job_log(job_id, "N11 stok eşitleme başlatılıyor...")
    xml_index = load_xml_source_index(xml_source_id)
    mp_map = xml_index.get('by_barcode') or {}
    
    if not mp_map:
        return {'success': False, 'message': 'XML kaynağında ürün bulunamadı.', 'updated_count': 0}
        
    items_to_update = []
    for barcode, info in mp_map.items():
        qty = to_int(info.get('quantity'))
        if qty < 0: qty = 0
        items_to_update.append({
            'barcode': barcode,
            'stock': qty
        })
        
    if not items_to_update:
        return {'success': False, 'message': 'Güncellenecek ürün bulunamadı.', 'updated_count': 0}
        
    append_mp_job_log(job_id, f"{len(items_to_update)} ürün için stok güncellemeleri hazırlanıyor...")
    return perform_n11_batch_update(job_id, items_to_update, user_id=user_id)

def perform_n11_sync_prices(job_id: str, xml_source_id: Any, user_id: int = None) -> Dict[str, Any]:
    """
    Sync ONLY prices from XML to N11 (using multiplier).
    """
    client = get_n11_client(user_id=user_id)
    from app.services.xml_service import load_xml_source_index
    from app.utils.helpers import to_float, get_marketplace_multiplier
    
    append_mp_job_log(job_id, "N11 fiyat eşitleme başlatılıyor...")
    xml_index = load_xml_source_index(xml_source_id)
    mp_map = xml_index.get('by_barcode') or {}
    multiplier = get_marketplace_multiplier('n11')
    
    if not mp_map:
        return {'success': False, 'message': 'XML kaynağında ürün bulunamadı.', 'updated_count': 0}
        
    items_to_update = []
    
    for barcode, info in mp_map.items():
        base_price = to_float(info.get('price'))
        if base_price <= 0:
            continue
            
        price = round(base_price * multiplier, 2)
        items_to_update.append({
            'barcode': barcode,
            'price': price
        })
        
    if not items_to_update:
        return {'success': False, 'message': 'Güncellenecek ürün bulunamadı.', 'updated_count': 0}
        
    append_mp_job_log(job_id, f"{len(items_to_update)} ürün için fiyat güncellemeleri hazırlanıyor (Çarpan: {multiplier})...")
    return perform_n11_batch_update(job_id, items_to_update)

def perform_n11_sync_all(job_id: str, xml_source_id: Any, match_by: str = 'barcode', user_id: int = None) -> Dict[str, Any]:
    """
    Sync BOTH stock and prices from XML to N11.
    Now uses Diff Sync logic.
    """
    return sync_n11_with_xml_diff(job_id, xml_source_id, user_id=user_id)


def perform_n11_product_update(barcode: str, data: Dict[str, Any], user_id: int = None) -> Dict[str, Any]:
    """
    Update details for N11 product.
    """
    client = get_n11_client(user_id=user_id)
    messages = []
    success = True
    
    # Identifier: prefer stockCode (SellerCode) if available in DB or fallback to barcode
    # In Vidos, we treat barcode as SellerCode usually.
    stock_code = data.get('stockCode') or barcode
    
    # 1. Price/Stock (Immediate where possible)
    if 'quantity' in data:
        try:
            qty = int(data['quantity'])
            client.update_stock_by_seller_code(stock_code, qty)
            messages.append("Stok güncellendi.")
        except Exception as e:
            messages.append(f"Stok hatası: {e}")
            success = False
            
    if 'salePrice' in data or 'listPrice' in data:
        try:
            # N11 usually only has one display price (the sale price). List price (strikeout) is often ignored or same.
            p = float(data.get('salePrice') or data.get('listPrice'))
            client.update_price_by_seller_code(stock_code, p)
            messages.append("Fiyat güncellendi.")
        except Exception as e:
            messages.append(f"Fiyat hatası: {e}")
            success = False

    # 2. Content Update (requires Product Update Task)
    content_fields = ['title', 'description', 'subtitle', 'images', 'onSale'] 
    
    if any(k in data for k in content_fields):
        try:
            update_item = {'sellerCode': stock_code}
            if 'title' in data:
                update_item['title'] = data['title']
            if 'description' in data:
                update_item['description'] = data['description']
            if 'subtitle' in data:
                update_item['subtitle'] = data['subtitle']
            
            # Images: N11 images structure [{'url':..., 'order':...}]
            if 'images' in data and isinstance(data['images'], list):
                imgs = []
                for idx, url in enumerate(data['images']):
                     imgs.append({'url': url, 'order': str(idx+1)})
                update_item['images'] = imgs
            
            # Status mapping
            if 'onSale' in data:
                 # Note: N11 uses 'productStatus' = 'Active' / 'Suspended'
                 update_item['productStatus'] = 'Active' if data['onSale'] else 'Suspended'
            
            # Only send if we have updateable fields beyond sellerCode
            if len(update_item) > 1:
                client.update_products([update_item])
                messages.append("İçerik güncelleme talebi iletildi.")
                
        except Exception as e:
            messages.append(f"İçerik güncelleme hatası: {e}")
            success = False

    return {'success': success, 'message': ' | '.join(messages)}

def clear_n11_cache(user_id: int):
    """
    Clear N11 related caches and local marketplace product data for a user.
    """
    from app.models import Setting, MarketplaceProduct
    from app import db
    
    # 1. Reset global memory cache (affects all but safe)
    global _N11_CATEGORY_CACHE, _N11_CAT_TFIDF, _N11_ATTR_CACHE
    _N11_CATEGORY_CACHE = {"by_id": {}, "list": [], "loaded": False, "timestamp": 0}
    _N11_CAT_TFIDF = {"leaf": [], "names": [], "vectorizer": None, "matrix": None}
    _N11_ATTR_CACHE = {}
    
    # 2. Clear category settings in DB
    Setting.set("N11_CATEGORY_TREE", "", user_id=user_id)
    
    # 3. Delete local marketplace products for N11
    try:
        MarketplaceProduct.query.filter_by(user_id=user_id, marketplace='n11').delete()
        db.session.commit()
        logging.info("N11 marketplace products cleared for user %s", user_id)
    except Exception as e:
        db.session.rollback()
        logging.error("Failed to clear N11 marketplace products: %s", e)

def sync_n11_products(user_id: int, job_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetch all products from N11 and sync them to the local MarketplaceProduct table.
    """
    from app import db
    from app.models import MarketplaceProduct
    from app.services.job_queue import append_mp_job_log
    
    logger.info(f"[N11] Syncing products for user {user_id}...")
    if job_id:
        append_mp_job_log(job_id, f"N11 ürün senkronizasyonu başlatıldı (User ID: {user_id})")

    try:
        products = fetch_all_n11_products(job_id=job_id, user_id=user_id)
        if not products:
            msg = "N11'den hiç ürün dönmedi veya bir hata oluştu."
            logger.warning(f"[N11] {msg}")
            if job_id: append_mp_job_log(job_id, msg, level='warning')
            return {'success': False, 'message': msg}

        # Check totalElements (requires fetch_all_n11_products to be updated, which we just did)
        # However, we don't have totalElements here easily unless we returned it.
        # Let's assume if we got > 0 products, we proceed, but we check for abnormal gaps later.

        if job_id:
            append_mp_job_log(job_id, f"N11 API'den {len(products)} ürün çekildi. Veritabanına işleniyor...")

        remote_barcodes = []
        for p in products:
            barcode = p.get('sellerCode') or p.get('barcode')
            if not barcode:
                # If no barcode/sellerCode, try to use n11Id as a last resort to keep it unique
                barcode = f"N11-{p.get('n11ProductId') or p.get('id')}"
            remote_barcodes.append(barcode)
            
            existing = db.session.query(MarketplaceProduct).filter_by(
                user_id=user_id, 
                marketplace='n11', 
                barcode=barcode
            ).first()
            
            qty = 0
            price = 0.0
            stock_items = p.get('stockItems', [])
            if isinstance(stock_items, list) and stock_items:
                for si in stock_items:
                    qty += int(si.get('quantity', 0))
                    price = float(si.get('sellerStockCodePrice') or si.get('price', 0))
            else:
                 qty = int(p.get('quantity', 0))
                 price = float(p.get('salePrice') or p.get('listPrice', 0))

            if not existing:
                existing = MarketplaceProduct(
                    user_id=user_id,
                    marketplace='n11',
                    barcode=barcode
                )
                db.session.add(existing)

            existing.title = p.get('title', 'İsimsiz Ürün')
            existing.quantity = qty
            existing.price = price
            existing.sale_price = price
            existing.stock_code = p.get('sellerCode')
            
            n11_status = p.get('productStatus')
            existing.status = 'Aktif' if n11_status == 'Active' else 'Pasif'
            existing.on_sale = (n11_status == 'Active')
            
            if hasattr(existing, 'brand'):
                brand_data = p.get('brand')
                if isinstance(brand_data, dict):
                    existing.brand = brand_data.get('name')
                else:
                    existing.brand = str(brand_data) if brand_data else None

            if hasattr(existing, 'category_name'):
                cat_data = p.get('category')
                if isinstance(cat_data, dict):
                    existing.category_name = cat_data.get('name')
                else:
                    existing.category_name = str(cat_data) if cat_data else None
            
            images = p.get('images', [])
            if images and isinstance(images, list):
                 existing.image_url = images[0].get('url') if isinstance(images[0], dict) else images[0]

        db.session.commit()
        
        # Safe Cleanup: Only delete if we didn't have a massive failure during fetch
        # Let's count current products for this user/marketplace
        current_count = db.session.query(MarketplaceProduct).filter_by(user_id=user_id, marketplace='n11').count()
        
        # If remote_barcodes is much smaller than current_count, and we didn't expect it, abort deletion
        # (e.g. if we had 1000 items and now only 10, it looks suspicious)
        # Note: remote_barcodes reflects what we just fetched.
        if current_count > 50 and len(remote_barcodes) < (current_count * 0.5):
            warn_msg = f"N11 Temizlik İptal Edildi: Veritabanında {current_count} ürün var ancak sadece {len(remote_barcodes)} ürün çekilebildi. Güvenlik nedeniyle silme işlemi yapılmadı."
            logger.warning(f"[N11] {warn_msg}")
            if job_id: append_mp_job_log(job_id, warn_msg, level='warning')
            deleted_count = 0
        else:
            deleted_count = db.session.query(MarketplaceProduct).filter(
                MarketplaceProduct.user_id == user_id,
                MarketplaceProduct.marketplace == 'n11',
                ~MarketplaceProduct.barcode.in_(remote_barcodes)
            ).delete(synchronize_session=False)
            db.session.commit()

        final_msg = f"N11 senkronizasyonu tamamlandı: {len(products)} güncellendi, {deleted_count} silindi."
        logger.info(f"[N11] {final_msg}")
        if job_id:
            append_mp_job_log(job_id, final_msg)

        return {'success': True, 'count': len(products), 'deleted': deleted_count}

    except Exception as e:
        err_msg = f"N11 senkronizasyon hatası: {str(e)}"
        logger.error(f"[N11] {err_msg}")
        if job_id:
            append_mp_job_log(job_id, err_msg, level='error')
        db.session.rollback()
        return {'success': False, 'message': err_msg}


def perform_n11_direct_push_actions(user_id: int, to_update: List[Any], to_create: List[Any], to_zero: List[Any], src: Any, job_id: Optional[str] = None) -> Dict[str, Any]:
    """
    N11 için Direct Push aksiyonlarını gerçekleştirir.
    """
    from app.services.job_queue import append_mp_job_log, append_mp_job_logs, get_mp_job, update_mp_job, update_job_progress
    from app.utils.helpers import calculate_price, chunked
    from app.models import MarketplaceProduct
    from app import db
    import json
    
    client = get_n11_client(user_id=user_id)
    res = {'updated_count': 0, 'created_count': 0, 'zeroed_count': 0}
    
    # Dynamic settings
    shipment_template = Setting.get("N11_DEFAULT_SHIPMENT_TEMPLATE", "Standart", user_id=user_id)
    default_brand = Setting.get("N11_DEFAULT_BRAND", "Vidos", user_id=user_id)
    
    # Barcode Settings (Sync Page & General)
    auto_gen_empty = Setting.get("N11_AUTO_GENERATE_BARCODE", "0", user_id=user_id) == "1" or \
                     Setting.get("AUTO_SYNC_USE_RANDOM_BARCODE_n11", "false", user_id=user_id) == "true" or \
                     (src and src.use_random_barcode)
    auto_gen_all = Setting.get("N11_OVERWRITE_BARCODE_ALL", "0", user_id=user_id) == "1" or \
                   Setting.get("AUTO_SYNC_USE_OVERRIDE_BARCODE_n11", "false", user_id=user_id) == "true"

    total_ops = len(to_update or []) + len(to_create or []) + len(to_zero or [])
    completed_ops = 0
    if job_id:
        update_job_progress(job_id, 0, total_ops, 'İşlemler başlatılıyor...')
    
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
            
            final_price, rule_desc = calculate_price(xml_item.price, 'n11', user_id=user_id, return_details=True)
            final_price = round(final_price, 2)
            
            update_payloads.append({
                "stockCode": local_item.stock_code,
                "salePrice": final_price,
                "listPrice": final_price,
                "quantity": xml_item.quantity,
                "currencyType": "TL"
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
                
                # Retry loop for API Limits
                max_retries = 3
                retry_delay = 300
                
                for attempt in range(max_retries):
                    try:
                        client.update_products_price_and_stock(batch)
                        
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
                        
                        break # Success
                        
                    except Exception as e:
                        if "talep limitiniz dolmuştur" in str(e).lower():
                            if attempt < max_retries - 1:
                                if job_id:
                                    append_mp_job_log(job_id, f"⚠️ N11 API Limiti doldu. {retry_delay} sn bekleniyor... (Deneme {attempt+1}/{max_retries})", level='warning')
                                    update_job_progress(job_id, completed_ops, total_ops, "API Limiti Bekleniyor (5 dk)...")
                                time.sleep(retry_delay)
                                continue
                            else:
                                if job_id: append_mp_job_log(job_id, "API limiti aşıldı, işlem iptal ediliyor.", level='error')
                                raise e
                        else:
                            if job_id: append_mp_job_log(job_id, f"Batch hatası: {e}", level='error')
                            raise e
                        
                res['updated_count'] += len(batch)
            
        except Exception as e:
            db.session.rollback()
            if job_id: append_mp_job_log(job_id, f"N11 güncelleme hatası: {str(e)}", level='error')

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
            
            # Application of Barcode Rules
            if auto_gen_all:
                barcode = generate_random_barcode()
            elif auto_gen_empty and (not barcode or barcode.strip() == "" or barcode == "0" or barcode.lower() == "bgz0"):
                barcode = generate_random_barcode()
            
            raw = json.loads(xml_item.raw_data)
            final_price, rule_desc = calculate_price(xml_item.price, 'n11', user_id=user_id, return_details=True)
            
            safe_title = (xml_item.title or "").strip()
            if len(safe_title) < 5: safe_title = f"{safe_title} - Ürün"
            if len(safe_title) > 100: safe_title = safe_title[:100]
 
            item_payload = {
                "title": safe_title,
                "description": raw.get('details') or raw.get('description') or xml_item.title,
                "categoryId": None, # Will be filled below
                "salePrice": round(final_price, 2),
                "listPrice": round(final_price, 2),
                "vatRate": 20,
                "currencyType": "TL",
                "images": [{"url": img['url'], "order": i+1} for i, img in enumerate(raw.get('images', [])) if img.get('url')][:8],
                "quantity": xml_item.quantity,
                "stockCode": xml_item.stock_code,
                "barcode": barcode,
                "productMainId": xml_item.stock_code,
                "shipmentTemplate": shipment_template,
                "preparingDay": 3,
                "attributes": [] # Mandatory
            }
            
            # Find category for the product
            from app.services.n11_service import find_matching_n11_category
            match = find_matching_n11_category(xml_item.title, user_id=user_id, job_id=job_id)
            if not match:
                if job_id: append_mp_job_log(job_id, f"[ATLADI] {xml_item.stock_code} için N11 kategorisi bulunamadı.", level='warning')
                continue
            
            item_payload['categoryId'] = int(match['id'])
            
            # Brand Attribute (Mandatory ID 1)
            item_payload['attributes'].append({
                "id": 1,
                "valueId": None,
                "customValue": default_brand
            })

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
                # Filter out skipped items (redundant now but safe)
                final_payloads = [p for p in payloads if p is not None]
                if not final_payloads:
                    completed_ops += len(batch) # Still count for progress even if all skipped
                    if job_id:
                        update_job_progress(job_id, completed_ops, total_ops, f"Yeni Ürünler Ekleniyor ({completed_ops}/{total_ops})...")
                    continue

                # Retry Loop for Create
                max_retries = 3
                retry_delay = 300
                
                for attempt in range(max_retries):
                    try:
                        res_api = client.create_products(final_payloads)
                        
                        # Check for Limit Error in Response Body
                        err_msg = str(res_api.get('result', {}).get('errorMessage', ''))
                        if "talep limitiniz dolmuştur" in err_msg.lower():
                            if attempt < max_retries - 1:
                                if job_id:
                                    append_mp_job_log(job_id, f"⚠️ N11 API Limiti doldu (Body). {retry_delay} sn bekleniyor... (Deneme {attempt+1}/{max_retries})", level='warning')
                                    update_job_progress(job_id, completed_ops, total_ops, "API Limiti Bekleniyor (5 dk)...")
                                time.sleep(retry_delay)
                                continue
                            # If retries exhausted, let it fall through to normal error handling or break
                        
                        task_id = res_api.get('taskId') or res_api.get('id')
                        
                        # Check for API level error
                        if res_api.get('result', {}).get('status') == 'ERROR' or res_api.get('status') == 'REJECT':
                            err_msg = res_api.get('result', {}).get('errorMessage') or \
                                     res_api.get('message') or \
                                     (", ".join(res_api.get('reasons', [])) if res_api.get('reasons') else 'Bilinmeyen API Hatası')
                            
                            # If it was limit error caught above but retries exhausted, we log it here
                            if "talep limitiniz dolmuştur" in err_msg.lower():
                                if job_id: append_mp_job_log(job_id, "API limiti aşıldı (Create), işlem iptal ediliyor.", level='error')
                                raise Exception(err_msg)
                            
                            # Normal Error
                            if job_id: append_mp_job_log(job_id, f"Batch isteği reddedildi: {err_msg}", level='error')
                            break # Non-retryable error
                        
                        break # Success
                        
                    except Exception as e:
                        if "talep limitiniz dolmuştur" in str(e).lower():
                            if attempt < max_retries - 1:
                                if job_id:
                                    append_mp_job_log(job_id, f"⚠️ N11 API Limiti doldu (Exception). {retry_delay} sn bekleniyor... (Deneme {attempt+1}/{max_retries})", level='warning')
                                    update_job_progress(job_id, completed_ops, total_ops, "API Limiti Bekleniyor (5 dk)...")
                                time.sleep(retry_delay)
                                continue
                            else:
                                if job_id: append_mp_job_log(job_id, "API limiti aşıldı (Create), işlem iptal ediliyor.", level='error')
                                raise e
                        else:
                            if job_id: append_mp_job_log(job_id, f"Create Batch Exception: {e}", level='error')
                            raise e
                    
                    if job_id: append_mp_job_log(job_id, f"N11 API Hatası (Batch): {err_msg}", level='error')
                    completed_ops += len(batch)
                    if job_id:
                        update_job_progress(job_id, completed_ops, total_ops, f"Yeni Ürünler Ekleniyor ({completed_ops}/{total_ops})...")
                    continue # Skip DB update for this failed batch

                # Bulk DB Create
                new_mps = []
                batch_logs = []
                processed_barcodes = set()
                
                for i, (item_payload, xml_record, r_desc) in enumerate(batch):
                    if payloads[i] is None: continue # Was skipped
                    
                    barcode = item_payload['barcode']

                    # Duplicate check for safety (DB level + In-Batch level)
                    existing = MarketplaceProduct.query.filter_by(user_id=user_id, marketplace='n11', barcode=barcode).first()
                    
                    if not existing and barcode not in processed_barcodes:
                        processed_barcodes.add(barcode)
                        new_mps.append(MarketplaceProduct(
                            user_id=user_id, marketplace='n11', barcode=barcode,
                            stock_code=xml_record.stock_code, title=xml_record.title,
                            price=xml_record.price, # Base
                            sale_price=item_payload.get('salePrice') or item_payload.get('listPrice'), # Calculated
                            quantity=item_payload['quantity'], status='Pending', on_sale=True,
                            xml_source_id=src.id
                        ))
                        if job_id:
                            batch_logs.append(f"[YENİ] {xml_record.stock_code} kuyruğa alındı (Task: {task_id}). Fiyat: {item_payload.get('salePrice')} ({r_desc}), Stok: {xml_record.quantity}")
                
                if new_mps:
                    db.session.bulk_save_objects(new_mps)
                    db.session.commit()
                
                if job_id and batch_logs:
                    append_mp_job_logs(job_id, batch_logs)

                res['created_count'] += len(final_payloads)
                completed_ops += len(batch) # Use original batch size for progress consistency
                if job_id:
                    update_job_progress(job_id, completed_ops, total_ops, f"Yeni Ürünler Ekleniyor ({completed_ops}/{total_ops})...")
            except Exception as e:
                db.session.rollback()
                if job_id: append_mp_job_log(job_id, f"N11 ürün oluşturma hatası: {str(e)}", level='error')
    # --- 3. STOK SIFIRLAMA (Zero) ---
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
                "sellerCode": local_item.stock_code,
                "price": local_item.sale_price,
                "quantity": 0
            })
            zero_mappings.append({'id': local_item.id, 'quantity': 0})

        try:
            for i, batch in enumerate(chunked(zero_payloads, 100)):
                if job_id and i % 5 == 0:
                    js = get_mp_job(job_id)
                    if js and js.get('cancel_requested'):
                        append_mp_job_log(job_id, "İptal edildi (Zero sırasında)", level='warning')
                        return res
                
                # Retry Loop for Zero
                max_retries = 3
                retry_delay = 300
                
                for attempt in range(max_retries):
                    try:
                        client.update_products_price_and_stock(batch)
                        res['zeroed_count'] += len(batch)
                        
                        batch_mappings = zero_mappings[i*100 : (i+1)*100]
                        db.session.bulk_update_mappings(MarketplaceProduct, batch_mappings)
                        db.session.commit()
                        
                        completed_ops += len(batch)
                        if job_id:
                            update_job_progress(job_id, completed_ops, total_ops, f"Stoklar Sıfırlanıyor ({completed_ops}/{total_ops})...")
                        
                        break # Success
                        
                    except Exception as e:
                        if "talep limitiniz dolmuştur" in str(e).lower():
                            if attempt < max_retries - 1:
                                if job_id:
                                    append_mp_job_log(job_id, f"⚠️ N11 API Limiti doldu (Zero). {retry_delay} sn bekleniyor... (Deneme {attempt+1}/{max_retries})", level='warning')
                                    update_job_progress(job_id, completed_ops, total_ops, "API Limiti Bekleniyor (5 dk)...")
                                time.sleep(retry_delay)
                                continue
                            else:
                                if job_id: append_mp_job_log(job_id, "API limiti aşıldı (Zero), işlem iptal ediliyor.", level='error')
                                raise e
                        else:
                            if job_id: append_mp_job_log(job_id, f"Zero Batch hatası: {e}", level='error')
                            raise e
        except Exception as e:
            db.session.rollback()
            if job_id: append_mp_job_log(job_id, f"N11 stok sıfırlama hatası: {str(e)}", level='error')

    return res

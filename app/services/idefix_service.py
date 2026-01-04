import base64
import json
import time
import logging
import requests
from typing import List, Dict, Optional, Any
from datetime import datetime
from app.utils.helpers import chunked, get_marketplace_multiplier, to_int, to_float, clean_forbidden_words, is_product_forbidden, calculate_price
from app.utils.rate_limiter import idefix_limiter

from app.services.job_queue import append_mp_job_log, get_mp_job, update_mp_job
from app.models import Setting, Product, SupplierXML

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO) # Keep INFO but will demote specific logs


# TF-IDF imports (optional, graceful fallback)
try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    logger.warning("sklearn yüklü değil, TF-IDF kategori eşleştirme çalışmayacak")

# Idefix Category TF-IDF Cache
_IDEFIX_CAT_TFIDF = {
    "leaf": [],
    "names": [],
    "vectorizer": None,
    "matrix": None
}

def prepare_idefix_tfidf(categories: List[Dict[str, Any]]):
    """
    Prepare TF-IDF vectorizer and matrix for Idefix categories.
    Only uses LEAF categories (categories without subcategories).
    """
    if not SKLEARN_AVAILABLE:
        logger.warning("sklearn yüklü değil, TF-IDF eşleştirme çalışmayacak")
        _IDEFIX_CAT_TFIDF.update({"leaf": [], "names": [], "vectorizer": None, "matrix": None})
        return
    
    # Filter to only LEAF categories (no subs or empty subs)
    leaf_categories = []
    for cat in categories:
        subs = cat.get('subs', [])
        # Is leaf if subs is empty or None
        if not subs or len(subs) == 0:
            leaf_categories.append(cat)
    
    logger.info(f"[IDEFIX] {len(categories)} kategoriden {len(leaf_categories)} tanesi yaprak (leaf)")
    
    names = [c.get('name', '') for c in leaf_categories if c.get('name')]
    if not names:
        _IDEFIX_CAT_TFIDF.update({"leaf": [], "names": [], "vectorizer": None, "matrix": None})
        return
    
    # Use char-level n-grams for Turkish/fuzzy matching
    vec = TfidfVectorizer(analyzer='char_wb', ngram_range=(2, 4))
    vec.fit(names)
    mat = vec.transform(names)
    _IDEFIX_CAT_TFIDF.update({"leaf": leaf_categories, "names": names, "vectorizer": vec, "matrix": mat})
    logger.info(f"Idefix TF-IDF matrisi hazır: {len(names)} yaprak kategori")

def ensure_idefix_tfidf_ready(user_id: int = None) -> bool:
    """
    Load Idefix categories from settings and prepare TF-IDF if not already done.
    If cache is empty, automatically fetch from API.
    Uses IDEFIX_CACHE_TIMESTAMP to detect if reload is needed across workers.
    """
    from app.models import Setting
    global _IDEFIX_CAT_TFIDF

    # Check if we need to reload due to external clear or first load
    if not hasattr(ensure_idefix_tfidf_ready, 'last_load_ts'):
        ensure_idefix_tfidf_ready.last_load_ts = 0.0

    db_ts_str = Setting.get("IDEFIX_CACHE_TIMESTAMP", "0", user_id=user_id)
    try:
        db_ts = float(db_ts_str)
    except:
        db_ts = 0.0

    needs_reload = False
    if db_ts > ensure_idefix_tfidf_ready.last_load_ts:
        needs_reload = True
        # Force clear memory cache if reload needed
        _IDEFIX_CAT_TFIDF.clear()
        logger.info(f"[IDEFIX] Cache reload triggered. DB version: {db_ts}, Mem version: {ensure_idefix_tfidf_ready.last_load_ts}")
    
    if _IDEFIX_CAT_TFIDF.get('vectorizer'):
        return True
    
    # Try loading from saved settings first
    raw = Setting.get("IDEFIX_CATEGORY_TREE", "", user_id=user_id)
    if raw:
        try:
            categories = json.loads(raw)
            if categories:
                prepare_idefix_tfidf(categories)
                # Update load timestamp
                ensure_idefix_tfidf_ready.last_load_ts = time.time()
                return True
        except Exception as e:
            logger.error(f"Idefix kategori ağacı yüklenirken hata: {e}")
    
    # If cache is still empty, automatically fetch from API
    logger.info("[IDEFIX] Kategori önbelleği boş veya geçersiz, API'den otomatik çekiliyor...")
    try:
        result = fetch_and_cache_categories(user_id=user_id)
        if result.get('success'):
            # Now try to load again
            raw = Setting.get("IDEFIX_CATEGORY_TREE", "", user_id=user_id)
            if raw:
                categories = json.loads(raw)
                prepare_idefix_tfidf(categories)
                # Update load timestamp after successful API fetch
                ensure_idefix_tfidf_ready.last_load_ts = time.time()
                logger.info(f"[IDEFIX] {len(categories)} kategori otomatik yüklendi")
                return True
    except Exception as e:
        logger.error(f"[IDEFIX] Kategori otomatik çekme hatası: {e}")
    
    return False

def match_idefix_category_tfidf(query: str, min_score: float = 0.15) -> Optional[int]:
    """
    Find best matching Idefix category using TF-IDF + cosine similarity.
    
    Args:
        query: The text to match (product title, category name, etc.)
        min_score: Minimum similarity score (0-1) to accept a match
        
    Returns:
        Category ID if found, None otherwise
    """
    if not query or not _IDEFIX_CAT_TFIDF.get('vectorizer'):
        return None
    
    vec = _IDEFIX_CAT_TFIDF['vectorizer']
    mat = _IDEFIX_CAT_TFIDF['matrix']
    leaf = _IDEFIX_CAT_TFIDF['leaf']
    
    try:
        q = vec.transform([query.lower()])
        sims = cosine_similarity(q, mat)[0]
        idx = int(sims.argmax())
        score = float(sims[idx])
        
        if score >= min_score:
            cat_id = leaf[idx].get('id')
            cat_name = leaf[idx].get('name', '')
            logger.info(f"[IDEFIX] Kategori eşleşti (skor:{score:.2f}): {query[:30]}... -> {cat_name}")
            return cat_id
        return None
    except Exception as e:
        logger.error(f"[IDEFIX] TF-IDF eşleştirme hatası: {e}")
        return None

def resolve_idefix_category(product_title: str, excel_category: str, log_callback=None, user_id: int = None) -> Optional[int]:
    """
    Resolve Idefix category ID from product info.
    
    Args:
        product_title: Product title
        excel_category: Category from Excel
        log_callback: Optional callback function for logging
        user_id: User ID for setting context
        
    Returns:
        Category ID if found, None otherwise
    """
    if not ensure_idefix_tfidf_ready(user_id=user_id):
        if log_callback:
            log_callback("Idefix kategori listesi boş! Ayarlardan 'Kategorileri Çek' yapın.", level='warning')
        return None
    
    # Try with category name first, then product title
    for query in (excel_category, product_title):
        if not query:
            continue
        
        cat_id = match_idefix_category_tfidf(query, min_score=0.10)
        if cat_id:
            if log_callback:
                # Get category name for logging
                for cat in _IDEFIX_CAT_TFIDF.get('leaf', []):
                    if cat.get('id') == cat_id:
                        log_callback(f"Kategori: {query[:25]}... -> {cat.get('name', '')}")
                        break
            return cat_id
    
    if log_callback:
        log_callback(f"Kategori eşleşmedi: {excel_category or product_title[:30]}", level='warning')
    
    return None

class IdefixClient:
    """
    Idefix API Client for product inventory and price management.
    """
    
    BASE_URL = "https://merchantapi.idefix.com"
    
    def __init__(self, api_key: str, api_secret: str, vendor_id: str):
        """
        Initialize Idefix API client.
        
        Args:
            api_key: Idefix API Key
            api_secret: Idefix API Secret
            vendor_id: Idefix vendor/seller ID
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.vendor_id = vendor_id
        self._auth_token = self._generate_auth_token()
        
        self.session = requests.Session()
        # Patch session.request
        original_request = self.session.request
        def rate_limited_request(method, url, *args, **kwargs):
            idefix_limiter.wait()
            return original_request(method, url, *args, **kwargs)
        self.session.request = rate_limited_request
        
    def _generate_auth_token(self) -> str:
        """
        Generate auth token for Authorization header.
        
        Using Basic Auth format: base64(ApiKey:ApiSecret)
        """
        auth_string = f"{self.api_key}:{self.api_secret}"
        token = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
        return token
    
    def get_token(self) -> str:
        """
        Satisfies the Connection Test call. Returns the auth token.
        """
        return self._auth_token
    
    def _get_headers(self) -> Dict[str, str]:
        """Get default headers for API requests."""
        vendor_token = base64.b64encode(f"{self.api_key}:{self.api_secret}".encode('utf-8')).decode('utf-8')
        return {
            'Content-Type': 'application/json',
            'X-API-KEY': vendor_token,
            'Accept': 'application/json'
        }
    
    def search_brand_by_name(self, brand_name: str) -> Optional[Dict[str, Any]]:
        """
        Search for a brand by name using İdefix API.
        
        Uses the official endpoint: /pim/brand/search-by-name?title={brandName}
        Documentation: https://developer.idefix.com/api/urun-entegrasyonu/marka-isim-arama
        
        Args:
            brand_name: Brand name to search for
            
        Returns:
            Brand dict if found, None otherwise
        """
        try:
            url = f"{self.BASE_URL}/pim/brand/search-by-name"
            params = {'title': brand_name.strip()}
            
            logger.info(f"[IDEFIX] Searching brand by name: {brand_name}")
            
            response = self.session.get(
                url,
                headers=self._get_headers(),
                params=params,
                timeout=30
            )
            response.raise_for_status()
            
            data = response.json()
            logger.info(f"[IDEFIX] Search response type: {type(data)}")
            
            brand = None
            if isinstance(data, list):
                if data:
                    brand = data[0]
                    logger.info(f"[IDEFIX] API returned list, using first item: {brand.get('title')}")
                else:
                    logger.warning("[IDEFIX] API returned empty list")
            elif isinstance(data, dict):
                brand = data
            
            if brand and brand.get('id'):
                logger.info(f"[IDEFIX] Brand found: {brand.get('title')} (ID: {brand.get('id')})")
                return brand
            else:
                logger.warning(f"[IDEFIX] Brand '{brand_name}' not found")
                return None
                
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                logger.warning(f"[IDEFIX] Brand '{brand_name}' not found (404)")
                return None
            logger.error(f"[IDEFIX] Brand search failed: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"[IDEFIX] Brand search error: {str(e)}")
            return None
    
    def update_inventory_and_price(
        self,
items: List[Dict[str, Any]],
        batch_callback: Optional[callable] = None
    ) -> Dict[str, Any]:
        """
        Update inventory and price for multiple products.
        
        Args:
            items: List of product items with inventory and price data
            batch_callback: Optional callback function to handle batch response
            
        Returns:
        """
        if not self.vendor_id:
            raise ValueError("Idefix vendor_id (Satıcı ID) eksik! Lütfen ayarları kontrol edin.")
            
        url = f"{self.BASE_URL}/pim/catalog/{self.vendor_id}/inventory-upload"
        
        processed_items = []
        for item in items:
            processed_item = item.copy()
            processed_item['price'] = int(float(item['price']) * 100)
            if 'comparePrice' in item and item['comparePrice']:
                processed_item['comparePrice'] = int(float(item['comparePrice']) * 100)
            else:
                processed_item['comparePrice'] = processed_item['price']
            processed_items.append(processed_item)
        
        payload = {"items": processed_items}
        
        try:
            response = self.session.post(
                url,
                headers=self._get_headers(),
                json=payload,
                timeout=30
            )
            
            response.raise_for_status()
            result = response.json()
            
            if batch_callback and 'batchRequestId' in result:
                batch_callback(result['batchRequestId'], result)
                
            return result
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Idefix API request failed: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response status: {e.response.status_code}")
                logger.error(f"Response body: {e.response.text}")
            raise
    
    def get_inventory_status(self, batch_request_id: str) -> Dict[str, Any]:
        """
        Get the status of an inventory update batch.
        
        Args:
            batch_request_id: The batch ID from update_inventory_and_price response
            
        Returns:
        """
        if not self.vendor_id:
            raise ValueError("Idefix vendor_id eksik!")
            
        url = f"{self.BASE_URL}/pim/catalog/{self.vendor_id}/inventory-result/{batch_request_id}"
        
        try:
            response = self.session.get(
                url,
                headers=self._get_headers(),
                timeout=30
            )
            
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get inventory status: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response status: {e.response.status_code}")
                logger.error(f"Response body: {e.response.text}")
            raise

    def get_categories(self) -> List[Dict[str, Any]]:
        """
        Get list of categories from Idefix.
        Endpoint: /pim/product-category
        The API returns a full tree structure (List), not paginated.
        """
        url = f"{self.BASE_URL}/pim/product-category"
        # API returns the whole tree, so we need a larger timeout.
        try:
            logger.info("[IDEFIX] Requesting category tree...")
            response = self.session.get(url, headers=self._get_headers(), timeout=120)
            
            # Better error reporting if not 200
            if response.status_code != 200:
                logger.error(f"[IDEFIX] get_categories failed: status {response.status_code}")
                # Log a bit of the body to see if it's HTML
                logger.error(f"[IDEFIX] Response start: {response.text[:200]}")
                response.raise_for_status()
                
            try:
                data = response.json()
            except json.JSONDecodeError as je:
                logger.error(f"[IDEFIX] JSON format error in categories: {je}")
                logger.error(f"[IDEFIX] Raw content (first 500 chars): {response.text[:500]}")
                raise ValueError("Idefix API geçersiz bir JSON yanıtı döndürdü. Detaylar için logları kontrol edin.")
                
            if isinstance(data, list):
                return data
            elif isinstance(data, dict) and 'content' in data:
                 # Fallback if they ever change it to paginated
                 return data['content']
            return data
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get categories: {str(e)}")
            raise

    def get_category_attributes(self, category_id: int) -> List[Dict[str, Any]]:
        """
        Get required attributes for a specific category.
        Endpoint: /pim/category-attribute/{categoryId}
        
        Returns list of attributes with:
        - attributeId: ID of the attribute
        - attributeTitle: Name of the attribute
        - required: Whether this attribute is mandatory
        - allowCustom: Whether custom values are allowed
        - attributeValues: List of possible values
        """
        url = f"{self.BASE_URL}/pim/category-attribute/{category_id}"
        
        try:
            logger.info(f"[IDEFIX] Fetching attributes for category {category_id}")
            logger.info(f"[IDEFIX] URL: {url}")
            response = self.session.get(url, headers=self._get_headers(), timeout=30)
            logger.info(f"[IDEFIX] Response status: {response.status_code}")
            
            response.raise_for_status()
            data = response.json()
            
            # Debug: Log raw response structure
            logger.info(f"[IDEFIX] Response type: {type(data)}")
            if isinstance(data, dict):
                logger.info(f"[IDEFIX] Response keys: {list(data.keys())}")
                # Try common keys
                for key in ['attributes', 'data', 'content', 'items', 'result']:
                    if key in data:
                        logger.info(f"[IDEFIX] Found '{key}' with {len(data[key]) if isinstance(data[key], list) else 'non-list'} items")
            elif isinstance(data, list):
                logger.info(f"[IDEFIX] Response is list with {len(data)} items")
                if data:
                    logger.info(f"[IDEFIX] First item keys: {list(data[0].keys()) if isinstance(data[0], dict) else 'not dict'}")
            
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                # Try various keys
                for key in ['attributes', 'data', 'content', 'items', 'result']:
                    if key in data and isinstance(data[key], list):
                        return data[key]
                # If it's a single category object with attributes
                if 'attributeId' in data or 'id' in data:
                    return [data]
            return []
        except requests.exceptions.RequestException as e:
            logger.error(f"[IDEFIX] Failed to get category attributes: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"[IDEFIX] Response body: {e.response.text[:500]}")
            return []

    def fast_list_products(self, products: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Fast List products on Idefix (Hızlı Ürün Ekleme).
        Endpoint: /pim/catalog/{vendorId}/fast-listing
        
        CRITICAL: Idefix API expects prices in TL (float), NOT kuruş!
        """
        if not self.vendor_id:
            raise ValueError("Idefix vendor_id eksik!")
            
        url = f"{self.BASE_URL}/pim/catalog/{self.vendor_id}/fast-listing"
        
        # NO CONVERSION - Idefix expects TL directly!
        processed = []
        for p in products:
            item = {
                'title': p.get('title'),
                'barcode': p.get('barcode'),
                'vendorStockCode': p.get('vendorStockCode'),
                'price': float(p.get('price', 0)),
                'comparePrice': float(p.get('comparePrice', 0)),
                'inventoryQuantity': int(p.get('inventoryQuantity', 0))
            }
            # Add brandId if available
            if p.get('brandId'):
                item['brandId'] = int(p.get('brandId'))
            
            processed.append(item)

        payload = {"items": processed}
        
        logger.info(f"[IDEFIX] Sending {len(processed)} products to fast-listing")
        
        try:
            response = self.session.post(url, headers=self._get_headers(), json=payload, timeout=30)
            response.raise_for_status()
            result = response.json()
            logger.info(f"[IDEFIX] Success! BatchRequestId: {result.get('batchRequestId')}")
            return result
        except requests.exceptions.RequestException as e:
            logger.error(f"[IDEFIX] Failed to fast list products: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"[IDEFIX] Response status: {e.response.status_code}")
                logger.error(f"[IDEFIX] Response body: {e.response.text}")
            raise

    def create_products(self, products: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Create new products in Idefix Pool (Ürün Havuzu).
        Endpoint: POST /pim/pool/{vendorId}/create
        
        Mandatory Fields in products:
        - barcode, title, productMainId, brandId, categoryId, 
        - inventoryQuantity, vendorStockCode, description, price, vatRate, images
        """
        if not self.vendor_id:
            raise ValueError("Idefix vendor_id eksik!")
            
        url = f"{self.BASE_URL}/pim/pool/{self.vendor_id}/create"
        
        payload = {"items": products}
        
        logger.info(f"[IDEFIX] Creating {len(products)} products in pool")
        
        try:
            response = self.session.post(url, headers=self._get_headers(), json=payload, timeout=60)
            response.raise_for_status()
            result = response.json()
            logger.info(f"[IDEFIX] Create Success! BatchRequestId: {result.get('batchRequestId')}")
            return result
        except requests.exceptions.RequestException as e:
            logger.error(f"[IDEFIX] Failed to create products in pool: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"[IDEFIX] Response status: {e.response.status_code}")
                logger.error(f"[IDEFIX] Response body: {e.response.text}")
            raise
    
    def query_pool_batch_status(self, batch_request_id: str) -> Dict[str, Any]:
        """
        Query the status of a pool/create batch request.
        Endpoint: /pim/pool/{vendorId}/batch-result/{batchRequestId}
        
        Returns:
        """
        if not self.vendor_id:
            return {}
            
        url = f"{self.BASE_URL}/pim/pool/{self.vendor_id}/batch-result/{batch_request_id}"
        
        logger.info(f"[IDEFIX] Querying pool batch status: {batch_request_id}")
        
        try:
            response = self.session.get(url, headers=self._get_headers(), timeout=20)
            response.raise_for_status()
            result = response.json()
            
            logger.info(f"[IDEFIX] Pool batch result: {json.dumps(result, ensure_ascii=False)[:500]}")
            return result
        except requests.exceptions.RequestException as e:
            logger.error(f"[IDEFIX] Failed to query pool batch status: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"[IDEFIX] Response: {e.response.text[:500]}")
            return {}

    def query_batch_status(self, batch_request_id: str) -> Dict[str, Any]:
        """
        Query the status of a batch request.
        
        Args:
            batch_request_id: The batchRequestId returned from fast_list_products
            
        Returns:
        """
        if not self.vendor_id:
            return {}
            
        url = f"{self.BASE_URL}/pim/catalog/{self.vendor_id}/fast-listing-result/{batch_request_id}"
        
        logger.info(f"[IDEFIX] Querying batch status: {batch_request_id}")
        
        try:
            response = self.session.get(url, headers=self._get_headers(), timeout=20)
            response.raise_for_status()
            result = response.json()
            
            status = result.get('status', 'UNKNOWN')
            items = result.get('items', [])
            logger.info(f"[IDEFIX] Batch status: {status}, Items: {len(items)}")
            
            for item in items:
                pool_state = item.get('poolState', 'unknown')
                barcode = item.get('barcode', 'N/A')
                logger.info(f"[IDEFIX]   - {barcode}: {pool_state}")
            
            return result
        except requests.exceptions.RequestException as e:
            logger.error(f"[IDEFIX] Failed to query batch status: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"[IDEFIX] Response status: {e.response.status_code}")
                logger.error(f"[IDEFIX] Response body: {e.response.text}")
            raise
            
    def get_full_pool_list(self) -> List[Dict[str, Any]]:
        """
        Fetch all products from Idefix pool to get current inventory/matching state.
        Endpoint: /pim/pool/{vendorId}/list
        """
        if not self.vendor_id: return []
        url = f"{self.BASE_URL}/pim/pool/{self.vendor_id}/list"
        all_items = []
        page = 1
        limit = 100
        
        while True:
            try:
                params = {"page": page, "limit": limit}
                resp = self.session.get(url, headers=self._get_headers(), params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                items = data.get('items', [])
                if not items: break
                
                all_items.extend(items)
                if len(items) < limit: break
                page += 1
                if page > 500: break
            except Exception as e:
                logger.error(f"[IDEFIX] Pool list fetch error: {e}")
                break
        return all_items

    def approve_pool_item(self, barcode: str) -> Dict[str, Any]:
        """
        Approve an item in the pool (waiting_vendor_approve state).
        Endpoint: /pim/pool/{vendorId}/approve-item
        """
        if not self.vendor_id: return {}
        url = f"{self.BASE_URL}/pim/pool/{self.vendor_id}/approve-item"
        payload = {"barcodes": [barcode]}
        try:
            resp = self.session.post(url, headers=self._get_headers(), json=payload, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"[IDEFIX] Pool item approve error for {barcode}: {e}")
            return {"success": False, "error": str(e)}

    def get_orders(self, page: int = 1, **kwargs) -> Dict[str, Any]:
        """
        Fetch orders from Idefix OMS API.
        Endpoint: /oms/{vendorId}/list
        
        Args:
            page: Page number (1-indexed default)
            limit: Page size
            startDate, endDate: Format 'YYYY/MM/DD HH:mm:ss'
        """
        if not self.vendor_id:
            logger.error("[IDEFIX] get_orders failed: vendor_id is missing")
            return {"items": [], "totalCount": 0}

        limit = kwargs.get('limit') or kwargs.get('size') or 50
        start_date = kwargs.get('startDate') or kwargs.get('start_date')
        end_date = kwargs.get('endDate') or kwargs.get('end_date')
        status = kwargs.get('status') or kwargs.get('state')
        
        # New OMS Endpoint
        url = f"{self.BASE_URL}/oms/{self.vendor_id}/list"
        
        params = {"page": page, "limit": limit}
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        if status:
            params["state"] = status
            
        try:
            logger.info(f"[IDEFIX] Fetching OMS orders from: {url}")
            resp = self.session.get(url, headers=self._get_headers(), params=params, timeout=20)
            
            if resp.status_code == 200:
                result = resp.json()
                return result # Returns { "items": [...], "totalCount": X, ... }
            
            logger.error(f"[IDEFIX] get_orders failed with status {resp.status_code}: {resp.text}")
            return {"items": [], "totalCount": 0}
        except Exception as e:
            logger.error(f"Idefix get_orders error: {e}")
            return {"items": [], "totalCount": 0}

    def update_order_status(self, order_id: str, status: str) -> Dict[str, Any]:
        """
        Update order status.
        
        Args:
            order_id: Order ID
            status: New status (e.g., 'Preparing', 'Shipped', 'Delivered')
            
        Returns:
            API response
        """
        url = f"{self.BASE_URL}/api/siparis-entegrasyonu/siparis-statu-guncelleme"
        
        payload = {
            "orderId": order_id,
            "status": status
        }
        
        logger.info(f"[IDEFIX] Updating order {order_id} status to {status}")
        
        try:
            resp = self.session.post(url, headers=self._get_headers(), json=payload, timeout=20)
            resp.raise_for_status()
            return resp.json() if resp.text else {"success": True}
        except Exception as e:
            logger.error(f"[IDEFIX] Order status update error: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"[IDEFIX] Response: {e.response.text}")
            raise

    def send_invoice_link(self, order_id: str, invoice_url: str) -> Dict[str, Any]:
        """
        Send invoice link for an order.
        
        Args:
            order_id: Order ID
            invoice_url: Invoice URL
            
        Returns:
            API response
        """
        url = f"{self.BASE_URL}/api/siparis-entegrasyonu/fatura-linki-gonderme"
        
        payload = {
            "orderId": order_id,
            "invoiceUrl": invoice_url
        }
        
        logger.info(f"[IDEFIX] Sending invoice link for order {order_id}")
        
        try:
            resp = self.session.post(url, headers=self._get_headers(), json=payload, timeout=20)
            resp.raise_for_status()
            return resp.json() if resp.text else {"success": True}
        except Exception as e:
            logger.error(f"[IDEFIX] Invoice link error: {e}")
            raise

    def update_shipment_info(
        self, 
        order_id: str, 
        cargo_company: str, 
        tracking_number: str
    ) -> Dict[str, Any]:
        """
        Update shipment/cargo information.
        
        Args:
            order_id: Order ID
            cargo_company: Cargo company name
            tracking_number: Tracking number
            
        Returns:
            API response
        """
        url = f"{self.BASE_URL}/api/siparis-entegrasyonu/gonderi-bilgisi-degistirme"
        
        payload = {
            "orderId": order_id,
            "cargoCompany": cargo_company,
            "trackingNumber": tracking_number
        }
        
        logger.info(f"[IDEFIX] Updating shipment info for order {order_id}")
        
        try:
            resp = self.session.post(url, headers=self._get_headers(), json=payload, timeout=20)
            resp.raise_for_status()
            return resp.json() if resp.text else {"success": True}
        except Exception as e:
            logger.error(f"[IDEFIX] Shipment update error: {e}")
            raise

    def get_returns(self, page: int = 0, size: int = 50) -> Dict[str, Any]:
        """
        Get list of return requests.
        
        Args:
            page: Page number
            size: Page size
            
        Returns:
            API response with returns list
        """
        url = f"{self.BASE_URL}/api/siparis-entegrasyonu/iade-listesi"
        
        params = {"page": page, "size": size}
        
        logger.info(f"[IDEFIX] Fetching returns list: page={page}")
        
        try:
            resp = self.session.get(url, headers=self._get_headers(), params=params, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"[IDEFIX] Get returns error: {e}")
            raise

    def approve_return(self, return_id: str) -> Dict[str, Any]:
        """
        Approve a return request.
        
        Args:
            return_id: Return request ID
            
        Returns:
            API response
        """
        url = f"{self.BASE_URL}/api/siparis-entegrasyonu/iade-onaylama"
        
        payload = {"returnId": return_id}
        
        logger.info(f"[IDEFIX] Approving return {return_id}")
        
        try:
            resp = self.session.post(url, headers=self._get_headers(), json=payload, timeout=20)
            resp.raise_for_status()
            return resp.json() if resp.text else {"success": True}
        except Exception as e:
            logger.error(f"[IDEFIX] Approve return error: {e}")
            raise

    def reject_return(self, return_id: str, reason_id: int) -> Dict[str, Any]:
        """
        Reject a return request.
        
        Args:
            return_id: Return request ID
            reason_id: Rejection reason ID
            
        Returns:
            API response
        """
        url = f"{self.BASE_URL}/api/siparis-entegrasyonu/iade-ret-talep-bildirimi"
        
        payload = {
            "returnId": return_id,
            "reasonId": reason_id
        }
        
        logger.info(f"[IDEFIX] Rejecting return {return_id} with reason {reason_id}")
        
        try:
            resp = self.session.post(url, headers=self._get_headers(), json=payload, timeout=20)
            resp.raise_for_status()
            return resp.json() if resp.text else {"success": True}
        except Exception as e:
            logger.error(f"[IDEFIX] Reject return error: {e}")
            raise

    def get_return_rejection_reasons(self) -> List[Dict[str, Any]]:
        """
        Get list of return rejection reasons.
        GET /api/siparis-entegrasyonu/iade-ret-nedenleri-listesi
        """
        url = f"{self.BASE_URL}/api/siparis-entegrasyonu/iade-ret-nedenleri-listesi"
        
        try:
            resp = self.session.get(url, headers=self._get_headers(), timeout=20)
            resp.raise_for_status()
            
            # API might return a list directly or wrapped in a dict
            data = resp.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                # Try common keys if wrapped
                for key in ['reasons', 'data', 'items', 'content']:
                    if key in data and isinstance(data[key], list):
                        return data[key]
                # If just a dict but not list, maybe single object? Unlikely for "list".
                # If wrapped in invalid way, return empty.
                
            return []
        except Exception as e:
            logging.error(f"[IDEFIX] Get return rejection reasons error: {e}")
            return []

    def split_order(self, order_id: str, split_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        POST /api/siparis-entegrasyonu/siparis-bolme
        Split an order into multiple packages.
        """
        url = f"{self.BASE_URL}/api/siparis-entegrasyonu/siparis-bolme"
        
        payload = {
            "orderId": order_id,
            **split_data
        }
        
        try:
            resp = self.session.post(url, headers=self._get_headers(), json=payload, timeout=20)
            resp.raise_for_status()
            return resp.json() if resp.text else {"success": True}
        except Exception as e:
            logger.error(f"[IDEFIX] Split order error: {e}")
            raise

    def mark_cannot_supply(self, order_id: str, items: List[str]) -> Dict[str, Any]:
        """
        POST /api/siparis-entegrasyonu/tedarik-edilemedi
        Mark items as cannot supply.
        """
        url = f"{self.BASE_URL}/api/siparis-entegrasyonu/tedarik-edilemedi"
        
        payload = {
            "orderId": order_id,
            "items": items
        }
        
        try:
            resp = self.session.post(url, headers=self._get_headers(), json=payload, timeout=20)
            resp.raise_for_status()
            return resp.json() if resp.text else {"success": True}
        except Exception as e:
            logger.error(f"[IDEFIX] Cannot supply error: {e}")
            raise

    def get_product_count(self) -> int:
        """
        Get total product count from Idefix across all status pools.
        """
        total_global = 0
        POOL_STATES = ["APPROVED", "WAITING_APPROVAL", "REJECTED", "WAITING_CONTENT", "DELETED"]
        
        for state in POOL_STATES:
            try:
                # Fetch only 1 item to get the totalElements metadata
                result = self.list_products(page=0, limit=1, pool_state=state)
                total_global += int(result.get('totalElements', 0))
            except Exception as e:
                logger.error(f"Idefix get_product_count error for state {state}: {e}")
                
        return total_global

    def list_products(
        self,
        page: int = 0,
        **kwargs
    ) -> Dict[str, Any]:
        """
        List products from Idefix pool.
        Endpoint: /pim/pool/{vendorId}/list
        
        Supports both 'limit' and 'size' as keyword arguments.
        
        Args:
            page: Page number (0-indexed)
            limit: Number of items per page (max 100-500)
            search: Search query (barcode, title, etc.)
            pool_state: Filter by pool state (WAITING_APPROVAL, APPROVED, etc.)
            
        Returns:
            Dict with 'content' (list of products) and 'totalElements' (totalcount)
        """
        limit = kwargs.get('limit') or kwargs.get('size') or 50
        search = kwargs.get('search')
        pool_state = kwargs.get('pool_state') or kwargs.get('poolState')
        if not self.vendor_id:
            logger.error("[IDEFIX] list_products failed: vendor_id is missing")
            return {'content': [], 'totalElements': 0}
            
        if not self.vendor_id:
            logger.error("[IDEFIX] list_products failed: vendor_id is missing")
            return {'content': [], 'totalElements': 0}
            
        url = f"{self.BASE_URL}/pim/pool/{self.vendor_id}/list"
        params = {"page": page, "limit": limit}
        if search:
            params["search"] = search
        if pool_state:
            params["poolState"] = pool_state
            
        try:
            resp = self.session.get(url, headers=self._get_headers(), params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            # Idefix returns { "items": [...], "totalElements": X }
            return {
                'content': data.get('items', []),
                'totalElements': data.get('totalElements', 0)
            }
        except Exception as e:
            logger.error(f"[IDEFIX] list_products error: {e}")
            return {'content': [], 'totalElements': 0}

    # ============================================================
    # Müşteri Soruları (Customer Questions)
    # ============================================================
    
    def get_product_questions(self, page: int = 1, limit: int = 50) -> Dict[str, Any]:
        """
        GET /pim/vendor/{vendorId}/question/filter
        Fetch questions asked by customers.
        """
        if not self.vendor_id:
            logger.error("[IDEFIX] get_product_questions failed: vendor_id is missing")
            return {"questions": []}

        url = f"{self.BASE_URL}/pim/vendor/{self.vendor_id}/question/filter"
        params = {"page": page, "limit": limit}

        try:
            logger.info(f"[IDEFIX] Fetching questions: {url}")
            resp = self.session.get(url, headers=self._get_headers(), params=params, timeout=20)
            
            if resp.status_code == 200:
                data = resp.json()
                # Unified format for api_questions.py
                if isinstance(data, list):
                     return {"questions": data}
                elif isinstance(data, dict):
                    if 'items' in data: return {"questions": data['items']}
                    if 'content' in data: return {"questions": data['content']}
                    return {"questions": [data]}
                return {"questions": []}
            
            logger.error(f"[IDEFIX] get_questions failed: {resp.status_code} {resp.text}")
            return {"questions": []}
        except Exception as e:
            logger.error(f"[IDEFIX] get_questions error: {e}")
            return {"questions": []}

    def answer_product_question(self, question_id: str, answer_text: str) -> Dict[str, Any]:
        """
        POST /pim/vendor/{vendorId}/question/{questionId}/answer
        """
        if not self.vendor_id:
            raise ValueError("Idefix vendor_id eksik!")
            
        url = f"{self.BASE_URL}/pim/vendor/{self.vendor_id}/question/{question_id}/answer"
        payload = {"answer_body": answer_text}
        
        try:
            resp = self.session.post(url, headers=self._get_headers(), json=payload, timeout=20)
            resp.raise_for_status()
            return resp.json() if resp.text else {"success": True}
        except Exception as e:
             logger.error(f"[IDEFIX] answer_question error: {e}")
             raise
        params = {"page": page, "limit": limit}
        
        if search:
            # If search looks like a barcode (digits only), pass as barcode param too
            if str(search).isdigit() and len(str(search)) >= 8:
                params["barcode"] = search
            # Keep general search for titles if supported
            params["search"] = search
        
        if pool_state:
            params["poolState"] = pool_state
            
        try:
            logger.debug(f"[IDEFIX] Listing products: page={page}, limit={limit}, search={search}, state={pool_state}")
            resp = self.session.get(url, headers=self._get_headers(), params=params, timeout=30)
            resp.raise_for_status()
            
            data = resp.json()
            logger.debug(f"[IDEFIX] list_products response keys: {list(data.keys()) if isinstance(data, dict) else 'raw list'}")
            
            if isinstance(data, dict):
                # Standard response: { "products": [...], ... }
                content = data.get('products', data.get('content', data.get('items', [])))
                # If totalElements is missing, we use length or a placeholder
                total = to_int(data.get('totalElements', data.get('totalCount', data.get('total', 0))))
                
                # If total is 0 but we have content, and we got exactly 'limit' items, assume there might be more
                if total <= 0 and content:
                    if len(content) >= limit:
                        total = (page + 1) * limit + 1 # Fake total to keep loops/paging active
                    else:
                        total = (page * limit) + len(content)
                
                return {
                    'content': content,
                    'totalElements': total,
                    'page': page,
                    'limit': limit
                }
            elif isinstance(data, list):
                # If API returns raw list
                return {
                    'content': data,
                    'totalElements': len(data),
                    'page': page,
                    'size': size
                }
            
            return {'content': [], 'totalElements': 0}
            
        except Exception as e:
            logger.error(f"[IDEFIX] list_products error: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"[IDEFIX] Response status: {e.response.status_code}")
                logger.error(f"[IDEFIX] Response body: {e.response.text}")
            return {'content': [], 'totalElements': 0}

    def get_product_questions(self, page: int = 1, limit: int = 20, barcode: str = None, 
                             startDate: str = None, endDate: str = None, sort: str = 'newest') -> Dict[str, Any]:
        """
        Fetch customer questions from Idefix.
        GET /pim/vendor/{vendor}/question/filter
        """
        url = f"{self.BASE_URL}/pim/vendor/{self.vendor_id}/question/filter"
        params = {
            "page": page,
            "limit": limit,
            "sort": sort
        }
        if barcode: params["barcode"] = barcode
        if startDate: params["startDate"] = startDate
        if endDate: params["endDate"] = endDate
        
        try:
            logger.info(f"[IDEFIX] Fetching questions: {params}")
            resp = self.session.get(url, headers=self._get_headers(), params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"[IDEFIX] get_product_questions error: {e}")
            return {"questions": [], "totalCount": 0}

    def answer_product_question(self, question_id: Any, answer_body: str) -> Dict[str, Any]:
        """
        Answer a customer question.
        POST /pim/vendor/{vendor}/question/{id}/answer (Hypothetical based on common patterns)
        """
        url = f"{self.BASE_URL}/pim/vendor/{self.vendor_id}/question/{question_id}/answer"
        payload = {"answerBody": answer_body}
        
        try:
            logger.info(f"[IDEFIX] Answering question {question_id}")
            resp = self.session.post(url, headers=self._get_headers(), json=payload, timeout=30)
            resp.raise_for_status()
            return resp.json() if resp.text else {"success": True}
        except Exception as e:
            logger.error(f"[IDEFIX] answer_product_question error: {e}")
            raise



def sync_idefix_with_xml_diff(job_id: str, xml_source_id: Any, user_id: int = None, **kwargs) -> Dict[str, Any]:
    """Smart Sync for Idefix (Diff Logic)"""
    startTime = time.time()
    append_mp_job_log(job_id, "Idefix Akıllı Senkronizasyon (Diff Sync) başlatıldı.")
    
    client = get_idefix_client(user_id=user_id)
    
    # Updated to use Stock Code & Exclusion List
    
    # 1. Fetch Pool items
    pool_items = client.get_full_pool_list()
    # Map STOCK CODE -> Item (Using 'barcode' or 'stockCode' from pool depending on what represents the vendor code)
    # Idefix pool items usually have 'barcode' and 'stockCode' (often same).
    # Since XML matching is now stock-code based, we'll map remote stock codes.
    remote_stock_map = {}
    for item in pool_items:
        # Check stock code first, fallback to barcode if stock code is missing or empty
        sc = item.get('stockCode') or item.get('barcode')
        if sc:
            remote_stock_map[sc.strip()] = item
            
    remote_stock_codes = set(remote_stock_map.keys())
    append_mp_job_log(job_id, f"Idefix hesabınızda toplam {len(remote_stock_codes)} stok kodlu ürün (havuz dahil) tespit edildi.")

    # 2. Check for waiting_vendor_approve and approve them
    waiting_approves = [item['barcode'] for item in pool_items if item.get('poolState') == 'waiting_vendor_approve']
    if waiting_approves:
        append_mp_job_log(job_id, f"{len(waiting_approves)} ürün satıcı onayı bekliyor. Otomatik onaylanıyor...")
        for bc in waiting_approves:
            client.approve_pool_item(bc)
        append_mp_job_log(job_id, "Bekleyen ordaş ürün onayları tamamlandı.")

    # 3. Load XML
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

    # 4. Find Diff & Fallback Matching
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
            # Fallback match to Barcode!
            matched_stock_codes.append(remote_sc)
            processed_remotes.add(remote_sc)
            continue
            
        # If neither, it's a candidate for zeroing
        to_zero_stock_codes.append(remote_sc)
        
    append_mp_job_log(job_id, f"XML'de OLMAYAN {len(to_zero_stock_codes)} ürün Idefix'te sıfırlanıyor.")
    if skipped_zero_count > 0:
        append_mp_job_log(job_id, f"🛡️ {skipped_zero_count} ürün harici listede olduğu için SIFIRLANMADI.")

    zeroed_count = 0
    if to_zero_stock_codes:
        zero_payload = []
        for sc in to_zero_stock_codes:
            # Need barcode for API usually
            item = remote_stock_map.get(sc)
            barcode = item.get('barcode') if item else sc
            zero_payload.append({'barcode': barcode, 'inventoryQuantity': 0, 'price': 0})
        
        for chunk in chunked(zero_payload, 100):
            try:
                # Inventory update for Idefix
                client.update_inventory_and_price(chunk)
                zeroed_count += len(chunk)
                append_mp_job_log(job_id, f"✅ {zeroed_count}/{len(to_zero_stock_codes)} ürün sıfırlandı.")
            except Exception as e:
                append_mp_job_log(job_id, f"Sıfırlama hatası: {e}", level='error')

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
        update_payload = []
        for sc in final_matched:
            # Try Primary Match
            xml_info = xml_map.get(sc)
            
            # Try Fallback Match
            if not xml_info:
                xml_info = xml_barcode_map.get(sc)
            
            if not xml_info: continue
            
            # Need barcode for update payload
            item = remote_stock_map.get(sc)
            barcode = item.get('barcode') if item else sc
            
            # Stock
            qty = to_int(xml_info.get('quantity'), 0)
            
            # Price
            base_price = to_float(xml_info.get('price'), 0.0)
            final_price = calculate_price(base_price, 'idefix', user_id=user_id)
            
            update_payload.append({
                'barcode': barcode,
                'inventoryQuantity': qty,
                'price': final_price,
                'comparePrice': final_price
            })
            
        for chunk in chunked(update_payload, 100):
            try:
                client.update_inventory_and_price(chunk)
                updated_count += len(chunk)
                append_mp_job_log(job_id, f"✅ {updated_count}/{len(final_matched)} eşleşen ürün güncellendi.")
            except Exception as e:
                append_mp_job_log(job_id, f"Güncelleme hatası: {e}", level='error')

    sync_res = {
        'success': True,
        'updated_count': updated_count,
        'zeroed_count': zeroed_count,
        'total_xml': len(xml_stock_codes),
        'total_remote': len(remote_stock_codes)
    }
    
    append_mp_job_log(job_id, f"Idefix senkronizasyon tamamlandı. Süre: {time.time()-startTime:.1f}s")
    return sync_res

def perform_idefix_sync_all(job_id: str, xml_source_id: Any, match_by: str = 'barcode', user_id: int = None) -> Dict[str, Any]:
    return sync_idefix_with_xml_diff(job_id, xml_source_id, user_id=user_id)


def fetch_and_cache_categories(user_id: int = None, job_id: str = None) -> Dict[str, Any]:
    """
    Fetch all categories from Idefix (Tree Structure) and cache flattened version.
    """
    from app.models import Setting
    from flask_login import current_user
    import json
    from app.services.job_queue import append_mp_job_log, update_mp_job
    
    if user_id is None and job_id:
        from app.services.job_queue import get_mp_job
        job = get_mp_job(job_id)
        if job and job.get('params'):
            user_id = job['params'].get('_user_id')
            
    if user_id is None:
        try:
            user_id = current_user.id if current_user and current_user.is_authenticated else None
        except Exception:
            user_id = None
    
    if job_id:
        append_mp_job_log(job_id, "İdefix kategori ağacı çekiliyor (Bu işlem 1-2 dakika sürebilir)...")
        
    try:
        client = get_idefix_client(user_id=user_id)
        logger.info("[IDEFIX] Fetching entire category tree...")
        
        # 1. Fetch Tree
        tree_data = client.get_categories()
        
        if job_id:
            append_mp_job_log(job_id, "Kategori ağacı alındı, işleniyor...")
            
        # 2. Flatten Tree (Iterative approach to avoid recursion limit)
        flattened_categories = []
        stack = []
        
        if isinstance(tree_data, list):
            stack = list(tree_data)
        elif isinstance(tree_data, dict) and 'content' in tree_data:
            stack = list(tree_data['content'])
        elif isinstance(tree_data, dict) and 'data' in tree_data:
            stack = list(tree_data['data'])
        else:
            logger.error(f"[IDEFIX] Unexpected category response format: {type(tree_data)}")
            if job_id:
                append_mp_job_log(job_id, "HATA: API geçersiz format döndürdü", level='error')
            return {"success": False, "message": "API beklenmeyen bir format döndürdü."}

        logger.info(f"[IDEFIX] Starting iterative flattening of {len(stack)} top-level categories...")
        
        processed_count = 0
        total_estimate = len(stack) # Minimal estimate
        
        while stack:
            cat = stack.pop()
            processed_count += 1
            subs = cat.get('subs', [])
            
            # Progress update in logs every 500 items to avoid spam
            if job_id and processed_count % 500 == 0:
                 update_mp_job(job_id, progress={'current': processed_count, 'message': f'{processed_count} kategori işlendi...'})

            # Only add LEAF categories (no subcategories)
            if not subs or len(subs) == 0:
                flat_cat = {
                    'id': cat.get('id'),
                    'name': cat.get('name'),
                    'parentId': cat.get('parentId'),
                    'topCategory': cat.get('topCategory'),
                    'isLeaf': True
                }
                flattened_categories.append(flat_cat)
            else:
                # Has subcategories, add them to stack
                stack.extend(subs)
            
        logger.info(f"[IDEFIX] Total {len(flattened_categories)} LEAF categories found and processed.")
        if job_id:
            append_mp_job_log(job_id, f"Toplam {len(flattened_categories)} alt kategori işlendi. Veritabanına kaydediliyor...")
        
        # Save to settings
        json_data = json.dumps(flattened_categories, ensure_ascii=False)
        logger.info(f"[IDEFIX] Saving {len(flattened_categories)} flattened categories. Data size: {len(json_data)} bytes.")
        Setting.set("IDEFIX_CATEGORY_TREE", json_data, user_id=user_id)
        
        if job_id:
            append_mp_job_log(job_id, "Başarıyla kaydedildi.")
            
        return {
            "success": True,
            "count": len(flattened_categories),
            "message": f"{len(flattened_categories)} kategori çekildi ve kaydedildi."
        }
        
    except Exception as e:
        logger.error(f"[IDEFIX] Category fetch failed: {e}")
        return {"success": False, "message": str(e)}
def get_idefix_client(user_id: Optional[int] = None) -> IdefixClient:
    from app.models import Setting
    from flask_login import current_user
    
    # Priority: passed user_id > current_user > global
    actual_user_id = user_id
    if actual_user_id is None:
        try:
            if current_user and current_user.is_authenticated:
                actual_user_id = current_user.id
        except Exception:
            pass

    if user_id is None:
        from flask_login import current_user
        try:
            actual_user_id = current_user.id if current_user and current_user.is_authenticated else None
        except Exception:
            actual_user_id = None
    else:
        actual_user_id = user_id

    api_key = Setting.get("IDEFIX_API_KEY", "", user_id=actual_user_id)
    api_secret = Setting.get("IDEFIX_API_SECRET", "", user_id=actual_user_id)
    vendor_id = Setting.get("IDEFIX_VENDOR_ID", "", user_id=actual_user_id)
    
    if not api_key or not vendor_id:
        logging.error("[IDEFIX] API Key or Vendor ID not found in settings for user %s. (Used user_id: %s)", actual_user_id, user_id)
        # Return a client that will fail on requests if keys are missing
        return IdefixClient("", "", "")
    
    return IdefixClient(api_key, api_secret, vendor_id)

def clear_idefix_cache(user_id: Optional[int] = None):
    """
    Clear Idefix related caches and local marketplace product data for a user.
    """
    from app.models import Setting, MarketplaceProduct
    from app import db
    import time
    
    # 1. Reset global TF-IDF cache (this affects all users but is safe since it auto-reloads)
    global _IDEFIX_CAT_TFIDF
    _IDEFIX_CAT_TFIDF = {
        "leaf": [],
        "names": [],
        "vectorizer": None,
        "matrix": None
    }
    
    # 2. Clear category tree setting
    if user_id:
        Setting.set("IDEFIX_CATEGORY_TREE", "", user_id=user_id)
        
        # 3. Update cache timestamp in DB to signal all workers to reload
        Setting.set("IDEFIX_CACHE_TIMESTAMP", str(time.time()), user_id=user_id)
        
        # 4. Delete local marketplace products for Idefix
        try:
            # Consistent lowercase 'idefix'
            MarketplaceProduct.query.filter_by(user_id=user_id, marketplace='idefix').delete()
            db.session.commit()
            logging.info("Idefix marketplace products cleared for user %s", user_id)
        except Exception as e:
            db.session.rollback()
            logging.error("Failed to clear Idefix marketplace products: %s", e)
    else:
        logging.warning("clear_idefix_cache called without user_id, only global cache cleared.")

def perform_idefix_send_products(job_id: str, barcodes: List[str], xml_source_id: Optional[int] = None, title_prefix: str = None, user_id: int = None, **kwargs) -> Dict[str, Any]:
    from app.services.job_queue import append_mp_job_log, get_mp_job, update_mp_job
    from app.services.xml_service import load_xml_source_index
    from app.utils.helpers import to_float, to_int, clean_forbidden_words, is_product_forbidden, calculate_price, get_marketplace_multiplier
    from app.models import Setting
    from flask_login import current_user
    import time
    
    # Extract options (Multiplier kaldırıldı - artık GLOBAL_PRICE_RULES kullanılıyor)
    default_price_val = to_float(kwargs.get('default_price', 0.0))
    skip_no_barcode = kwargs.get('skip_no_barcode', False)
    skip_no_image = kwargs.get('skip_no_image', False)
    zero_stock_as_one = kwargs.get('zero_stock_as_one', False)
    
    append_mp_job_log(job_id, f"İdefix gönderim işlemi başlatılıyor... Barkodsuz Atla={skip_no_barcode}")
    
    try:
        if not user_id and xml_source_id:
            try:
                from app.models import SupplierXML
                s_id = str(xml_source_id)
                if s_id.isdigit():
                    src = SupplierXML.query.get(int(s_id))
                    if src: user_id = src.user_id
            except Exception as e:
                logging.warning(f"Failed to resolve user_id in idefix send: {e}")

        client = get_idefix_client(user_id=user_id)
    except Exception as e:
        error_msg = f"İdefix client hatası: {str(e)}"
        append_mp_job_log(job_id, error_msg, level='error')
        return {"success_count": 0, "fail_count": len(barcodes), "failures": [error_msg]}

    append_mp_job_log(job_id, "XML verisi yükleniyor...")
    index = load_xml_source_index(xml_source_id)
    if not index:
        error_msg = "XML verisi okunamadı"
        append_mp_job_log(job_id, error_msg, level='error')
        return {"success_count": 0, "fail_count": len(barcodes), "failures": [error_msg]}

    products_to_send = []
    skipped_count = 0
    skipped_list = []
    
    # Settings fetch
    if user_id is None:
        user_id = current_user.id if hasattr(current_user, 'id') else None
    
    
    
    # Multiplier kaldırıldı - artık GLOBAL_PRICE_RULES kullanılıyor
        
    default_cat_id = Setting.get("IDEFIX_DEFAULT_CATEGORY_ID", "", user_id=user_id)
    default_brand_id = Setting.get("IDEFIX_BRAND_ID", "", user_id=user_id)  # Fallback brand ID
    
    # Barcode settings
    # Barcode settings
    barcode_prefix = Setting.get("IDEFIX_BARCODE_PREFIX", "", user_id=user_id)
    # Support both old manual setting and new Auto Sync toggle
    use_random_old = Setting.get("IDEFIX_USE_RANDOM_BARCODE", "off", user_id=user_id) == "on"
    use_random_new = Setting.get(f"AUTO_SYNC_USE_RANDOM_BARCODE_idefix", user_id=user_id) == "true"
    use_random_barcode = use_random_old or use_random_new
    
    # Brand resolution - API-first approach with local cache
    local_brand_cache = {}
    
    def resolve_idefix_brand_id(brand_name: str) -> Optional[int]:
        """Resolve brand name to Idefix brand ID using API. Returns None if not found."""
        if not brand_name:
            return None
        
        brand_key = brand_name.lower().strip()
        
        # Check local session cache first
        if brand_key in local_brand_cache:
            return local_brand_cache[brand_key]
        
        # Call Idefix API to search for brand
        try:
            brand_result = client.search_brand_by_name(brand_name)
            if brand_result and brand_result.get('id'):
                brand_id = brand_result['id']
                local_brand_cache[brand_key] = brand_id
                append_mp_job_log(job_id, f"Marka '{brand_name}' API ile bulundu: {brand_id}")
                return brand_id
            else:
                append_mp_job_log(job_id, f"Marka '{brand_name}' API'de bulunamadı", level='warning')
        except Exception as e:
            append_mp_job_log(job_id, f"Marka arama hatası ({brand_name}): {e}", level='warning')
        
        # Cache the failure
        local_brand_cache[brand_key] = None
        return None
    
    # Prepare data objects
    prepared_data_map = {} 
    total_items = len(barcodes)
    def check_cancelled():
        from app.services.job_queue import get_mp_job
        job = get_mp_job(job_id)
        return job and job.get('cancel_requested')

    processed = 0
    for barcode in barcodes:
        # Check for cancel request
        if check_cancelled():
            append_mp_job_log(job_id, f"İşlem kullanıcı tarafından iptal edildi. {processed}/{total_items} ürün işlendi.", level='warning')
            break
        
        processed += 1
        
        # Update progress
        update_mp_job(job_id, progress={
            'current': processed,
            'total': total_items,
            'message': f'{processed} / {total_items} ürün hazırlanıyor'
        })
        
        rec = index.get(str(barcode))
        if not rec:
            skipped_count += 1
            skipped_list.append({'barcode': barcode, 'reason': 'XML\'de bulunamadı'})
            continue
            
        # Forbidden check
        title = rec.get('title') or ''
        brand = rec.get('brand') or rec.get('vendor') or ''
        category = rec.get('category') or rec.get('top_category') or ''
        
        forbidden_reason = is_product_forbidden(user_id, title=title, brand=brand, category=category)
        if forbidden_reason:
            skipped_count += 1
            skipped_list.append({'barcode': barcode, 'reason': f"Yasaklı Liste: {forbidden_reason}"})
            continue
            
        # Clean words
        title = clean_forbidden_words(title)
            
        # Barcode Cleaning Logic
        final_barcode = barcode
        if barcode_prefix and final_barcode.startswith(barcode_prefix):
            final_barcode = final_barcode[len(barcode_prefix):]
            append_mp_job_log(job_id, f"🧹 Barkod temizlendi: {barcode} -> {final_barcode}")
            
        if not final_barcode and use_random_barcode:
            # Generate random if empty after clean
            import random, string
            suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
            final_barcode = f"GEN-{suffix}"
            append_mp_job_log(job_id, f"🎲 Rastgele barkod oluşturuldu: {final_barcode}")
        elif not final_barcode:
             skipped_count += 1
             skipped_list.append({'barcode': barcode, 'reason': 'Barkod temizlendi ve boş kaldı'})
             append_mp_job_log(job_id, f"❌ {barcode}: Temizlik sonrası boş kaldı, atlanıyor.", level='warning')
             continue

        price = float(rec.get('price', 0))
        # Artık GLOBAL_PRICE_RULES kullanılıyor (multiplier kaldırıldı)
        final_price = calculate_price(price, 'idefix', user_id=user_id)
        
        # Resolve brand via API
        brand_name = rec.get('brand') or rec.get('vendor') or ''
        resolved_brand_id = resolve_idefix_brand_id(brand_name) if brand_name else None
        
        # Use resolved brand ID, fallback to default from settings
        final_brand_id = resolved_brand_id
        if not final_brand_id and default_brand_id:
            final_brand_id = int(default_brand_id)
        
        # Resolve category via TF-IDF
        excel_category = rec.get('category') or rec.get('top_category') or ''
        product_title = rec.get('title') or ''
        resolved_cat_id = resolve_idefix_category(product_title, excel_category, user_id=user_id)
        
        # Use resolved category ID, fallback to default from settings
        final_cat_id = resolved_cat_id
        if not final_cat_id and default_cat_id:
            final_cat_id = int(default_cat_id)
        
        # Base item for Fast Listing
        final_title = (f"{title_prefix} " if title_prefix else "") + title
        item = {
            "barcode": final_barcode, # Use the cleaned/new barcode
            "title": final_title[:200],
            "vendorStockCode": rec.get('stockCode') or barcode,
            "price": final_price,
            "comparePrice": final_price,
            "inventoryQuantity": int(rec.get('quantity', 0)),
            # Extra fields for Create Product
            "description": clean_forbidden_words(rec.get('description', '') or title)[:5000],
            "images": rec.get('images', []),
            "vatRate": rec.get('vatRate', 18),
            "desi": 1,
            "brandId": final_brand_id,  # Resolved via API
            "brandName": brand_name,  # Keep original name for reference
            "categoryId": final_cat_id,  # Resolved via TF-IDF
        }
        products_to_send.append(item)
        # Map using the FINAL barcode so we can look it up later in fallback loop
        prepared_data_map[final_barcode] = item

    if not products_to_send:
        error_msg = "Gönderilecek ürün bulunamadı"
        append_mp_job_log(job_id, error_msg, level='warning')
        return {
            "success_count": 0, "fail_count": skipped_count, 
            "failures": ["No products"], "matched": [], "skipped": skipped_list
        }

    success_count = 0
    fail_count = 0
    failures = []
    batch_request_ids = []
    
    # ---------------------------------------------------------
    # PRODUCT CREATE API - Yeni ürünler için doğrudan oluşturma
    # Endpoint: POST /pim/pool/{vendorId}/create
    # ---------------------------------------------------------
    
    append_mp_job_log(job_id, f"📦 {len(products_to_send)} adet ürün İdefix'e gönderiliyor...")
    
    # Prepare products for create API
    create_batch_list = []
    skipped_no_brand = 0
    skipped_no_category = 0
    category_attrs_cache = {}  # Cache category attributes to avoid repeated API calls
    
    for item in products_to_send:
        # Validate required fields
        item_brand_id = item.get('brandId')
        item_cat_id = item.get('categoryId')
        
        if not item_brand_id:
            skipped_no_brand += 1
            append_mp_job_log(job_id, f"⚠️ {item['barcode']}: Marka bulunamadı, atlanıyor", level='warning')
            continue
        
        if not item_cat_id:
            skipped_no_category += 1
            append_mp_job_log(job_id, f"⚠️ {item['barcode']}: Kategori bulunamadı, atlanıyor", level='warning')
            continue
        
        # Fix images format
        fixed_images = []
        for img in item.get('images', []):
            if isinstance(img, dict) and 'url' in img:
                fixed_images.append(img)
            elif isinstance(img, str) and img:
                fixed_images.append({'url': img})
        
        # Fetch category attributes if not cached
        if item_cat_id not in category_attrs_cache:
            append_mp_job_log(job_id, f"📋 Kategori {item_cat_id} için özellikler çekiliyor...")
            attrs = client.get_category_attributes(item_cat_id)
            category_attrs_cache[item_cat_id] = attrs
            
            # Log attribute details
            if attrs:
                required_attrs = [a for a in attrs if a.get('required', False)]
                append_mp_job_log(job_id, f"   ✓ {len(attrs)} özellik bulundu, {len(required_attrs)} tanesi zorunlu")
                for ra in required_attrs:
                    attr_name = ra.get('attributeTitle') or ra.get('name', 'Bilinmeyen')
                    attr_values = ra.get('attributeValues', [])
                    allow_custom = ra.get('allowCustom', False)
                    append_mp_job_log(job_id, f"   • {attr_name}: {len(attr_values)} değer {'(özel değer izinli)' if allow_custom else ''}")
            else:
                append_mp_job_log(job_id, f"   ⚠️ Kategori için özellik bulunamadı!", level='warning')
        
        # Build required attributes with variant matching
        product_attributes = []
        missing_required = []
        variant_attributes = rec.get('variant_attributes', [])
        
        def get_variant_value(attr_name):
            attr_name_lower = attr_name.lower()
            for va in variant_attributes:
                v_name = va['name'].lower()
                if v_name in attr_name_lower or attr_name_lower in v_name:
                    return va['value']
            return None
        
        for attr in category_attrs_cache.get(item_cat_id, []):
            if attr.get('required', False):
                attr_id = attr.get('attributeId') or attr.get('id')
                attr_name = attr.get('attributeTitle') or attr.get('name', 'Bilinmeyen')
                attr_values = attr.get('attributeValues', [])
                
                # Try to get value from XML variants
                val_from_xml = get_variant_value(attr_name)
                matched_val_id = None
                
                if val_from_xml and attr_values:
                    val_from_xml_lower = val_from_xml.lower()
                    for v in attr_values:
                        v_name = v.get('value', '').lower()
                        if v_name == val_from_xml_lower:
                            matched_val_id = v.get('id')
                            break
                    # Fuzzy match
                    if not matched_val_id:
                        for v in attr_values:
                            v_name = v.get('value', '').lower()
                            if v_name in val_from_xml_lower or val_from_xml_lower in v_name:
                                matched_val_id = v.get('id')
                                break
                
                if matched_val_id:
                    product_attributes.append({
                        "attributeId": attr_id,
                        "attributeValueId": matched_val_id,
                        "customAttributeValue": None
                    })
                elif val_from_xml and attr.get('allowCustom', False):
                    product_attributes.append({
                        "attributeId": attr_id,
                        "attributeValueId": None,
                        "customAttributeValue": val_from_xml[:100]
                    })
                elif attr_values and len(attr_values) > 0:
                    # Fallback to first available value
                    first_value = attr_values[0]
                    product_attributes.append({
                        "attributeId": attr_id,
                        "attributeValueId": first_value.get('id'),
                        "customAttributeValue": None
                    })
                elif attr.get('allowCustom', False):
                    # Use custom value from title
                    product_attributes.append({
                        "attributeId": attr_id,
                        "attributeValueId": None,
                        "customAttributeValue": item.get('title', '')[:100]
                    })
                else:
                    # Required but no value available!
                    missing_required.append(attr_name)
        
        if missing_required:
            append_mp_job_log(job_id, f"   ⚠️ {item['barcode']}: Eksik zorunlu özellik: {', '.join(missing_required)}", level='warning')
        
        # Construct payload for create API
        # Map vatRate to Idefix-accepted values (0, 1, 10, 20)
        raw_vat = int(item.get('vatRate', 20))
        idefix_vat = raw_vat if raw_vat in (0, 1, 10, 20) else 20  # Default to 20 if not valid
        
        # Determine grouping ID - Fixed priority: parent_barcode > modelCode > productCode
        pm_id = rec.get('parent_barcode') or rec.get('modelCode') or rec.get('productCode') or item.get('vendorStockCode', item['barcode'])
        
        new_prod = {
            "barcode": item['barcode'],
            "title": item['title'],
            "productMainId": pm_id,
            "brandId": int(item_brand_id),
            "categoryId": int(item_cat_id),
            "inventoryQuantity": int(item.get('inventoryQuantity', 0)),
            "vendorStockCode": pm_id, # Idefix often uses vendorStockCode as grouping anchor in some flows
            "desi": item.get('desi', 1),
            "description": rec.get('details') or item.get('description') or item.get('title', ''),
            "price": float(item.get('price', 0)),
            "comparePrice": float(item.get('comparePrice', item.get('price', 0))),
            "vatRate": idefix_vat,
            "deliveryDuration": 3,
            "deliveryType": "regular",
            "images": fixed_images if fixed_images else [],
            "attributes": product_attributes  # Required category attributes
        }
        
        create_batch_list.append(new_prod)
    
    if skipped_no_brand > 0:
        append_mp_job_log(job_id, f"⚠️ {skipped_no_brand} ürün marka bulunamadığı için atlandı", level='warning')
    if skipped_no_category > 0:
        append_mp_job_log(job_id, f"⚠️ {skipped_no_category} ürün kategori bulunamadığı için atlandı", level='warning')
    
    if not create_batch_list:
        append_mp_job_log(job_id, "Gönderilecek geçerli ürün bulunamadı", level='error')
        return {
            "success_count": 0,
            "fail_count": len(products_to_send),
            "failures": ["Marka veya kategori bulunamadı"],
            "matched": [],
            "skipped": skipped_list,
            "batch_request_ids": []
        }
    
    # Send in batches of 20
    batch_size = 20
    total_batches = (len(create_batch_list) + batch_size - 1) // batch_size
    def check_cancelled():
        from app.services.job_queue import get_mp_job
        job = get_mp_job(job_id)
        return job and job.get('cancel_requested')

    for i in range(0, len(create_batch_list), batch_size):
        # Check for cancel request before each batch
        if check_cancelled():
            append_mp_job_log(job_id, f"İşlem kullanıcı tarafından iptal edildi.", level='warning')
            cancelled = True
            break
        
        batch = create_batch_list[i:i+batch_size]
        batch_num = i // batch_size + 1
        
        # Update progress
        update_mp_job(job_id, progress={
            'current': i + len(batch),
            'total': len(create_batch_list),
            'message': f'Batch {batch_num}/{total_batches} gönderiliyor'
        })
        
        append_mp_job_log(job_id, f"Batch {batch_num}/{total_batches} gönderiliyor ({len(batch)} ürün)...")
        
        try:
            resp = client.create_products(batch)
            batch_request_id = resp.get('batchRequestId')
            
            if batch_request_id:
                batch_request_ids.append(batch_request_id)
                append_mp_job_log(job_id, f"✅ Batch {batch_num} gönderildi! ID: {batch_request_id}")
                
                # Wait and check batch result with loop
                import time
                append_mp_job_log(job_id, f"⏳ Batch sonucu bekleniyor (maks 30sn)...")
                
                for _poll in range(10): # 10 * 3s = 30s
                    time.sleep(3)
                    try:
                        result = client.query_pool_batch_status(batch_request_id)
                        batch_status = result.get('status', 'UNKNOWN')
                        
                        # If still processing, continue waiting
                        if batch_status in ('QUEUED', 'PROCESSING', 'IN_PROGRESS'):
                             if _poll == 9: 
                                 append_mp_job_log(job_id, f"⚠️ Batch hala işlemde: {batch_status} (Zaman aşımı)")
                             continue
                        
                        # Log final status
                        append_mp_job_log(job_id, f"📊 Batch bitti: {batch_status}")
                        
                        # Check products in result
                        products = result.get('products', []) or result.get('items', [])
                        for prod in products:
                            prod_barcode = prod.get('barcode', 'N/A')
                            prod_status = prod.get('status', prod.get('poolState', 'unknown'))
                            failure_reasons = prod.get('failureReasons', {})
                            
                            if failure_reasons:
                                reason_msg = failure_reasons.get('message', str(failure_reasons))
                                append_mp_job_log(job_id, f"   ❌ {prod_barcode}: {prod_status} - {reason_msg}", level='error')
                            else:
                                append_mp_job_log(job_id, f"   ✓ {prod_barcode}: {prod_status}")
                                
                        success_count += len(batch)
                        break # Done with this batch
                        
                    except Exception as poll_err:
                        append_mp_job_log(job_id, f"⚠️ Batch durum kontrol hatası (Deneme {_poll}): {poll_err}", level='warning')
                        if _poll == 9: success_count += len(batch) # Assume sent if polling fails
            else:
                fail_count += len(batch)
                append_mp_job_log(job_id, f"❌ Batch {batch_num}: ID alınamadı", level='error')
                
        except Exception as e:
            fail_count += len(batch)
            error_msg = str(e)
            
            # Try to extract response body for detailed error info
            if hasattr(e, 'response') and e.response is not None:
                try:
                    resp_text = e.response.text[:500] if e.response.text else 'No response body'
                    error_msg = f"{e} - Response: {resp_text}"
                    append_mp_job_log(job_id, f"❌ Batch {batch_num} API yanıtı: {resp_text}", level='error')
                except:
                    pass
            
            append_mp_job_log(job_id, f"❌ Batch {batch_num} hatası: {error_msg}", level='error')
            failures.append(error_msg)
    
    # Final summary
    append_mp_job_log(job_id, "")
    append_mp_job_log(job_id, "📋 SON DURUM:")
    append_mp_job_log(job_id, f"Gönderilen: {success_count}")
    append_mp_job_log(job_id, f"Hatalı/Atlanan: {fail_count + skipped_count}")
    if batch_request_ids:
        append_mp_job_log(job_id, f"Batch ID'leri: {', '.join(batch_request_ids[:3])}...")
        append_mp_job_log(job_id, "")
        append_mp_job_log(job_id, "ℹ️ Ürünler İdefix tarafından işleniyor. Sonuçları İdefix panelinden takip edebilirsiniz.")

    return {
        "success": True,
        "success_count": success_count,
        "fail_count": fail_count + skipped_count, # Include skipped in fail for BatchLog summary
        "failures": failures[:20],
        "batch_id": batch_request_id if 'batch_request_id' in locals() else None,
        "skipped": skipped_list,
        "summary": {
            "success_count": success_count,
            "fail_count": fail_count + skipped_count
        }
    }

def perform_idefix_send_all(job_id: str, xml_source_id: Any, user_id: int = None, **kwargs) -> Dict[str, Any]:
    """Send ALL products from XML source to Idefix"""
    from app.services.job_queue import append_mp_job_log
    from app.services.xml_service import load_xml_source_index
    
    append_mp_job_log(job_id, "Tüm ürünler hazırlanıyor...")
    xml_index = load_xml_source_index(xml_source_id)
    all_barcodes = list((xml_index.get('by_barcode') or {}).keys())
    
    if not all_barcodes:
        return {'success': False, 'message': 'XML kaynağında ürün bulunamadı.', 'count': 0}

    append_mp_job_log(job_id, f"Toplam {len(all_barcodes)} ürün bulundu. Gönderim başlıyor...")
    return perform_idefix_send_products(job_id, all_barcodes, xml_source_id, user_id=user_id, **kwargs)

def perform_idefix_batch_update(job_id: str, items: List[Dict[str, Any]], user_id: int = None) -> Dict[str, Any]:
    """
    Batch update Idefix stock/price from local data.
    items: [{'barcode': '...', 'stock': 10, 'price': 100.0}, ...]
    """
    try:
        from app.services.job_queue import append_mp_job_log
        client = get_idefix_client(user_id=user_id)
        append_mp_job_log(job_id, f"Idefix toplu güncelleme başlatıldı. {len(items)} ürün.")
        
        formatted_items = []
        for item in items:
            f_item = {'barcode': item['barcode']}
            if 'stock' in item:
                f_item['stockCount'] = int(item['stock'])
            if 'price' in item:
                f_item['salePrice'] = float(item['price'])
                f_item['listPrice'] = float(item['price'])
            formatted_items.append(f_item)
            
        if not formatted_items:
             return {'success': False, 'message': 'Güncellenecek veri bulunamadı.'}
             
        # Idefix client.update_inventory_and_price handles chunking internally or just take all?
        # Usually it's better to chunk here too
        total_sent = 0
        from app.utils.helpers import chunked
        for chunk in chunked(formatted_items, 50):
            client.update_inventory_and_price(chunk)
            total_sent += len(chunk)
            append_mp_job_log(job_id, f"✅ {total_sent}/{len(formatted_items)} ürün gönderildi.")
            time.sleep(1)
            
        return {'success': True, 'count': total_sent}
    except Exception as e:
        return {'success': False, 'message': str(e)}

def fetch_all_idefix_products(user_id: Optional[int] = None, job_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch all products from Idefix API across all status pools."""
    from app.services.job_queue import append_mp_job_log, update_mp_job
    client = get_idefix_client(user_id=user_id)
    
    all_items = []
    # Idefix has different pools for different statuses
    POOL_STATES = ["APPROVED", "WAITING_APPROVAL", "REJECTED", "WAITING_CONTENT", "DELETED"]
    
    # Status Mapping & Colors
    STATUS_MAP = {
        "APPROVED": ("Satışta", "success"),
        "WAITING_APPROVAL": ("Onay Bekliyor", "warning"),
        "REJECTED": ("Reddedildi", "danger"),
        "WAITING_CONTENT": ("İçerik Bekleniyor", "info"),
        "DELETED": ("Silindi", "secondary")
    }
    
    if job_id:
        append_mp_job_log(job_id, "İdefix'ten tüm statülerdeki ürünler çekiliyor...")
        
    for state in POOL_STATES:
        if job_id:
            append_mp_job_log(job_id, f"Statü çekiliyor: {state}...")
            
        page = 0
        limit = 100
        state_item_count = 0
        
        while True:
            try:
                res = client.list_products(page=page, limit=limit, pool_state=state)
                items = res.get('content', [])
                total = int(res.get('totalElements', 0))
                
                if not items:
                    break
                
                # Enrich items with status info
                label, color = STATUS_MAP.get(state, (state, "secondary"))
                for item in items:
                    item['status_label'] = label
                    item['status_color'] = color
                    item['original_status'] = state
                    
                all_items.extend(items)
                state_item_count += len(items)
                
                if job_id:
                    update_mp_job(job_id, progress={
                        'current': len(all_items),
                        'total': total if total > len(all_items) else len(all_items) + 1,
                        'message': f'{state}: {state_item_count} ürün çekildi (Toplam: {len(all_items)})'
                    })
                    
                # Break if we reached or exceeded total count for THIS state
                # FIX: If total is 0/missing/fake, do NOT break based on total check alone.
                # Only break if we got fewer items than limit (meaning end of list).
                if len(items) < limit:
                    break
                    
                # If total is explicitly > 0 and looks real (not the fake +1 logic), we might use it,
                # but it's safer to just rely on len(items) < limit or empty items.
                # However, to avoid infinite loops if API is buggy (returns same page), safety break remains.
                
                page += 1
                if page > 1000: break # Increased Safety limit
                
            except Exception as e:
                # Log error but continue to next state
                error_msg = f"Statü {state} Sayfa {page} hatası: {e}"
                logging.error(error_msg)
                if job_id:
                    append_mp_job_log(job_id, error_msg, level='error')
                break
            
    return all_items

def sync_idefix_products(user_id: Optional[int] = None, job_id: Optional[str] = None) -> Dict[str, Any]:
    """Sync Idefix products to local DB."""
    try:
        from app.models import MarketplaceProduct
        from app import db
        
        # Priority: passed user_id > job params
        if user_id is None and job_id:
            from app.services.job_queue import get_mp_job
            job = get_mp_job(job_id)
            if job and job.get('params'):
                user_id = job['params'].get('_user_id')

        items = fetch_all_idefix_products(user_id=user_id, job_id=job_id)
        
        if job_id:
            from app.services.job_queue import append_mp_job_log
            append_mp_job_log(job_id, f"Veritabanına {len(items)} ürün kaydediliyor...")
            
        count = 0
        batch_size = 100
        remote_barcodes = set()
        
        from app.utils.helpers import chunked
        
        for chunk in chunked(items, batch_size):
            for item in chunk:
                # Idefix item structure:
                # barcode, vendorStockCode, title, price, salePrice(?), quantity/stockAmount
                # status (poolState?)
                
                barcode = item.get('barcode', '')
                if not barcode: continue
                remote_barcodes.add(barcode)
                
                stock_code = item.get('vendorStockCode') or barcode
                title = item.get('title')
                
                # Price might be in 'price', 'salePrice'?
                # list_products returns 'price' usually.
                list_price = float(item.get('price', 0))
                sale_price = float(item.get('salePrice', list_price)) # Fallback
                
                # Stock fallback
                qty = item.get('stockAmount')
                if qty is None: qty = item.get('inventoryQuantity')
                if qty is None: qty = item.get('quantity')
                if qty is None: qty = item.get('stock')
                if qty is None: qty = 0
                qty = int(qty)
                
                # Status mapping for better UI
                pool_state = item.get('poolState') or item.get('productStatus') or item.get('original_status') or 'UNKNOWN'
                pool_state_up = str(pool_state).upper()
                
                if pool_state_up == "APPROVED":
                    status_str = "Satışta"
                elif pool_state_up == "WAITING_APPROVAL":
                    status_str = "İnceleniyor"
                elif pool_state_up == "WAITING_CONTENT":
                    status_str = "Eksik Bilgili"
                elif pool_state_up == "REJECTED":
                    status_str = "Reddedildi"
                elif pool_state_up == "DELETED":
                    status_str = "Silindi"
                else:
                    # If we have a friendly label from fetch_all, use it
                    if item.get('status_label'):
                        status_str = item.get('status_label')
                    else:
                        status_str = pool_state

                approval_str = status_str
                
                # On Sale?
                on_sale = (pool_state_up == "APPROVED")
                
                # Images
                imgs = item.get('images', [])
                img_json = json.dumps([i.get('url') if isinstance(i, dict) else i for i in imgs])
                
                # Upsert
                if not user_id:
                    # If user_id is missing, we must skip to prevent cross-user pollution
                    continue

                existing = MarketplaceProduct.query.filter_by(
                    marketplace='idefix',
                    barcode=barcode,
                    user_id=user_id
                ).first()
                
                if existing:
                    existing.stock_code = stock_code
                    existing.title = title
                    existing.price = list_price
                    existing.sale_price = sale_price
                    existing.quantity = qty
                    existing.status = status_str
                    existing.approval_status = approval_str
                    existing.images_json = img_json
                    existing.raw_data = json.dumps(item)
                    existing.last_sync_at = datetime.now()
                else:
                    new_p = MarketplaceProduct(
                        user_id=user_id,
                        marketplace='idefix',
                        barcode=barcode,
                        stock_code=stock_code,
                        title=title,
                        price=list_price,
                        sale_price=sale_price,
                        quantity=qty,
                        status=status_str,
                        approval_status=approval_str,
                        images_json=img_json,
                        raw_data=json.dumps(item)
                    )
                    db.session.add(new_p)
                count += 1
            db.session.commit()
            
        if user_id:
            # Cleanup
            db.session.query(MarketplaceProduct).filter(
                 MarketplaceProduct.user_id == user_id,
                 MarketplaceProduct.marketplace == 'idefix',
                 ~MarketplaceProduct.barcode.in_(remote_barcodes)
            ).delete(synchronize_session=False)
            db.session.commit()
            
        return {'success': True, 'count': count}
    except Exception as e:
        if job_id:
             from app.services.job_queue import append_mp_job_log
             append_mp_job_log(job_id, f"Sync hatası: {e}", level='error')
        logger.exception("Sync Idefix error")
        return {'success': False, 'error': str(e)}


def perform_idefix_product_update(barcode: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Detailed update for Idefix product.
    """
    client = get_idefix_client()
    messages = []
    success = True
    
    # 1. Price/Stock (Immediate - UpdateInventoryAndPrice)
    if 'quantity' in data or 'salePrice' in data or 'listPrice' in data:
        try:
            update_item = {'barcode': barcode}
            
            # Idefix update_inventory_and_price expects specific keys
            # We need to fetch current values first ideally, or just send what we have
            # But the endpoint is inventory-upload.
            
            # Since we don't know current stock/price easily without fetching, 
            # we rely on data provided. 
            
            # NOTE: Idefix requires both price and stock in the same payload for inventory-upload usually?
            # Or we can send partial? Let's check update_inventory_and_price impl.
            # It processes list of items.
            
            # We assume the user provided values or we might be overwriting with 0 if missing.
            # Ideally fetch product first.
            
            # Fetch current to be safe
            current_prod = None
            try:
                # Search by barcode (fast listing check or similar?)
                pass 
            except: pass
            
            if 'quantity' in data:
                update_item['inventoryQuantity'] = int(data['quantity'])
            
            if 'salePrice' in data:
                update_item['price'] = float(data['salePrice']) 
            if 'listPrice' in data:
                update_item['comparePrice'] = float(data['listPrice']) # Idefix convention
                
            # If we don't have all price fields, we might error.
            # Let's hope partial works or we just send what we changed.
            
            # WARNING: If only stock is sent, price might reset? need to check API behavior.
            # For now, we proceed.
            
            if len(update_item) > 1:
                client.update_inventory_and_price([update_item])
                messages.append("Fiyat/Stok güncellendi.")
                
        except Exception as e:
            messages.append(f"Fiyat/Stok hatası: {e}")
            success = False

    # 2. Content Update (via Pool Create/Update)
    content_fields = ['title', 'description', 'images', 'brandId', 'categoryId']
    
    if any(k in data for k in content_fields):
        try:
            # Re-create product in pool to update content
            update_item = {'barcode': barcode}
            
            if 'title' in data: update_item['name'] = data['title']
            if 'description' in data: update_item['description'] = data['description']
            if 'images' in data: update_item['images'] = data['images']
            if 'brandId' in data: update_item['brandId'] = int(data['brandId'])
            if 'categoryId' in data: update_item['categoryId'] = int(data['categoryId'])
            
            # Mandatory fields for creation might be missing (e.g. Attributes).
            # This is risky. Updates usually require full payload.
            
            client.create_product([update_item])
            messages.append("İçerik güncelleme/onay isteği gönderildi.")
                
        except Exception as e:
            messages.append(f"İçerik güncelleme hatası: {e}")
            success = False

    return {'success': success, 'message': ' | '.join(messages)}



def perform_idefix_batch_update(job_id: str, items: List[Dict[str, Any]], user_id: int = None) -> Dict[str, Any]:
    """
    Batch update Idefix stock/price from Excel items.
    items: [{'barcode': '...', 'stock': 10, 'price': 100.0}, ...]
    """
    try:
        client = get_idefix_client(user_id=user_id)
        append_mp_job_log(job_id, f"Idefix toplu güncelleme başlatıldı. {len(items)} ürün.")
        
        payload = []
        for item in items:
            barcode = item['barcode']
            idefix_item = {'barcode': barcode}
            
            if 'stock' in item:
                idefix_item['inventoryQuantity'] = int(item['stock'])
            
            if 'price' in item:
                # Idefix update usually wants both if they are strict, but let's try partial
                idefix_item['price'] = float(item['price'])
                idefix_item['comparePrice'] = float(item['price']) # Default same
                
            payload.append(idefix_item)
            
        if not payload:
            return {'success': False, 'message': 'Güncellenecek veri bulunamadı.'}
            
        # Idefix batch limit is usually 100
        total_sent = 0
        from app.utils.helpers import chunked
        for chunk in chunked(payload, 50):
            client.update_inventory_and_price(chunk)
            total_sent += len(chunk)
            append_mp_job_log(job_id, f"{total_sent}/{len(payload)} ürün gönderildi.")
            time.sleep(1)
            
        append_mp_job_log(job_id, "Idefix güncelleme işlemi tamamlandı.")
        return {'success': True, 'count': total_sent}
        
    except Exception as e:
        msg = f"Idefix batch update hatası: {str(e)}"
        append_mp_job_log(job_id, msg, level='error')
        return {'success': False, 'message': msg}


def perform_idefix_direct_push_actions(user_id: int, to_update: List[Any], to_create: List[Any], to_zero: List[Any], src: Any, job_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Idefix için Direct Push aksiyonlarını gerçekleştirir.
    to_update: (xml_item, local_item) listesi
    to_create: xml_item listesi
    to_zero: local_item listesi
    """
    from app.services.job_queue import append_mp_job_log
    from app.utils.helpers import calculate_price
    from app.models import MarketplaceProduct, db
    
    client = get_idefix_client(user_id=user_id)
    res = {'updated_count': 0, 'created_count': 0, 'zeroed_count': 0}
    
    # --- 1. GÜNCELLEMELER (Update) ---
    if to_update:
        update_items = []
        for xml_item, local_item in to_update:
            final_price = calculate_price(xml_item.price, 'idefix', user_id=user_id)
            update_items.append({
                'barcode': local_item.barcode, # Veritabanındaki barkodu kullan
                'price': final_price,
                'inventoryQuantity': xml_item.quantity
            })
            
            # Canlı Log
            if job_id: append_mp_job_log(job_id, f"Güncelleniyor: {xml_item.stock_code} (Stok: {local_item.quantity} -> {xml_item.quantity})")
            
            # Yerel veritabanını güncelle
            local_item.quantity = xml_item.quantity
            local_item.sale_price = final_price
            local_item.last_sync_at = datetime.now()

        # Idefix toplu güncellemeyi destekler
        try:
            from app.utils.helpers import chunked
            for batch in chunked(update_items, 100):
                client.update_inventory_and_price(batch)
                res['updated_count'] += len(batch)
            db.session.commit()
        except Exception as e:
            if job_id: append_mp_job_log(job_id, f"Idefix güncelleme hatası: {str(e)}", level='error')

    # --- 2. YENİ ÜRÜNLER (Create) ---
    if to_create:
        from app.services.xml_service import generate_random_barcode
        create_items = []
        for xml_item in to_create:
            # Random barkod seçeneği
            barcode = xml_item.barcode
            
            # Check random barcode settings (Global overrides from Auto Sync Menu)
            use_random_setting = Setting.get(f'AUTO_SYNC_USE_RANDOM_BARCODE_idefix', user_id=user_id) == 'true'
            use_override_setting = Setting.get(f'AUTO_SYNC_USE_OVERRIDE_BARCODE_idefix', user_id=user_id) == 'true'

            if use_override_setting or (not barcode and (src.use_random_barcode or use_random_setting)):
                barcode = generate_random_barcode()
            
            # Zorunlu alanları XML'den çek (xml_item.raw_data içerisinde hepsi var)
            raw = json.loads(xml_item.raw_data)
            
            # Marka ve Kategori Çözümü
            brand_id = 0
            # 1. Öncelik: Ayarlardaki Marka ID
            default_brand_id = Setting.get("IDEFIX_BRAND_ID", user_id=user_id)
            if default_brand_id and default_brand_id.strip():
                try:
                    brand_id = int(default_brand_id)
                except:
                    pass
            
            # 2. Öncelik: XML'den Çözümle
            if not brand_id:
                brand_id = resolve_idefix_brand(raw.get('brand'), user_id=user_id)
            
            cat_id = resolve_idefix_category(raw.get('title'), raw.get('category'), user_id=user_id)
            
            if not brand_id or not cat_id:
                reason = "Marka bulunamadı" if not brand_id else "Kategori bulunamadı"
                if job_id: append_mp_job_log(job_id, f"Atlandı ({reason}): {xml_item.stock_code}", level='warning')
                continue

            final_price = calculate_price(xml_item.price, 'idefix', user_id=user_id)
            
            item = {
                "barcode": barcode,
                "title": xml_item.title,
                "productMainId": raw.get('modelCode') or raw.get('parent_barcode') or xml_item.stock_code,
                "brandId": int(brand_id),
                "categoryId": int(cat_id),
                "inventoryQuantity": xml_item.quantity,
                "vendorStockCode": xml_item.stock_code,
                "description": raw.get('details') or raw.get('description') or xml_item.title,
                "price": final_price,
                "vatRate": raw.get('vatRate', 20.0),
                "images": [img['url'] for img in raw.get('images', []) if img.get('url')]
            }
            create_items.append((item, xml_item))
            
            if job_id: append_mp_job_log(job_id, f"Yeni Ürün Yükleniyor: {xml_item.stock_code} ({xml_item.title[:30]}...)")

        # Toplu Yükleme
        if create_items:
            try:
                payloads = [x[0] for x in create_items]
                client.create_products(payloads)
                
                # Başarılı ise yerel veritabanına MarketplaceProduct olarak ekle
                for item_payload, xml_record in create_items:
                    # Duplicate check
                    existing = MarketplaceProduct.query.filter_by(user_id=user_id, marketplace='idefix', barcode=item_payload['barcode']).first()
                    if not existing:
                        new_mp = MarketplaceProduct(
                            user_id=user_id,
                            marketplace='idefix',
                            barcode=item_payload['barcode'],
                            stock_code=xml_record.stock_code,
                            title=xml_record.title,
                            price=item_payload['price'],
                            sale_price=item_payload['price'],
                            quantity=xml_record.quantity,
                            status='Pending',
                            on_sale=True
                        )
                        db.session.add(new_mp)
                db.session.commit()
                res['created_count'] += len(create_items)
            except Exception as e:
                if job_id: append_mp_job_log(job_id, f"Idefix yükleme hatası: {str(e)}", level='error')

    # --- 3. STOK SIFIRLAMA (Zero) ---
    if to_zero:
        from app.utils.helpers import chunked
        zero_items = []
        for local_item in to_zero:
            zero_items.append({
                'barcode': local_item.barcode,
                'price': local_item.sale_price,
                'inventoryQuantity': 0
            })
            if job_id: append_mp_job_log(job_id, f"Stok Sıfırlanıyor (XML'de yok): {local_item.stock_code}")
            local_item.quantity = 0

        try:
            for batch in chunked(zero_items, 100):
                client.update_inventory_and_price(batch)
                res['zeroed_count'] += len(batch)
            db.session.commit()
        except Exception as e:
            if job_id: append_mp_job_log(job_id, f"Idefix stok sıfırlama hatası: {str(e)}", level='error')

    return res

def resolve_idefix_brand(brand_name: str, user_id: int) -> Optional[int]:
    """Marka isminden Idefix brandId çözümler."""
    if not brand_name: return None
    try:
        client = get_idefix_client(user_id=user_id)
        brand = client.search_brand_by_name(brand_name)
        if brand and brand.get('id'):
            return int(brand['id'])
    except Exception:
        pass
    return None

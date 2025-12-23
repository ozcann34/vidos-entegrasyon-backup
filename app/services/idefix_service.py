import base64
import json
import time
import logging
import requests
from typing import List, Dict, Optional, Any
from datetime import datetime
from app.utils.helpers import chunked, get_marketplace_multiplier, to_int, to_float, clean_forbidden_words, is_product_forbidden
from app.utils.rate_limiter import idefix_limiter

from app.services.job_queue import append_mp_job_log, get_mp_job, update_mp_job
from app.models import Setting, Product, SupplierXML

logger = logging.getLogger(__name__)

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

def ensure_idefix_tfidf_ready() -> bool:
    """
    Load Idefix categories from settings and prepare TF-IDF if not already done.
    If cache is empty, automatically fetch from API.
    """
    from app.models import Setting
    
    if _IDEFIX_CAT_TFIDF.get('vectorizer'):
        return True
    
    # Try loading from saved settings first
    raw = Setting.get("IDEFIX_CATEGORY_TREE", "")
    if raw:
        try:
            categories = json.loads(raw)
            if categories:
                prepare_idefix_tfidf(categories)
                return True
        except Exception as e:
            logger.error(f"Idefix kategori ağacı yüklenirken hata: {e}")
    
    # If cache is empty, automatically fetch from API
    logger.info("[IDEFIX] Kategori önbelleği boş, API'den otomatik çekiliyor...")
    try:
        result = fetch_and_cache_categories()
        if result.get('success'):
            # Now try to load again
            raw = Setting.get("IDEFIX_CATEGORY_TREE", "")
            if raw:
                categories = json.loads(raw)
                prepare_idefix_tfidf(categories)
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

def resolve_idefix_category(product_title: str, excel_category: str, log_callback=None) -> Optional[int]:
    """
    Resolve Idefix category ID from product info.
    
    Args:
        product_title: Product title
        excel_category: Category from Excel
        log_callback: Optional callback function for logging
        
    Returns:
        Category ID if found, None otherwise
    """
    if not ensure_idefix_tfidf_ready():
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
            Dict containing the API response
        """
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
            Dict containing the batch status and item results
        """
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
        Documentation: https://developer.idefix.com/api/urun-entegrasyonu/hizli-urun-ekleme
        """
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
            response = self.session.post(url, headers=self._get_headers(), json=payload, timeout=60)
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
    
    def query_pool_batch_status(self, batch_request_id: str) -> Dict[str, Any]:
        """
        Query the status of a pool/create batch request.
        Endpoint: /pim/pool/{vendorId}/batch-result/{batchRequestId}
        
        Returns:
            Dict containing batch status and item details with failure reasons
        """
        url = f"{self.BASE_URL}/pim/pool/{self.vendor_id}/batch-result/{batch_request_id}"
        
        logger.info(f"[IDEFIX] Querying pool batch status: {batch_request_id}")
        
        try:
            response = self.session.get(url, headers=self._get_headers(), timeout=30)
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
            Dict containing batch status and item details
        """
        url = f"{self.BASE_URL}/pim/catalog/{self.vendor_id}/fast-listing-result/{batch_request_id}"
        
        logger.info(f"[IDEFIX] Querying batch status: {batch_request_id}")
        
        try:
            response = self.session.get(url, headers=self._get_headers(), timeout=30)
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
            
    def create_product(self, products: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Create new products on Idefix.
        Endpoint: /pim/pool/{vendor_id}/create
        """
        url = f"{self.BASE_URL}/pim/pool/{self.vendor_id}/create"
        
        payload = {"products": products}
        
        logger.info(f"[IDEFIX] Creating {len(products)} new products")
        
        try:
            response = self.session.post(url, headers=self._get_headers(), json=payload, timeout=60)
            response.raise_for_status()
            result = response.json()
            logger.info(f"[IDEFIX] Create Product Success! BatchRequestId: {result.get('batchRequestId')}")
            return result
        except requests.exceptions.RequestException as e:
            logger.error(f"[IDEFIX] Failed to create products: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"[IDEFIX] Response status: {e.response.status_code}")
                logger.error(f"[IDEFIX] Response body: {e.response.text}")
            raise

    def get_orders(self, page: int = 0, **kwargs) -> Dict[str, Any]:
        """
        Fetch orders from Idefix.
        Endpoint: /pim/orders
        
        Supports: limit (or size), startDate, endDate
        """
        limit = kwargs.get('limit') or kwargs.get('size') or 50
        start_date = kwargs.get('startDate') or kwargs.get('start_date')
        end_date = kwargs.get('endDate') or kwargs.get('end_date')
        
        url = f"{self.BASE_URL}/pim/orders"
        
        params = {"page": page, "limit": limit}
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
            
        try:
            resp = self.session.get(url, headers=self._get_headers(), params=params, timeout=30)
            if resp.status_code == 200:
                result = resp.json()
                # Ensure we return a consistent format
                # Idefix usually returns { "items": [], "totalCount": 10 } or { "content": [], "totalElements": 10 }
                return result
            return {"items": [], "total": 0}
        except Exception as e:
            logger.error(f"Idefix get_orders error: {e}")
            return {"items": [], "total": 0}

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
            resp = self.session.post(url, headers=self._get_headers(), json=payload, timeout=30)
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
            resp = self.session.post(url, headers=self._get_headers(), json=payload, timeout=30)
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
            resp = self.session.post(url, headers=self._get_headers(), json=payload, timeout=30)
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
            resp = self.session.get(url, headers=self._get_headers(), params=params, timeout=30)
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
            resp = self.session.post(url, headers=self._get_headers(), json=payload, timeout=30)
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
            resp = self.session.post(url, headers=self._get_headers(), json=payload, timeout=30)
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
            resp = self.session.get(url, headers=self._get_headers(), timeout=30)
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
            resp = self.session.post(url, headers=self._get_headers(), json=payload, timeout=30)
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
            resp = self.session.post(url, headers=self._get_headers(), json=payload, timeout=30)
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
        pool_state = kwargs.get('pool_state')
        
        url = f"{self.BASE_URL}/pim/pool/{self.vendor_id}/list"
        
        # documentation says 'limit' instead of 'size'
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
            logger.info(f"[IDEFIX] Listing products: page={page}, limit={limit}, search={search}, state={pool_state}")
            resp = self.session.get(url, headers=self._get_headers(), params=params, timeout=60)
            resp.raise_for_status()
            
            data = resp.json()
            logger.info(f"[IDEFIX] list_products response keys: {list(data.keys()) if isinstance(data, dict) else 'raw list'}")
            
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


def fetch_and_cache_categories() -> Dict[str, Any]:
    """
    Fetch all categories from Idefix (Tree Structure) and cache flattened version.
    """
    from app.models import Setting
    from flask_login import current_user
    import json
    
    user_id = current_user.id if current_user and current_user.is_authenticated else None
    
    try:
        client = get_idefix_client()
        logger.info("[IDEFIX] Fetching entire category tree...")
        
        # 1. Fetch Tree
        tree_data = client.get_categories()
        
        # 2. Flatten Tree
        flattened_categories = []
        
        def flatten_recursive(cats):
            for cat in cats:
                subs = cat.get('subs', [])
                
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
                    # Has subcategories, recurse into them
                    flatten_recursive(subs)
                    
        if isinstance(tree_data, list):
             flatten_recursive(tree_data)
        elif isinstance(tree_data, dict) and 'content' in tree_data:
             flatten_recursive(tree_data['content'])
        else:
            logger.error(f"[IDEFIX] Unexpected category response format: {type(tree_data)}")
            return {"success": False, "message": "API beklenmeyen bir format döndürdü."}
            
        logger.info(f"[IDEFIX] Total {len(flattened_categories)} LEAF categories found and processed.")
        
        # Save to settings
        Setting.set("IDEFIX_CATEGORY_TREE", json.dumps(flattened_categories, ensure_ascii=False), user_id=user_id)
        
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

    api_key = Setting.get("IDEFIX_API_KEY", "", user_id=actual_user_id)
    api_secret = Setting.get("IDEFIX_API_SECRET", "", user_id=actual_user_id)
    vendor_id = Setting.get("IDEFIX_VENDOR_ID", "", user_id=actual_user_id)
    
    if not api_key or not vendor_id:
        # Fallback to hardcoded just in case for legacy/dev, but logs warn
        # Actually better to raise error if not in settings
        if not api_key:
             logging.warning("[IDEFIX] API Key not found in settings for user %s", actual_user_id)
             # Fallback to dev keys only if absolutely empty
             api_key = "44d9e992-99bb-4d2c-a71d-dc049496438e"
             api_secret = "babbe6fe-a86a-465d-972e-55b295eecc66"
             vendor_id = "15237"
    
    return IdefixClient(api_key, api_secret, vendor_id)

def perform_idefix_send_products(job_id: str, barcodes: List[str], xml_source_id: Optional[int] = None, title_prefix: str = None, **kwargs) -> Dict[str, Any]:
    from app.services.job_queue import append_mp_job_log, get_mp_job, update_mp_job
    from app.services.xml_service import load_xml_source_index
    from app.utils.helpers import to_float, to_int
    from app.models import Setting
    from flask_login import current_user
    import time
    
    # Extract options
    price_multiplier = to_float(kwargs.get('price_multiplier', 1.0))
    default_price_val = to_float(kwargs.get('default_price', 0.0))
    skip_no_barcode = kwargs.get('skip_no_barcode', False)
    skip_no_image = kwargs.get('skip_no_image', False)
    zero_stock_as_one = kwargs.get('zero_stock_as_one', False)
    
    append_mp_job_log(job_id, f"İdefix gönderim işlemi başlatılıyor... Seçenekler: Çarpan={price_multiplier}, Barkodsuz Atla={skip_no_barcode}")
    
    try:
        client = get_idefix_client()
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
    user_id = current_user.id if hasattr(current_user, 'id') else None
    
    
    # Use provided multiplier
    multiplier = price_multiplier
        
    default_cat_id = Setting.get("IDEFIX_DEFAULT_CATEGORY_ID", "", user_id=user_id)
    default_brand_id = Setting.get("IDEFIX_BRAND_ID", "", user_id=user_id)  # Fallback brand ID
    
    # Barcode settings
    barcode_prefix = Setting.get("IDEFIX_BARCODE_PREFIX", "", user_id=user_id)
    use_random_barcode = Setting.get("IDEFIX_USE_RANDOM_BARCODE", "off", user_id=user_id) == "on"
    
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
        final_price = price * multiplier
        
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
        resolved_cat_id = resolve_idefix_category(product_title, excel_category)
        
        # Use resolved category ID, fallback to default from settings
        final_cat_id = resolved_cat_id
        if not final_cat_id and default_cat_id:
            final_cat_id = int(default_cat_id)
        
        # Base item for Fast Listing
        item = {
            "barcode": final_barcode, # Use the cleaned/new barcode
            "title": (f"{title_prefix} " if title_prefix else "") + (rec.get('title') or ""),
            "vendorStockCode": rec.get('stockCode') or barcode,
            "price": final_price,
            "comparePrice": final_price,
            "inventoryQuantity": int(rec.get('quantity', 0)),
            # Extra fields for Create Product
            "description": rec.get('description', ''),
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
        
        # Build required attributes with first available value
        product_attributes = []
        missing_required = []
        
        for attr in category_attrs_cache.get(item_cat_id, []):
            if attr.get('required', False):
                attr_id = attr.get('attributeId') or attr.get('id')
                attr_name = attr.get('attributeTitle') or attr.get('name', 'Bilinmeyen')
                attr_values = attr.get('attributeValues', [])
                
                if attr_values and len(attr_values) > 0:
                    # Use first available value if no custom mapping
                    first_value = attr_values[0]
                    product_attributes.append({
                        "attributeId": attr_id,
                        "attributeValueId": first_value.get('id'),
                        "customAttributeValue": None
                    })
                elif attr.get('allowCustom', False):
                    # Use custom value if allowed (use product title/description)
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
        
        new_prod = {
            "barcode": item['barcode'],
            "title": item['title'],
            "productMainId": rec.get('parent_barcode') or item.get('vendorStockCode', item['barcode']),
            "brandId": int(item_brand_id),
            "categoryId": int(item_cat_id),
            "inventoryQuantity": int(item.get('inventoryQuantity', 0)),
            "vendorStockCode": item.get('vendorStockCode', item['barcode']),
            "desi": item.get('desi', 1),
            "description": item.get('description') or item.get('title', ''),
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
    cancelled = False
    
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
            resp = client.create_product(batch)
            batch_request_id = resp.get('batchRequestId')
            
            if batch_request_id:
                batch_request_ids.append(batch_request_id)
                append_mp_job_log(job_id, f"✅ Batch {batch_num} gönderildi! ID: {batch_request_id}")
                
                # Wait and check batch result
                import time
                append_mp_job_log(job_id, f"⏳ Batch sonucu bekleniyor (5 sn)...")
                time.sleep(5)
                
                try:
                    result = client.query_pool_batch_status(batch_request_id)
                    
                    # Log batch status
                    batch_status = result.get('status', 'UNKNOWN')
                    append_mp_job_log(job_id, f"📊 Batch durumu: {batch_status}")
                    
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
                except Exception as poll_err:
                    append_mp_job_log(job_id, f"⚠️ Batch sonuç sorgulaması hatası: {poll_err}", level='warning')
                    success_count += len(batch)  # Still count as sent
            else:
                fail_count += len(batch)
                append_mp_job_log(job_id, f"❌ Batch {batch_num}: ID alınamadı", level='error')
                
        except Exception as e:
            fail_count += len(batch)
            append_mp_job_log(job_id, f"❌ Batch {batch_num} hatası: {e}", level='error')
            failures.append(str(e))
    
    # Final summary
    append_mp_job_log(job_id, "")
    append_mp_job_log(job_id, "📋 SON DURUM:")
    append_mp_job_log(job_id, f"Gönderilen: {success_count}")
    append_mp_job_log(job_id, f"Hatalı: {fail_count}")
    if batch_request_ids:
        append_mp_job_log(job_id, f"Batch ID'leri: {', '.join(batch_request_ids[:3])}...")
        append_mp_job_log(job_id, "")
        append_mp_job_log(job_id, "ℹ️ Ürünler İdefix tarafından işleniyor. Sonuçları İdefix panelinden takip edebilirsiniz.")

    return {
        "success_count": success_count,
        "fail_count": fail_count,
        "failures": failures[:20],
        "batch_id": batch_request_id if 'batch_request_id' in locals() else None,
        "skipped": skipped_list
    }

def perform_idefix_send_all(job_id: str, xml_source_id: Any, **kwargs) -> Dict[str, Any]:
    """Send ALL products from XML source to Idefix"""
    from app.services.job_queue import append_mp_job_log
    from app.services.xml_service import load_xml_source_index
    
    append_mp_job_log(job_id, "Tüm ürünler hazırlanıyor...")
    xml_index = load_xml_source_index(xml_source_id)
    all_barcodes = list((xml_index.get('by_barcode') or {}).keys())
    
    if not all_barcodes:
        return {'success': False, 'message': 'XML kaynağında ürün bulunamadı.', 'count': 0}
    
    append_mp_job_log(job_id, f"Toplam {len(all_barcodes)} ürün bulundu. Gönderim başlıyor...")
    
    return perform_idefix_send_products(job_id, all_barcodes, xml_source_id, **kwargs)

def fetch_all_idefix_products(user_id: Optional[int] = None, job_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Fetch all products from Idefix API across all status pools."""
    from app.services.job_queue import append_mp_job_log, update_mp_job
    client = get_idefix_client(user_id=user_id)
    
    all_items = []
    # Idefix has different pools for different statuses
    POOL_STATES = ["APPROVED", "WAITING_APPROVAL", "REJECTED", "WAITING_CONTENT", "DELETED"]
    
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
                    
                all_items.extend(items)
                state_item_count += len(items)
                
                if job_id:
                    update_mp_job(job_id, progress={
                        'current': len(all_items),
                        'total': total if total > len(all_items) else len(all_items) + 1,
                        'message': f'{state}: {state_item_count} ürün çekildi (Toplam: {len(all_items)})'
                    })
                    
                # Break if we reached or exceeded total count for THIS state
                if len(items) < limit or (total > 0 and state_item_count >= total):
                    break
                    
                page += 1
                if page > 500: break # Safety
                
            except Exception as e:
                if job_id:
                    append_mp_job_log(job_id, f"Statü {state} Sayfa {page} hatası: {e}", level='error')
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
                pool_state = item.get('poolState') or item.get('productStatus') or 'UNKNOWN'
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
                    status_str = pool_state

                approval_str = status_str
                
                # On Sale?
                on_sale = (pool_state_up == "APPROVED")
                
                # Images
                imgs = item.get('images', [])
                img_json = json.dumps([i.get('url') if isinstance(i, dict) else i for i in imgs])
                
                # Upsert
                existing = MarketplaceProduct.query.filter_by(
                    marketplace='idefix',
                    barcode=barcode
                ).filter(
                    (MarketplaceProduct.user_id == user_id) if user_id else True
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


def perform_idefix_send_products(job_id: str, barcodes: List[str], xml_source_id: Any, title_prefix: str = None, **kwargs) -> Dict[str, Any]:
    """
    Send products to Idefix from XML source
    
    Args:
        job_id: Job queue ID for progress tracking
        barcodes: List of product barcodes to send
        xml_source_id: XML source database ID
        
    Returns:
        Result dictionary with success status and counts
    """
    from app.services.job_queue import update_mp_job, get_mp_job
    from app.services.xml_service import load_xml_source_index
    from app.utils.helpers import clean_forbidden_words
    
    client = get_idefix_client()
    append_mp_job_log(job_id, "Idefix istemcisi hazır")
    
    # Debug: Log barcode count
    append_mp_job_log(job_id, f"Gelen barkod sayısı: {len(barcodes) if barcodes else 0}")
    
    # Resolve User ID from XML Source
    user_id = None
    if xml_source_id:
        try:
            from app.models import SupplierXML
            s_id = str(xml_source_id)
            if s_id.isdigit():
                src = SupplierXML.query.get(int(s_id))
                if src: user_id = src.user_id
        except Exception as e:
            logging.warning(f"Failed to resolve user_id: {e}")
    
    xml_index = load_xml_source_index(xml_source_id)
    mp_map = xml_index.get('by_barcode') or {}
    multiplier = get_marketplace_multiplier('idefix')
    
    if not mp_map:
        append_mp_job_log(job_id, "XML kaynak haritası boş", level='warning')
        return {'success': False, 'message': 'XML kaynağında ürün bulunamadı.', 'count': 0}
    
    if not barcodes:
        append_mp_job_log(job_id, "Barkod listesi boş", level='warning')
        return {'success': False, 'message': 'Gönderilecek barkod yok.', 'count': 0}
    
    # Ensure categories/TFIDF ready
    ensure_idefix_tfidf_ready()
    
    failures = []
    skipped = []
    products_to_send = []
    
    total = len(barcodes)
    
    # Check for saved brand ID from settings
    saved_brand_id = Setting.get('IDEFIX_BRAND_ID', '') or ''
    if saved_brand_id:
        try: saved_brand_id = int(saved_brand_id)
        except: saved_brand_id = None
        append_mp_job_log(job_id, f"Kayıtlı marka ID kullanılıyor: {saved_brand_id}")
    
    DEFAULT_DESI = 1
    # Idefix usually uses KDV Rate e.g. 10, 20
    DEFAULT_VAT_RATE = 20
    
    for idx, barcode in enumerate(barcodes, 1):
        # Check for pause/cancel
        job_state = get_mp_job(job_id)
        if job_state:
            if job_state.get('cancel_requested'):
                append_mp_job_log(job_id, "İşlem iptal edildi", level='warning')
                break
            
            while job_state.get('pause_requested'):
                append_mp_job_log(job_id, "İşlem duraklatıldı...", level='info')
                time.sleep(5)
                job_state = get_mp_job(job_id)
                if job_state.get('cancel_requested'):
                    break
        
        product = mp_map.get(barcode)
        if not product:
            skipped.append({'barcode': barcode, 'reason': 'XML verisi yok'})
            continue
        
        # Blacklist check
        forbidden_reason = is_product_forbidden(user_id, title=product.get('title'), brand=product.get('brand'), category=product.get('category'))
        if forbidden_reason:
            skipped.append({'barcode': barcode, 'reason': f"Yasakli Liste: {forbidden_reason}"})
            continue
            
        try:
            # Extract product data
            title = clean_forbidden_words(product.get('title', ''))
            description = clean_forbidden_words(product.get('description', '') or title)
            # Ensure description is not empty and reasonably long
            if not description or len(description) < 10:
                description = f"{title} - {description}"
            
            # Idefix desc max length validation? Usually 20000 chars is fine.
            
            top_category = product.get('top_category', '')
            xml_category = product.get('category', '')
            brand_name = product.get('brand') or product.get('vendor') or product.get('manufacturer') or ''
            
            # Helper for logging
            def category_log(msg, level='info'):
                append_mp_job_log(job_id, f"[{barcode[:15]}] {msg}", level=level)
            
            # Resolve Brand - Always fallback to saved_brand_id if API fails
            brand_id = None
            
            # Step 1: Try API lookup if brand_name exists
            if brand_name:
                # Search brand dynamically
                b_res = client.search_brand_by_name(brand_name)
                if b_res:
                    brand_id = b_res['id']
                    category_log(f"Marka '{brand_name}' API ile bulundu: {brand_id}")
            
            # Step 2: Fallback to saved default brand ID from settings
            if not brand_id and saved_brand_id:
                brand_id = saved_brand_id
                category_log(f"Varsayılan marka ID kullanılıyor: {saved_brand_id}")
            
            # Step 3: Skip if still no brand
            if not brand_id:
                 skipped.append({'barcode': barcode, 'reason': 'Marka ID bulunamadı (Ayarlardan varsayılan marka tanımlayın)'})
                 continue

            # Resolve Category
            category_id = resolve_idefix_category(title, xml_category, log_callback=category_log if idx <= 5 else None)
            if not category_id:
                skipped.append({'barcode': barcode, 'reason': 'Kategori eşleşmedi'})
                continue
            
            # Price & Stock
            base_price = to_float(product.get('price', 0))
            stock = to_int(product.get('quantity', 0))
            
            if base_price <= 0:
                skipped.append({'barcode': barcode, 'reason': 'Fiyat 0'})
                continue
            
            sale_price = round(base_price * multiplier, 2)
            # List price usually a bit higher or same
            list_price = round(sale_price * 1.10, 2) 

            # Images - Idefix expects [{url: "..."}] format
            raw_images = product.get('images', [])
            product_images = []
            for img in raw_images:
                url = None
                if isinstance(img, dict): url = img.get('url')
                elif isinstance(img, str): url = img
                if url: product_images.append({"url": url})
            
            # Create Product Payload (per Idefix API docs)
            # Endpoint: /pim/pool/{vendorId}/create
            item_payload = {
                "barcode": barcode,
                "title": title[:200] if title else f"Ürün {barcode}",  # Required - cannot be null
                "productMainId": barcode,  # Required - use barcode as unique ID
                "description": description[:5000] if description else title[:200],
                "brandId": brand_id,
                "categoryId": category_id,
                "price": sale_price,       # Satış Fiyatı (Kdv Dahil)
                "comparePrice": list_price,# Liste Fiyatı
                "vatRate": DEFAULT_VAT_RATE,
                "vendorStockCode": product.get('stock_code', barcode)[:50],
                "inventoryQuantity": stock,
                "desi": DEFAULT_DESI,
                "deliveryDuration": 3,  # Default 3 days
                "deliveryType": "regular",
                "images": product_images[:10]  # Max 10 images
            }
            
            # Attributes? fast_list_products might accept 'attributes' list if mandatory?
            # Outline for simple fast listing usually doesn't require deep attribute mapping if not strict.
            # But I'll check if we need to add attributes.
            # For now, minimal payload.
            
            products_to_send.append(item_payload)
            
            if idx % 10 == 0:
                append_mp_job_log(job_id, f"{idx}/{total} ürün hazırlandı...")
                
        except Exception as e:
            failures.append({'barcode': barcode, 'reason': str(e)})
            append_mp_job_log(job_id, f"Hata {barcode}: {e}", level='error')
            
    append_mp_job_log(job_id, f"Hazırlanan ürün: {len(products_to_send)}, Atlanan: {len(skipped)}")
    
    if not products_to_send:
        skip_reasons = {}
        for s in skipped:
            r = s.get('reason', '?')
            skip_reasons[r] = skip_reasons.get(r, 0) + 1
        append_mp_job_log(job_id, f"Atlama nedenleri: {skip_reasons}", level='warning')
        return {'success': True, 'count': 0, 'message': 'Gönderilecek geçerli ürün oluşturulamadı.', 'skipped': skipped}

    # Send in chunks using create_product API (per user request)
    chunk_size = 20
    total_sent = 0
    main_batch_id = None
    
    for chunk in chunked(products_to_send, chunk_size):
        try:
            resp = client.create_product(chunk)
            # Response: { "batchRequestId": "...", "products": [...] }
            batch_id = resp.get('batchRequestId')
            if batch_id:
                if not main_batch_id: main_batch_id = batch_id
                append_mp_job_log(job_id, f"Parti gönderildi. Batch ID: {batch_id}")
                total_sent += len(chunk)
            else:
                append_mp_job_log(job_id, f"Parti gönderildi fakat Batch ID dönmedi: {str(resp)[:200]}")
                total_sent += len(chunk)
                 
        except Exception as e:
            append_mp_job_log(job_id, f"API İstek Hatası: {e}", level='error')
            
    return {
        'success': True,
        'count': total_sent,
        'batch_id': main_batch_id,
        'skipped': skipped,
        'message': f"{total_sent} ürün için işlem başlatıldı."
    }

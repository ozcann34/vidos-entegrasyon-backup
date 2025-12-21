
import requests
import logging
from typing import Dict, Any, List
from app.utils.rate_limiter import hepsiburada_limiter

class HepsiburadaClient:
    def __init__(self, merchant_id: str, service_key: str, api_username: str = "", test_mode: bool = False):
        self.merchant_id = merchant_id
        self.service_key = service_key
        self.api_username = api_username
        self.test_mode = test_mode
        
        # Base URLs
        if test_mode:
            self.listing_api_url = "https://listing-external-sit.hepsiburada.com"
            self.base_api_url = "https://mpop-sit.hepsiburada.com"
            self.oms_api_url = "https://oms-external-sit.hepsiburada.com"
            self.finance_api_url = "https://mpfinance-sit.hepsiburada.com"
        else:
            self.listing_api_url = "https://listing-external.hepsiburada.com"
            self.base_api_url = "https://mpop.hepsiburada.com"
            self.oms_api_url = "https://oms-external.hepsiburada.com"
            self.finance_api_url = "https://mpfinance.hepsiburada.com"
        
        self.session = requests.Session()
        # Patch session.request
        original_request = self.session.request
        def rate_limited_request(method, url, *args, **kwargs):
            hepsiburada_limiter.wait()
            return original_request(method, url, *args, **kwargs)
        self.session.request = rate_limited_request

    def _get_headers(self) -> Dict[str, str]:
        ua = self.api_username or "glowifystore_dev"
        return {
            "User-Agent": ua,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def check_connection(self) -> Dict[str, Any]:
        """
        Test connection with multiple auth strategies to diagnose 401/403 errors.
        """
        # Endpoint: GET /listings/merchantid/{merchantId}/listings?limit=1
        url = f"{self.listing_api_url}/listings/merchantid/{self.merchant_id}/listings?limit=1"
        m_id = self.merchant_id.strip()
        s_key = self.service_key.strip()
        ua_val = (self.api_username or "glowifystore_dev").strip()
        
        import base64
        auth_val = base64.b64encode(f"{m_id}:{s_key}".encode('utf-8')).decode('utf-8')
        
        # Strategy 1: Defined API Username (User-Agent = api_username, Auth = MerchantID:ServiceKey)
        headers_1 = {
            "Authorization": f"Basic {auth_val}",
            "User-Agent": ua_val,
            "Accept": "application/json"
        }
        
        # Strategy 2: MerchantID as UA (Legacy fallback)
        headers_2 = {
            "Authorization": f"Basic {auth_val}",
            "User-Agent": m_id,
            "Accept": "application/json"
        }
        
        # Strategy 3: Standard Vidos UA (Often fails 403)
        headers_3 = {
            "Authorization": f"Basic {auth_val}",
            "User-Agent": "VidosEntegrasyon/1.0", 
            "Accept": "application/json"
        }

        strategies = [
            (f"API Username UA ({ua_val})", headers_1),
            ("MerchantID UA", headers_2),
            ("App UA (Vidos)", headers_3)
        ]
        
        results = {}
        
        for name, headers in strategies:
            try:
                logging.info(f"Checking HB Connection with Strategy: {name}")
                resp = self.session.get(url, headers=headers, timeout=10)
                if resp.ok:
                    logging.info(f"HB Auth Success with {name}")
                    return {"success": True, "strategy": name, "status_code": resp.status_code}
                else:
                    results[name] = f"Status: {resp.status_code}, Resp: {resp.text[:100]}"
            except Exception as e:
                results[name] = str(e)
                
        # If all failed
        logging.error(f"HB All Auth Strategies Failed: {results}")
        return {"success": False, "details": results}

    def import_products_file(self, json_file_content: str, file_name: str = "products.json") -> Dict[str, Any]:
        """
        Upload products via Hepsiburada mPOP Catalog API (File Import).
        Endpoint: POST https://mpop.hepsiburada.com/product/api/products/import?version=1
        """
        url = f"{self.base_api_url}/product/api/products/import?version=1"
        
        headers = self._get_auth_headers()
        # Ensure multipart/form-data doesn't have fixed Content-Type here, requests handles it
        if "Content-Type" in headers:
            del headers["Content-Type"]
            
        files = {
            'file': (file_name, json_file_content, 'application/json')
        }
        
        try:
            resp = self.session.post(url, headers=headers, files=files, timeout=120)
            
            if not resp.ok:
                logging.error(f"HB Import Failed ({resp.status_code}): {resp.text[:200]}")
                if resp.status_code in [401, 403]:
                    # Diagnostic
                    self.check_connection()
                    
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logging.error(f"Hepsiburada import error: {e}")
            raise e

    def create_update_ticket(self, json_file_content: str, file_name: str = "update_ticket.json") -> Dict[str, Any]:
        """
        Create a ticket for updating existing products.
        Endpoint: POST https://mpop.hepsiburada.com/ticket-api/api/integrator/import?version=1
        """
        url = f"{self.base_api_url}/ticket-api/api/integrator/import?version=1"
        
        headers = self._get_auth_headers()
        if "Content-Type" in headers:
            del headers["Content-Type"]
            
        files = {
            'file': (file_name, json_file_content, 'application/json')
        }
        
        try:
            resp = self.session.post(url, headers=headers, files=files, timeout=60)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logging.error(f"Hepsiburada ticket update error: {e}")
            raise e

    def upload_products(self, products: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Upload products via Hepsiburada Listing API (Inventory Upload).
        Endpoint: POST /listings/merchantid/{merchantId}/inventory-uploads
        """
        url = f"{self.listing_api_url}/listings/merchantid/{self.merchant_id}/inventory-uploads"
        headers = self._get_auth_headers()
        
        try:
            resp = self.session.post(url, json=products, headers=headers, timeout=60)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logging.error(f"Hepsiburada listing upload error: {e}")
            raise e

    def check_upload_status(self, tracking_id: str) -> Dict[str, Any]:
        """
        Check status of upload.
        GET /listings/merchantid/{merchantId}/inventory-uploads/id/{trackingId}
        """
        url = f"{self.listing_api_url}/listings/merchantid/{self.merchant_id}/inventory-uploads/id/{tracking_id}"
        headers = self._get_auth_headers()
        
        try:
            resp = self.session.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logging.error(f"Hepsiburada status check error: {e}")
            raise e

    def get_catalog_import_status(self, tracking_id: str) -> Dict[str, Any]:
        """
        Check status of a Catalog Import task.
        Endpoint: GET https://mpop.hepsiburada.com/product/api/products/status/{trackingId}?version=1
        """
        url = f"{self.base_api_url}/product/api/products/status/{tracking_id}?version=1"
        headers = self._get_auth_headers()
        
        try:
            resp = self.session.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logging.error(f"HB Catalog Import status error: {e}")
            raise e

    def search_brands(self, keyword: str, page: int = 0, size: int = 20) -> Dict[str, Any]:
        """
        Search for brands in Hepsiburada.
        Endpoint: GET https://mpop.hepsiburada.com/product/api/brands/search?name={keyword}&page={page}&size={size}&version=1
        """
        # Try both 'name' and 'keyword' as parameters as HB has variations
        for param in ['name', 'keyword']:
            url = f"{self.base_api_url}/product/api/brands/search?{param}={keyword}&page={page}&size={size}&version=1"
            headers = self._get_auth_headers()
            try:
                resp = self.session.get(url, headers=headers, timeout=30)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                continue
        
        logging.error(f"HB Brand Search failed for: {keyword}")
        return {"data": [], "success": False}

    def get_brands_by_category(self, category_id: int) -> Dict[str, Any]:
        """Get list of brands allowed for a category."""
        url = f"{self.base_api_url}/product/api/categories/get-brands-by-category-id/{category_id}?version=1"
        headers = self._get_auth_headers()
        try:
            resp = self.session.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logging.error(f"HB Generic brands by cat error: {e}")
            return {"data": []}

    def get_category_attributes(self, category_id: int) -> Dict[str, Any]:
        """Get attributes for a specific category."""
        url = f"{self.base_api_url}/product/api/categories/get-attributes-by-category-id/{category_id}?version=1"
        headers = self._get_auth_headers()
        try:
            resp = self.session.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logging.error(f"HB get_category_attributes error: {e}")
            return {"data": []}

    def get_orders(self, start_date: str = None, end_date: str = None, page: int = 0, size: int = 50) -> Dict[str, Any]:
        """
        Fetch orders from Hepsiburada using the OMS packages endpoint.
        """
        url = f"{self.oms_api_url}/packages/merchantid/{self.merchant_id}"
        
        params = {
            "offset": page * size,
            "limit": size
        }
        
        try:
            headers = self._get_auth_headers()
            resp = self.session.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logging.warning(f"HB get_orders failed: {e}")
            return {"items": [], "total": 0}

    def get_accounting_transactions(self, order_number: str) -> List[Dict[str, Any]]:
        """
        Fetch detailed accounting transactions for an order.
        """
        url = f"{self.finance_api_url}/transactions/merchantid/{self.merchant_id}"
        params = {"OrderNumber": order_number}
        headers = self._get_auth_headers()
        
        try:
            resp = self.session.get(url, headers=headers, params=params, timeout=20)
            if resp.status_code == 200:
                return resp.json()
            return []
        except Exception as e:
            logging.error(f"HB Finance API error: {e}")
            return []

    def get_order_detail(self, order_id: str) -> Dict[str, Any]:
        url = f"{self.base_api_url}/lineItems/merchantId/{self.merchant_id}/id/{order_id}"
        try:
            headers = self._get_auth_headers()
            resp = self.session.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logging.error(f"Hepsiburada get_order_detail error: {e}")
            raise

    def get_package_tracking(self, package_number: str) -> Dict[str, Any]:
        url = f"{self.base_api_url}/packages/merchantId/{self.merchant_id}/packageNumber/{package_number}"
        try:
            headers = self._get_auth_headers()
            resp = self.session.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logging.error(f"Hepsiburada package tracking error: {e}")
            raise

    def get_product_count(self) -> int:
        url = f"{self.listing_api_url}/listings/merchantid/{self.merchant_id}/listings"
        try:
            headers = self._get_auth_headers()
            resp = self.session.get(url, headers=headers, params={"limit": 1}, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                return int(data.get("total", 0))
            return 0
        except Exception as e:
            logging.error(f"Hepsiburada get_product_count error: {e}")
            return 0

    def list_products(self, page: int = 0, limit: int = 50, search: str = None) -> Dict[str, Any]:
        """
        List merchant products from Hepsiburada.
        Tries multiple endpoints as HB API structure varies.
        """
        headers = self._get_auth_headers()
        offset = page * limit
        size = min(limit, 100)
        
        # Endpoints to try in order
        endpoints = [
            # 1. mPOP Products API
            (f"{self.base_api_url}/product/api/products/merchantid/{self.merchant_id}", 
             {"version": 1, "page": page, "size": size}),
            # 2. Listing API - listings
            (f"{self.listing_api_url}/listings/merchantid/{self.merchant_id}/listings", 
             {"offset": offset, "limit": size}),
            # 3. Listing API - sku-list
            (f"{self.listing_api_url}/listings/merchantid/{self.merchant_id}/sku-list", 
             {"offset": offset, "limit": size}),
            # 4. mPOP - product list (alternative structure)
            (f"{self.base_api_url}/product/api/products/get-by-merchant/{self.merchant_id}", 
             {"version": 1, "page": page, "size": size}),
        ]
        
        last_error = None
        for url, params in endpoints:
            if search:
                params["productName"] = search
            
            try:
                logging.info(f"HB Listing: Trying {url}")
                resp = self.session.get(url, headers=headers, params=params, timeout=60)
                
                if resp.status_code == 200:
                    data = resp.json()
                    logging.info(f"HB Listing: Success with {url}, keys: {list(data.keys()) if isinstance(data, dict) else 'list'}")
                    return data
                elif resp.status_code == 404:
                    logging.info(f"HB Listing: 404 for {url}, trying next...")
                    last_error = f"404 Not Found - Tüm endpoint'ler başarısız"
                    continue
                elif resp.status_code in (401, 403):
                    logging.warning(f"HB Auth Error ({resp.status_code}) for {url}")
                    self.check_connection()
                    last_error = f"Auth Error: {resp.status_code}"
                    break
                else:
                    logging.warning(f"HB Listing: {resp.status_code} for {url}: {resp.text[:200]}")
                    last_error = f"HTTP {resp.status_code}"
                    
            except Exception as e:
                logging.error(f"HB Listing error for {url}: {e}")
                last_error = str(e)
        
        raise Exception(f"Hepsiburada ürün listesi alınamadı. Son hata: {last_error}")

    def get_changeable_cargo_companies(self, order_line_id: str) -> List[Dict[str, Any]]:
        url = f"{self.base_api_url}/delivery/changeableCargoCompanies/merchantId/{self.merchant_id}/orderLineId/{order_line_id}"
        try:
            headers = self._get_auth_headers()
            resp = self.session.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logging.error(f"Hepsiburada get_changeable_cargo_companies error: {e}")
            return []

    def update_cargo_company(self, order_line_id: str, cargo_company: str) -> Dict[str, Any]:
        url = f"{self.base_api_url}/lineItems/merchantId/{self.merchant_id}/orderLineId/{order_line_id}/cargoCompany"
        payload = {"cargoCompany": cargo_company}
        try:
            headers = self._get_auth_headers()
            resp = self.session.put(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            return resp.json() if resp.text else {"success": True}
        except Exception as e:
            logging.error(f"Hepsiburada update_cargo_company error: {e}")
            raise

    def cancel_order_line(self, order_line_id: str, reason: str) -> Dict[str, Any]:
        url = f"{self.base_api_url}/lineItems/merchantId/{self.merchant_id}/id/{order_line_id}/cancelByMerchant"
        payload = {"reason": reason}
        try:
            headers = self._get_auth_headers()
            resp = self.session.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            return resp.json() if resp.text else {"success": True}
        except Exception as e:
            logging.error(f"Hepsiburada cancel_order_line error: {e}")
            raise

    def send_invoice(self, package_number: str, invoice_data: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_api_url}/packages/merchantId/{self.merchant_id}/packageNumber/{package_number}/invoice"
        try:
            headers = self._get_auth_headers()
            resp = self.session.put(url, headers=headers, json=invoice_data, timeout=30)
            resp.raise_for_status()
            return resp.json() if resp.text else {"success": True}
        except Exception as e:
            logging.error(f"Hepsiburada send_invoice error: {e}")
            raise

    def _get_auth_headers(self) -> Dict[str, str]:
        """Helper method with correct auth format and User-Agent"""
        import base64
        m_id = self.merchant_id.strip()
        s_key = self.service_key.strip()
        ua_val = (self.api_username or "glowifystore_dev").strip()
        
        # Correct format: merchantId:serviceKey
        auth_string = f"{m_id}:{s_key}"
        encoded = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
        
        return {
            "Authorization": f"Basic {encoded}",
            "User-Agent": ua_val, 
            "Accept": "application/json",
            "Content-Type": "application/json"
        }



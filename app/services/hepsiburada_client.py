
import requests
import logging
from typing import Dict, Any, List
from app.utils.rate_limiter import hepsiburada_limiter

class HepsiburadaClient:
    def __init__(self, merchant_id: str, service_key: str):
        self.merchant_id = merchant_id
        self.service_key = service_key
        # Base URLs
        self.listing_api_url = "https://listing-external.hepsiburada.com"
        self.base_api_url = "https://mpop.hepsiburada.com"
        
        self.session = requests.Session()
        
        # Retry mechanism for DNS/Connection issues
        from urllib3.util.retry import Retry
        from requests.adapters import HTTPAdapter
        
        retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=frozenset(['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS']),
            raise_on_status=False
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        # Patch session.request
        original_request = self.session.request
        def rate_limited_request(method, url, *args, **kwargs):
            # Default timeout for all requests if not specified
            if 'timeout' not in kwargs:
                kwargs['timeout'] = 20
                
            hepsiburada_limiter.wait()
            return original_request(method, url, *args, **kwargs)
        self.session.request = rate_limited_request

    def _get_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": "VidosEntegrasyon/1.0",
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
        
        import base64
        
        # Strategy 1: New Integrator (User-Agent = MerchantID, Auth = MerchantID:ServiceKey)
        auth_1 = base64.b64encode(f"{m_id}:{s_key}".encode('utf-8')).decode('utf-8')
        headers_1 = {
            "Authorization": f"Basic {auth_1}",
            "User-Agent": f"{m_id}",
            "Accept": "application/json"
        }
        
        # Strategy 2: HB App Identity (UA: VidosE_MerchantID)
        headers_2 = {
            "Authorization": f"Basic {auth_1}",
            "User-Agent": f"VidosE_{m_id}", 
            "Accept": "application/json"
        }
        
        # Strategy 3: Standard UA
        headers_3 = {
            "Authorization": f"Basic {auth_1}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json"
        }

        strategies = [
            ("New Integrator (UA=ID)", headers_1),
            ("HB Identity (UA=VidosE_ID)", headers_2),
            ("Standard Browser UA", headers_3)
        ]
        
        results = {}
        
        for name, headers in strategies:
            try:
                resp = self.session.get(url, headers=headers, timeout=15)
                if resp.ok:
                    return {"success": True, "strategy": name, "status_code": resp.status_code}
                else:
                    results[name] = f"Status: {resp.status_code}, Resp: {resp.text[:100]}"
            except Exception as e:
                results[name] = str(e)
                
        # If all failed
        logging.error(f"HB All Auth Strategies Failed: {results}")
        return {"success": False, "details": results}

    def _get_auth_headers(self) -> Dict[str, str]:
        """Helper method with correct auth format"""
        import base64
        m_id = self.merchant_id.strip()
        s_key = self.service_key.strip()
        encoded = base64.b64encode(f"{m_id}:{s_key}".encode('utf-8')).decode('utf-8')
        
        return {
            "Authorization": f"Basic {encoded}",
            "User-Agent": f"{m_id}",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    def import_products_file(self, json_file_content: str, file_name: str = "products.json") -> Dict[str, Any]:
        """
        Upload products via Hepsiburada Catalog/Product API (File Import).
        Endpoint: POST https://mpop.hepsiburada.com/product/api/products/import
        """
        # Production URL for Catalog Import
        url = "https://mpop.hepsiburada.com/product/api/products/import"
        
        m_id = self.merchant_id.strip()
        s_key = self.service_key.strip()
        
        # Manual Basic Auth as requested by user debugging
        import base64
        auth_str = f"{m_id}:{s_key}"
        encoded_auth = base64.b64encode(auth_str.encode('utf-8')).decode('utf-8')
        
        headers = {
            "Authorization": f"Basic {encoded_auth}",
            "User-Agent": "VidosEntegrasyon/1.0"
        }
        
        # Multipart upload
        files = {
            'file': (file_name, json_file_content, 'application/json')
        }
        
        logging.info(f"HB Import URL: {url} | Auth Length: {len(encoded_auth)}")
        
        try:
            # Note: headers usually shouldn't include Content-Type for multipart, requests adds it with boundary
            resp = self.session.post(url, headers=headers, files=files, timeout=40)
            
            if resp.status_code == 403:
                 logging.error(f"HB 403 Forbidden (Import). Check exact permissions for 'Product Import'.")
                 # Check connection to simple endpoint to see if auth is valid at least
                 conn_test = self.check_connection()
                 logging.info(f"Connection Test: {conn_test}")
                 
            if resp.status_code == 401:
                logging.error(f"HB 401 Unauthorized (Import). Check MerchantID: {m_id[:5]}***")
                
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logging.error(f"Hepsiburada import error: {e}")
            if hasattr(e, 'response') and e.response:
                logging.error(f"HB Response: {e.response.text}")
            raise e

    def upload_products(self, products: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Upload products via Hepsiburada Listing API (Inventory Upload).
        Endpoint: POST /listings/merchantid/{merchantId}/inventory-uploads
        """
        url = f"{self.listing_api_url}/listings/merchantid/{self.merchant_id}/inventory-uploads"
        
        m_id = self.merchant_id.strip()
        s_key = self.service_key.strip()
        
        headers = self._get_auth_headers()
        logging.info(f"HB Listing Upload URL: {url}")
        
        try:
            resp = self.session.post(url, json=products, headers=headers, timeout=30)
            
            if resp.status_code == 401:
                logging.error(f"HB 401 Unauthorized (Listing). Check MerchantID/Key. UA used: {m_id}")
                # Run diagnostic to find working strategy
                diag = self.check_connection()
                logging.error(f"Diagnostic Result: {diag}")
                
            elif resp.status_code == 403:
                logging.error(f"HB 403 Forbidden (Listing). Check permissions.")
                # Try connection check
                self.check_connection()
                
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logging.error(f"Hepsiburada listing upload error: {e}")
            if hasattr(e, 'response') and e.response:
                logging.error(f"HB Response: {e.response.text}")
            raise e

    def check_upload_status(self, tracking_id: str) -> Dict[str, Any]:
        """
        Check status of upload.
        GET /listings/merchantid/{merchantId}/inventory-uploads/id/{trackingId}
        """
        url = f"{self.listing_api_url}/listings/merchantid/{self.merchant_id}/inventory-uploads/id/{tracking_id}"
        auth = (self.merchant_id, self.service_key)
        
        try:
            resp = self.session.get(url, auth=auth, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logging.error(f"Hepsiburada status check error: {e}")
            raise e

    def get_orders(self, start_date: str = None, end_date: str = None, page: int = 0, size: int = 50) -> Dict[str, Any]:
        """
        Fetch orders from Hepsiburada using the OMS packages endpoint.
        Host: https://oms-external.hepsiburada.com
        Endpoint: /packages/merchantid/{merchantId}
        """
        # separate host for OMS often required
        oms_url = "https://oms-external.hepsiburada.com"
        url = f"{oms_url}/packages/merchantid/{self.merchant_id}"
        
        params = {
            "offset": page * size,
            "limit": size
        }
        
        try:
            headers = self._get_auth_headers()
            resp = self.session.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            
            # Mapping packages response to generic structure
            # Packages usually returns a list of packages which are effectively orders or sub-orders.
            return resp.json()
        except requests.exceptions.RequestException as e:
            logging.warning(f"Hepsiburada get_orders connection failed: {e}")
            return {"items": [], "total": 0}
        except Exception as e:
            logging.error(f"Hepsiburada get_orders error: {e}")
            return {"items": [], "total": 0}

    def get_order_detail(self, order_id: str) -> Dict[str, Any]:
        """
        GET /lineItems/merchantId/{merchantId}/id/{id}
        """
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
        """
        GET /packages/merchantId/{merchantId}/packageNumber/{packageNumber}
        """
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
        """
        Get total product count from Listings API.
        GET /listings/merchantid/{merchantId}/listings?limit=1
        """
        url = f"{self.listing_api_url}/listings/merchantid/{self.merchant_id}/listings"
        
        try:
            headers = self._get_auth_headers()
            # Just request 1 item to get total count
            resp = self.session.get(url, headers=headers, params={"limit": 1}, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                return int(data.get("total", 0))
            return 0
        except Exception as e:
            logging.error(f"Hepsiburada get_product_count error: {e}")
            return 0

    def get_products(self, offset: int = 0, limit: int = 100) -> Dict[str, Any]:
        """
        Fetch product listings with pagination.
        GET /listings/merchantid/{merchantId}/listings
        """
        url = f"{self.listing_api_url}/listings/merchantid/{self.merchant_id}/listings"
        params = {
            "offset": offset,
            "limit": limit
        }
        try:
            headers = self._get_auth_headers()
            resp = self.session.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logging.error(f"Hepsiburada get_products error: {e}")
            return {"items": [], "total": 0}

    def get_changeable_cargo_companies(self, order_line_id: str) -> List[Dict[str, Any]]:
        """
        Get list of changeable cargo companies for an order line.
        GET /delivery/changeableCargoCompanies/merchantId/{merchantId}/orderLineId/{orderLineId}
        """
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
        """
        Update cargo company for an order line.
        PUT /lineItems/merchantId/{merchantId}/orderLineId/{id}/cargoCompany
        """
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
        """
        Cancel an order line by merchant.
        POST /lineItems/merchantId/{merchantId}/id/{lineId}/cancelByMerchant
        """
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
        """
        Send invoice for a package.
        PUT /packages/merchantId/{merchantId}/packageNumber/{packageNumber}/invoice
        """
        url = f"{self.base_api_url}/packages/merchantId/{self.merchant_id}/packageNumber/{package_number}/invoice"
        
        try:
            headers = self._get_auth_headers()
            resp = self.session.put(url, headers=headers, json=invoice_data, timeout=30)
            resp.raise_for_status()
            return resp.json() if resp.text else {"success": True}
        except Exception as e:
            logging.error(f"Hepsiburada send_invoice error: {e}")
            raise

    def get_claims(self, status: str = 'NewRequest', offset: int = 0, limit: int = 100) -> List[Dict[str, Any]]:
        """
        Fetch claims (returns) from Hepsiburada.
        GET https://oms-external.hepsiburada.com/claims/merchantId/{merchantId}/status/{status}
        """
        oms_url = "https://oms-external.hepsiburada.com"
        url = f"{oms_url}/claims/merchantId/{self.merchant_id}/status/{status}"
        
        params = {
            "offset": offset,
            "limit": limit
        }
        
        try:
            headers = self._get_auth_headers()
            resp = self.session.get(url, headers=headers, params=params, timeout=30)
            
            if resp.status_code == 403:
                logging.warning(f"Hepsiburada get_claims: 403 Forbidden - insufficient API permissions for merchant {self.merchant_id}")
                return []
                
            resp.raise_for_status()
            return resp.json() # Returns a list of claims
        except Exception as e:
            logging.error(f"Hepsiburada get_claims error: {e}")
            return []

    def approve_claim(self, claim_number: str) -> Dict[str, Any]:
        """
        Approve/Accept a claim.
        POST https://oms-external.hepsiburada.com/claims/number/{claimNumber}/approve
        """
        oms_url = "https://oms-external.hepsiburada.com"
        url = f"{oms_url}/claims/number/{claim_number}/approve"
        
        try:
            headers = self._get_auth_headers()
            resp = self.session.post(url, headers=headers, timeout=30)
            resp.raise_for_status()
            return resp.json() if resp.text else {"success": True}
        except Exception as e:
            logging.error(f"Hepsiburada approve_claim error: {e}")
            raise

    def reject_claim(self, claim_number: str, reason_code: str, explanation: str = '') -> Dict[str, Any]:
        """
        Reject a claim.
        POST https://oms-external.hepsiburada.com/claims/number/{claimNumber}/reject
        """
        oms_url = "https://oms-external.hepsiburada.com"
        url = f"{oms_url}/claims/number/{claim_number}/reject"
        
        payload = {
            "reasonCode": reason_code,
            "explanation": explanation
        }
        
        try:
            headers = self._get_auth_headers()
            resp = self.session.post(url, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            return resp.json() if resp.text else {"success": True}
        except Exception as e:
            logging.error(f"Hepsiburada reject_claim error: {e}")
            raise

    def _get_auth_headers(self) -> Dict[str, str]:
        """Helper method with correct auth format"""
        import base64
        m_id = self.merchant_id.strip()
        s_key = self.service_key.strip()
        
        # Correct format: merchantId:serviceKey
        auth_string = f"{m_id}:{s_key}"
        encoded = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')
        
        return {
            "Authorization": f"Basic {encoded}",
            "User-Agent": f"VidosE_{m_id}", 
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    def search_brands(self, name: str) -> List[Dict[str, Any]]:
        """Search for brands by name."""
        url = f"{self.base_api_url}/product/api/brands/search"
        params = {"keyword": name}
        headers = self._get_auth_headers()
        try:
            resp = self.session.get(url, headers=headers, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logging.error(f"HB Brand search error: {e}")
            return []

    def get_product_questions(self, status: str = None, sortBy: int = 0, search: str = None) -> Dict[str, Any]:
        """
        Fetch customer questions from Hepsiburada.
        GET https://api-asktoseller-merchant.hepsiburada.com/api/v1.0/issues
        """
        url = "https://api-asktoseller-merchant.hepsiburada.com/api/v1.0/issues"
        params = {"sortBy": sortBy}
        if status: params["status"] = status
        if search: params["search"] = search
        
        try:
            headers = self._get_auth_headers()
            resp = self.session.get(url, headers=headers, params=params, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                logging.error(f"HB Questions 401 Unauthorized. Check credentials. Headers: {headers}")
            if e.response.status_code == 403:
                logging.error(f"HB Questions 403 Forbidden. Check permissions.")
            logging.error(f"Hepsiburada get_product_questions HTTP error: {e}")
            return {"data": [], "totalCount": 0}
        except Exception as e:
            logging.exception(f"Hepsiburada get_product_questions error: {e}")
            return {"data": [], "totalCount": 0}

    def answer_product_question(self, question_number: str, answer_text: str) -> Dict[str, Any]:
        """
        Answer a customer question on Hepsiburada.
        POST https://api-asktoseller-merchant.hepsiburada.com/api/v1.0/issues/{number}/answer
        """
        url = f"https://api-asktoseller-merchant.hepsiburada.com/api/v1.0/issues/{question_number}/answer"
        payload = {
            "Answer": answer_text,
            "Files": []
        }
        
        try:
            headers = self._get_auth_headers()
            resp = self.session.post(url, headers=headers, json=payload, timeout=20)
            resp.raise_for_status()
            return resp.json() if resp.text else {"success": True}
        except Exception as e:
            logging.error(f"Hepsiburada answer_product_question error: {e}")
            raise

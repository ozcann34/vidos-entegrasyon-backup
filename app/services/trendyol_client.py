import os
import time
import json
import logging
import requests
from typing import List, Dict, Any, Optional
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from app.utils.rate_limiter import trendyol_limiter

DEFAULT_TIMEOUT = 60
BATCH_SLEEP_SECONDS = 5

class TrendyolClient:
    def __init__(self, seller_id: str, api_key: str, api_secret: str, timeout: int = DEFAULT_TIMEOUT, cookies_str: Optional[str] = None):
        self.seller_id = str(seller_id)
        self.auth = (api_key, api_secret)
        self.timeout = timeout
        self.session = requests.Session()
        
        # Patch session.request to use rate limiter
        original_request = self.session.request
        def rate_limited_request(method, url, *args, **kwargs):
            trendyol_limiter.wait()
            return original_request(method, url, *args, **kwargs)
        self.session.request = rate_limited_request

        # Default headers (browser-like) to reduce WAF friction

        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/118.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Content-Type": "application/json",
            "Origin": "https://partner.trendyol.com",
            "Referer": "https://partner.trendyol.com/"
        })
        retry = Retry(total=5, backoff_factor=1, status_forcelist=(500, 502, 503, 504, 429))
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.base_product_url = f"https://apigw.trendyol.com/integration/product/sellers/{self.seller_id}"
        self.category_url = "https://apigw.trendyol.com/integration/product/product-categories"
        # Supplier product listing bases (sapigw)
        self.base_supplier_url = f"https://api.trendyol.com/sapigw/suppliers/{self.seller_id}"
        self.base_supplier_url_alt = f"https://apigw.trendyol.com/sapigw/suppliers/{self.seller_id}"
        # Optional cookie string (e.g., cf_clearance, session cookies)
        if cookies_str:
            cookies_str = cookies_str.strip()
            if cookies_str:
                # Attach raw Cookie header (simplest), also populate cookie jar best-effort
                self.session.headers["Cookie"] = cookies_str
                try:
                    for part in cookies_str.split(';'):
                        if '=' in part:
                            k, v = part.split('=', 1)
                            self.session.cookies.set(k.strip(), v.strip(), domain="api.trendyol.com")
                            self.session.cookies.set(k.strip(), v.strip(), domain="apigw.trendyol.com")
                except Exception:
                    pass

    def create_products(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        # Trendyol Product Creation Endpoint (Integration API)
        url = f"https://apigw.trendyol.com/integration/product/sellers/{self.seller_id}/products"
        
        payload = {"items": items}
        
        # Debug: Write first item to file
        if items:
            try:
                with open("trendyol_payload_debug.json", "w", encoding="utf-8") as f:
                    json.dump(items[0], f, ensure_ascii=False, indent=2)
            except:
                pass
        
        # API expects "items" list wrapper
        resp = self.session.post(url, auth=self.auth, json=payload, timeout=self.timeout)
        
        if resp.status_code != 200:
            logging.error(f"Trendyol API Error {resp.status_code}: {resp.text[:500]}")
        
        resp.raise_for_status()
        return resp.json()

    def update_product(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Update product content (Title, Description, Images, etc.).
        PUT https://apigw.trendyol.com/integration/product/sellers/{sellerId}/products
        """
        url = f"https://apigw.trendyol.com/integration/product/sellers/{self.seller_id}/products"
        payload = {"items": items}
        
        logging.info(f"Updating {len(items)} products on Trendyol (Content)")
        resp = self.session.put(url, auth=self.auth, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def check_batch_status(self, batch_request_id: str) -> Dict[str, Any]:
        url = f"{self.base_product_url}/products/batch-requests/{batch_request_id}"
        resp = self.session.get(url, auth=self.auth, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def update_price_inventory(self, updates: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Bulk update price and inventory.
        Tries sapigw suppliers endpoint primarily, with fallbacks.
        """
        payload = {"items": updates}
        candidates = [
            f"{self.base_supplier_url}/products/price-and-inventory",
            f"{self.base_supplier_url_alt}/products/price-and-inventory",
            f"{self.base_product_url}/products/price-and-inventory",
        ]
        last_resp = None
        for i, url in enumerate(candidates):
            try:
                resp = self.session.post(url, auth=self.auth, json=payload, timeout=self.timeout)
                # Some gateways may return 207 Multi-Status or 202 Accepted
                if 200 <= resp.status_code < 300 or resp.status_code in (202, 207):
                    return resp.json() if 'application/json' in (resp.headers.get('Content-Type','')) else {"status_code": resp.status_code, "text": resp.text}
                last_resp = resp
                # If 404/405, try next candidate
                if resp.status_code in (404, 405):
                    continue
                # If 403/5xx, try next candidate as well
                if resp.status_code == 403 or resp.status_code >= 500:
                    continue
                # Otherwise raise
                resp.raise_for_status()
            except requests.RequestException:
                last_resp = resp if 'resp' in locals() else None
                continue
        # If all failed, raise the last response as HTTPError if present
        if last_resp is not None:
            try:
                last_resp.raise_for_status()
            except Exception as e:
                raise e
            return {"status_code": last_resp.status_code, "text": last_resp.text}
        raise RuntimeError("Failed to update price/inventory: no response")


    def delete_products(self, barcodes: List[str]) -> Dict[str, Any]:
        # Trendyol tarafında ürün silme için farklı uç nokta/kural olabilir; burada placeholder bırakıldı
        url = f"{self.base_product_url}/products/delete"
        payload = {"items": [{"barcode": b} for b in barcodes]}
        resp = self.session.post(url, auth=self.auth, json=payload, timeout=self.timeout)
        # Not: Eğer bu uç nokta yoksa 404/405 dönebilir; gerçek dokümantasyona göre güncellenmeli
        return {"status_code": resp.status_code, "text": resp.text}

    def get_category_tree(self) -> Dict[str, Any]:
        resp = self.session.get(self.category_url, auth=self.auth, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_category_attributes(self, category_id: int) -> Dict[str, Any]:
        url = f"https://apigw.trendyol.com/integration/product/product-categories/{category_id}/attributes"
        resp = self.session.get(url, auth=self.auth, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def get_brands_by_name(self, name: str) -> List[Dict[str, Any]]:
        """Search brands by name using Trendyol integration API."""
        url = "https://apigw.trendyol.com/integration/product/brands/by-name"
        try:
            resp = self.session.get(url, auth=self.auth, params={"name": name}, timeout=self.timeout)
            if resp.status_code == 200:
                result = resp.json()
                if isinstance(result, list): return result
                if isinstance(result, dict):
                    for key in ['brands', 'items', 'data', 'content']:
                        if key in result and isinstance(result[key], list): return result[key]
                return []
            return []
        except Exception as e:
            logging.exception(f"Brand search error for '{name}': {e}")
            return []

    def get_brands(self, **kwargs) -> List[Dict[str, Any]]:
        """Alias for get_brands_by_name or get_all_brands depending on params."""
        name = kwargs.get('name')
        if name:
            return self.get_brands_by_name(name)
        res = self.get_all_brands(page=kwargs.get('page', 0), size=kwargs.get('size', 1000))
        return res.get('brands', []) if isinstance(res, dict) else []

    def get_shipment_addresses(self) -> List[Dict[str, Any]]:
        """
        Lightweight call usually for connection testing.
        Official: GET /suppliers/{sellerId}/addresses
        """
        url = f"https://api.trendyol.com/sapigw/suppliers/{self.seller_id}/addresses"
        resp = self.session.get(url, auth=self.auth, timeout=self.timeout)
        resp.raise_for_status()
        # Returns: { "supplierAddresses": [...] }
        data = resp.json()
        return data.get("supplierAddresses", [])

    def get_all_brands(self, page: int = 0, size: int = 1500) -> Dict[str, Any]:
        """
        Fetch all brands from Trendyol API.
        New endpoint: GET https://apigw.trendyol.com/integration/product/brands
        Size must be between 1000-2000
        """
        url = "https://apigw.trendyol.com/integration/product/brands"
        params = {"page": page, "size": size}
        resp = self.session.get(url, auth=self.auth, params=params, timeout=self.timeout)
        if resp.status_code == 200:
            return resp.json()
        # Log error for debugging
        logging.warning(f"Brand fetch failed: {resp.status_code} - {resp.text[:200]}")
        return {"brands": []}

    def list_products(
        self,
        page: int = 1,
        size: int = 200,
        search: Optional[str] = None,
        barcode: Optional[str] = None,
        approved: Optional[bool] = None,
        approval_status: Optional[str] = None,
        on_sale: Optional[bool] = None,
        rejected: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """List products with robust fallbacks.
        1) Try sapigw suppliers products endpoint (supports q, onSale, approvalStatus)
        2) On WAF/5xx or failure, try apigw integration filterProducts endpoint
        """
        # Primary: suppliers endpoint
        url_sup = f"{self.base_supplier_url}/products"
        params_sup: Dict[str, Any] = {"page": page, "size": size}
        if barcode:
            params_sup["barcode"] = barcode
        if search:
            params_sup["q"] = search
        if approved is not None:
            params_sup["approved"] = str(bool(approved)).lower()
        if approval_status is not None:
            params_sup["approvalStatus"] = approval_status
        if on_sale is not None:
            params_sup["onSale"] = str(on_sale).lower()
        if rejected is not None:
            params_sup["rejected"] = str(rejected).lower()

        def _try_sup(p: Dict[str, Any]):
            return self.session.get(url_sup, auth=self.auth, params=p, timeout=self.timeout)

        def _try_sup_alt(p: Dict[str, Any]):
            alt_url = f"{self.base_supplier_url_alt}/products"
            return self.session.get(alt_url, auth=self.auth, params=p, timeout=self.timeout)

        resp = _try_sup(params_sup)
        content_type = resp.headers.get('Content-Type', '')
        waf_or_5xx = (resp.status_code == 403 and 'text/html' in content_type) or (resp.status_code >= 500) or (resp.status_code == 556)
        if waf_or_5xx:
            resp = _try_sup_alt(params_sup)
            if resp.status_code >= 500 or resp.status_code == 556:
                small_params = dict(params_sup)
                small_params['size'] = min(int(params_sup.get('size', 50)), 10)
                for attempt in range(2):
                    time.sleep(1 + attempt)
                    alt = _try_sup_alt(small_params)
                    if 200 <= alt.status_code < 300:
                        resp = alt
                        break
                    pri = _try_sup(small_params)
                    if 200 <= pri.status_code < 300:
                        resp = pri
                        break

        if 200 <= resp.status_code < 300:
            return resp.json()

        # Fallback: integration filterProducts endpoint (usually more stable)
        url_int = f"https://apigw.trendyol.com/integration/product/sellers/{self.seller_id}/products"
        params_int: Dict[str, Any] = {"page": page, "size": size}
        if barcode:
            params_int["barcode"] = barcode
        elif search:
            # integration endpoint supports 'barcode' and limited filters; reuse search as barcode heuristic
            params_int["barcode"] = search
        if approved is not None:
            params_int["approved"] = str(bool(approved)).lower()
        elif approval_status is not None:
            try:
                if isinstance(approval_status, str):
                    approval_status_bool = approval_status.lower() in ("true","1","yes","approved")
                else:
                    approval_status_bool = bool(approval_status)
                params_int["approved"] = str(approval_status_bool).lower()
            except Exception:
                pass
        if on_sale is not None:
            params_int["onSale"] = str(on_sale).lower()
        if rejected is not None:
            params_int["rejected"] = str(rejected).lower()

        try:
            resp_int = self.session.get(url_int, auth=self.auth, params=params_int, timeout=self.timeout)
            resp_int.raise_for_status()
            return resp_int.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                # Log detailed auth info (masking secret)
                masked_key = self.auth[0][:4] + "***" + self.auth[0][-4:] if self.auth[0] else "None"
                masked_secret = "***"
                print(f"[TRENDYOL AUTH ERROR] 401 Unauthorized. Key: {masked_key}, SellerID: {self.seller_id}")
                print(f"[TRENDYOL AUTH ERROR] URL: {url_int}")
                print(f"[TRENDYOL AUTH ERROR] Response: {e.response.text}")
            raise e

    def get_shipment_packages(self, status: Optional[str] = None, page: int = 0, size: int = 50, order_number: Optional[str] = None, start_date: Optional[int] = None, end_date: Optional[int] = None) -> Dict[str, Any]:
        """
        Fetch shipment packages (orders) from Trendyol.
        status: 'Created', 'Picking', 'Invoiced', 'Shipped', 'Cancelled', 'Delivered', 'UnDelivered', 'Returned', 'Repack', 'UnSupplied'
        start_date: Timestamp in milliseconds (GMT+3)
        end_date: Timestamp in milliseconds (GMT+3)
        Note: Max date range is 14 days per Trendyol API
        """
        url = f"https://api.trendyol.com/sapigw/suppliers/{self.seller_id}/orders"
        params = {
            "page": page,
            "size": size,
            "orderByField": "PackageLastModifiedDate",
            "orderByDirection": "DESC"
        }
        if status:
            params["status"] = status
        if order_number:
            params["orderNumber"] = order_number
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
            
        resp = self.session.get(url, auth=self.auth, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # ============================================================
    # Sipariş Durum Güncelleme (Order Status Update)
    # ============================================================
    
    def update_shipment_package_status(
        self, 
        shipment_package_id: int, 
        status: str,
        tracking_number: Optional[str] = None,
        cargo_provider_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Update shipment package status.
        
        Args:
            shipment_package_id: Package ID from order
            status: Target status - 'Picking', 'Invoiced', 'Shipped'
            tracking_number: Cargo tracking number (required for 'Shipped' status)
            cargo_provider_id: Cargo company ID (required for 'Shipped' status)
            
        Returns:
            API response dict
            
        Raises:
            requests.HTTPError: If API request fails
        """
        url = f"https://api.trendyol.com/sapigw/suppliers/{self.seller_id}/shipment-packages/{shipment_package_id}"
        
        payload: Dict[str, Any] = {"status": status}
        
        # For Shipped status, tracking info is required
        if status == "Shipped":
            if tracking_number:
                payload["trackingNumber"] = tracking_number
            if cargo_provider_id:
                payload["cargoProviderName"] = cargo_provider_id
        
        logging.info(f"Updating package {shipment_package_id} to status: {status}")
        resp = self.session.put(url, auth=self.auth, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json() if resp.text else {"success": True}

    # ============================================================
    # Tedarik Edilemedi (Unsupplied Notification)
    # ============================================================
    
    def mark_unsupplied(
        self, 
        shipment_package_id: int, 
        line_items: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Mark items in a shipment package as unsupplied.
        
        Args:
            shipment_package_id: Package ID
            line_items: List of dicts with 'lineId' and 'quantity' keys
                       Example: [{'lineId': 123, 'quantity': 1}]
                       
        Returns:
            API response dict
        """
        url = f"https://api.trendyol.com/sapigw/suppliers/{self.seller_id}/shipment-packages/{shipment_package_id}/unsupplied"
        
        payload = {"lineItems": line_items}
        
        logging.info(f"Marking {len(line_items)} items as unsupplied in package {shipment_package_id}")
        resp = self.session.put(url, auth=self.auth, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json() if resp.text else {"success": True}

    # ============================================================
    # Fatura Entegrasyonu (Invoice Integration)
    # ============================================================
    
    def send_invoice_link(
        self, 
        shipment_package_id: int, 
        invoice_link: str
    ) -> Dict[str, Any]:
        """
        Send invoice link for a shipment package.
        
        Args:
            shipment_package_id: Package ID
            invoice_link: URL of the invoice (PDF link)
            
        Returns:
            API response dict
        """
        url = f"https://api.trendyol.com/sapigw/suppliers/{self.seller_id}/supplier-invoices"
        
        payload = {
            "shipmentPackageId": shipment_package_id,
            "invoiceLink": invoice_link
        }
        
        logging.info(f"Sending invoice link for package {shipment_package_id}")
        resp = self.session.post(url, auth=self.auth, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json() if resp.text else {"success": True}

    # ============================================================
    # Paket Bölme (Split Package)
    # ============================================================
    
    def split_shipment_package(
        self, 
        package_id: int, 
        order_line_ids: List[int],
        tracking_number: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Split a shipment package into multiple packages.
        
        Args:
            package_id: Original package ID
            order_line_ids: List of order line IDs to move to new package
            tracking_number: Optional tracking number for new package
            
        Returns:
            API response dict with new package info
        """
        url = f"https://api.trendyol.com/sapigw/suppliers/{self.seller_id}/shipment-packages/{package_id}/split"
        
        payload: Dict[str, Any] = {"orderLineIds": order_line_ids}
        
        if tracking_number:
            payload["trackingNumber"] = tracking_number
        
        logging.info(f"Splitting package {package_id} with {len(order_line_ids)} line items")
        resp = self.session.post(url, auth=self.auth, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json() if resp.text else {"success": True}

    # ============================================================
    # Müşteri Soruları (Customer Questions)
    # ============================================================
    
    def get_customer_questions(
        self, 
        page: int = 0, 
        size: int = 100,
        status: Optional[str] = None,
        barcode: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get customer questions for products.
        
        Args:
            page: Page number (0-indexed)
            size: Page size (default 100)
            status: Filter by status - 'WAITING_FOR_ANSWER', 'ANSWERED', 'REJECTED'
            barcode: Filter by product barcode
            
        Returns:
            Dict with 'content' list and pagination info
        """
        url = f"https://apigw.trendyol.com/integration/qna/sellers/{self.seller_id}/questions/filter"
        
        params: Dict[str, Any] = {"page": page, "size": size}
        if status:
            params["status"] = status
        if barcode:
            params["barcode"] = barcode
        
        resp = self.session.get(url, auth=self.auth, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def answer_customer_question(
        self, 
        question_id: int, 
        answer_text: str
    ) -> Dict[str, Any]:
        """
        Answer a customer question.
        
        Args:
            question_id: Question ID
            answer_text: Answer text (min 2 characters)
            
        Returns:
            API response dict
        """
        url = f"https://apigw.trendyol.com/integration/qna/sellers/{self.seller_id}/questions/{question_id}/answers"
        
        payload = {"text": answer_text}
        
        logging.info(f"Answering question {question_id}")
        resp = self.session.post(url, auth=self.auth, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json() if resp.text else {"success": True}

    # ============================================================
    # Müşteri Soruları (Customer Questions)
    # ============================================================
    
    def get_questions(
        self, 
        page: int = 0, 
        size: int = 100,
        status: Optional[str] = None,
        barcode: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get customer questions with fallback support.
        
        Tries apigw/integration endpoint first (more widely accessible),
        falls back to sapigw endpoint if 403 Forbidden error occurs.
        
        Args:
            page: Page number (0-indexed)
            size: Page size (default 100)
            status: Filter by status (WAITING_FOR_ANSWER, ANSWERED, REJECTED)
            barcode: Filter by product barcode
            
        Returns:
            Dict with questions list and pagination info
        """
        params: Dict[str, Any] = {"page": page, "size": size}
        if status:
            params["status"] = status
        if barcode:
            params["barcode"] = barcode
        
        # Try apigw/integration endpoint first (more accessible)
        try:
            url = f"https://apigw.trendyol.com/integration/qna/sellers/{self.seller_id}/questions/filter"
            resp = self.session.get(url, auth=self.auth, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 403:
                logging.warning(f"apigw/integration endpoint returned 403, trying sapigw fallback")
                # Fallback to sapigw endpoint
                try:
                    url = f"https://api.trendyol.com/sapigw/suppliers/{self.seller_id}/questions/filter"
                    resp = self.session.get(url, auth=self.auth, params=params, timeout=self.timeout)
                    resp.raise_for_status()
                    return resp.json()
                except requests.exceptions.HTTPError as e2:
                    if e2.response.status_code == 403:
                        # Both endpoints failed with 403 - return empty result with clear message
                        logging.error("Both question endpoints returned 403 - insufficient API permissions")
                        return {
                            "content": [],
                            "totalElements": 0,
                            "totalPages": 0,
                            "page": page,
                            "size": size,
                            "error": "API erişim izni yok. Trendyol API anahtarınızın 'Müşteri Soruları' iznine sahip olduğundan emin olun."
                        }
                    raise
            raise

    def answer_question(self, question_id: int, answer_text: str) -> Dict[str, Any]:
        """
        Answer a customer question.
        
        Tries integration/qna endpoint first, then falls back to sapigw.
        """
        payload = {"text": answer_text}
        
        endpoints = [
            f"https://apigw.trendyol.com/integration/qna/sellers/{self.seller_id}/questions/{question_id}/answers",
            f"https://api.trendyol.com/sapigw/suppliers/{self.seller_id}/questions/{question_id}/answers"
        ]
        
        last_error = None
        
        for url in endpoints:
            try:
                logging.info(f"Answering question {question_id} via {url}")
                resp = self.session.post(url, auth=self.auth, json=payload, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json() if resp.text else {"success": True}
            except Exception as e:
                logging.warning(f"Endpoint {url} failed: {e}")
                last_error = e
                continue
                
        logging.error(f"All answer_question endpoints failed for {question_id}. Last error: {last_error}")
        raise last_error

    # ============================================================
    # İade/Claim Yönetimi (Claims Management)
    # ============================================================
    
    def get_claims(
        self, 
        page: int = 0, 
        size: int = 100,
        claim_status: Optional[str] = None,
        start_date: Optional[int] = None,
        end_date: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Get claims/returns.
        
        Args:
            page: Page number (0-indexed)
            size: Page size (default 100)
            claim_status: Filter by status
            start_date: Start date as timestamp (ms)
            end_date: End date as timestamp (ms)
            
        Returns:
            Dict with claims list and pagination info
        """
        url = f"https://api.trendyol.com/sapigw/suppliers/{self.seller_id}/claims"
        
        params: Dict[str, Any] = {"page": page, "size": size}
        if claim_status:
            params["claimStatus"] = claim_status
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        
        resp = self.session.get(url, auth=self.auth, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def accept_claim(self, claim_id: str) -> Dict[str, Any]:
        """
        Accept a claim/return request.
        
        Tries detailed integration endpoint first, then falls back to sapigw.
        """
        endpoints = [
            f"https://apigw.trendyol.com/integration/claims/{claim_id}/approve",
            f"https://api.trendyol.com/sapigw/suppliers/{self.seller_id}/claims/{claim_id}/approve"
        ]
        
        last_error = None
        
        for url in endpoints:
            try:
                logging.info(f"Accepting claim {claim_id} via {url}")
                resp = self.session.put(url, auth=self.auth, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json() if resp.text else {"success": True}
            except Exception as e:
                logging.warning(f"Endpoint {url} failed: {e}")
                last_error = e
                continue
                
        logging.error(f"All accept_claim endpoints failed for {claim_id}. Last error: {last_error}")
        raise last_error

    def reject_claim(
        self, 
        claim_id: str, 
        reject_reason_id: int,
        reject_reason_text: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Reject a claim/return request.
        
        Tries detailed integration endpoint first, then falls back to sapigw.
        """
        payload: Dict[str, Any] = {"claimRejectReasonId": reject_reason_id}
        if reject_reason_text:
            payload["claimRejectReasonText"] = reject_reason_text
            
        endpoints = [
            f"https://apigw.trendyol.com/integration/suppliers/{self.seller_id}/claims/{claim_id}",
            f"https://api.trendyol.com/sapigw/suppliers/{self.seller_id}/claims/{claim_id}"
        ]
        
        last_error = None
        
        for url in endpoints:
            try:
                logging.info(f"Rejecting claim {claim_id} via {url} with reason {reject_reason_id}")
                resp = self.session.put(url, auth=self.auth, json=payload, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json() if resp.text else {"success": True}
            except Exception as e:
                logging.warning(f"Endpoint {url} failed: {e}")
                last_error = e
                continue
                
        logging.error(f"All reject_claim endpoints failed for {claim_id}. Last error: {last_error}")
        raise last_error

    # ============================================================
    # Kargo Firmaları (Cargo Providers)
    # ============================================================
    
    def get_cargo_providers(self) -> List[Dict[str, Any]]:
        """
        Get list of available cargo providers.
        
        Returns:
            List of cargo provider dicts with 'id' and 'name'
        """
        url = "https://api.trendyol.com/integration/product/cargo-providers"
        
        resp = self.session.get(url, auth=self.auth, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def update_cargo_company(
        self, 
        shipment_package_id: int, 
        cargo_provider_id: int,
        tracking_number: str
    ) -> Dict[str, Any]:
        """
        Change cargo company for a shipment package.
        
        Args:
            shipment_package_id: Shipment package ID
            cargo_provider_id: New cargo provider ID
            tracking_number: New tracking number
            
        Returns:
            API response dict
        """
        url = f"https://api.trendyol.com/sapigw/suppliers/{self.seller_id}/packages/{shipment_package_id}/change-cargo-company"
        
        payload = {
            "cargoProviderCode": cargo_provider_id,
            "trackingNumber": tracking_number
        }
        
        logging.info(f"Changing cargo for package {shipment_package_id} to provider {cargo_provider_id}")
        resp = self.session.put(url, auth=self.auth, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json() if resp.text else {"success": True}

    def send_invoice_link(
        self, 
        shipment_package_id: int, 
        invoice_link: str
    ) -> Dict[str, Any]:
        """
        Send invoice link for a shipment package.
        
        Args:
            shipment_package_id: Shipment package ID
            invoice_link: Invoice URL
            
        Returns:
            API response dict
        """
        url = f"https://api.trendyol.com/sapigw/suppliers/{self.seller_id}/packages/{shipment_package_id}/invoice-link"
        
        payload = {"invoiceLink": invoice_link}
        
        logging.info(f"Sending invoice link for package {shipment_package_id}")
        resp = self.session.put(url, auth=self.auth, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json() if resp.text else {"success": True}

    def mark_unsupplied(
        self, 
        shipment_package_id: int, 
        line_items: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Mark order items as unsupplied (cannot supply).
        
        Args:
            shipment_package_id: Shipment package ID
            line_items: List of line items with quantity and reason
                        [{"orderLineId": xxx, "quantity": 1, "reasonCode": "xxx"}]
            
        Returns:
            API response dict
        """
        url = f"https://api.trendyol.com/sapigw/suppliers/{self.seller_id}/orders/mark-as-unsupplied"
        
        payload = {
            "shipmentPackageId": shipment_package_id,
            "lineItems": line_items
        }
        
        logging.info(f"Marking {len(line_items)} items as unsupplied for package {shipment_package_id}")
        resp = self.session.post(url, auth=self.auth, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json() if resp.text else {"success": True}


    def get_claims(
        self, 
        page: int = 0, 
        size: int = 50,
        claim_status: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        GET /integration/order/sellers/{sellerId}/claims
        Fetch claims (returns/disputes) using correct Trendyol integration endpoint.
        """
        # Correct endpoint confirmed via documentation
        url = f"https://apigw.trendyol.com/integration/order/sellers/{self.seller_id}/claims"
        
        params: Dict[str, Any] = {"page": page, "size": size}
        
        # Use correct parameter name: claimItemStatus instead of claimStatus
        if claim_status:
            params["claimItemStatus"] = claim_status
        
        try:
            logging.info(f"[CLAIMS] Trying endpoint: {url} with params: {params}")
            resp = self.session.get(url, auth=self.auth, params=params, timeout=self.timeout)
            
            # Log response details
            logging.info(f"[CLAIMS] Response status: {resp.status_code}")
            
            resp.raise_for_status()
            
            # Parse and log response
            result = resp.json()
            logging.info(f"[CLAIMS] Response keys: {list(result.keys()) if isinstance(result, dict) else 'not a dict'}")
            
            # Check if response has expected structure
            if isinstance(result, dict):
                content = result.get('content') or result.get('claims') or result.get('data') or []
                total = result.get('totalElements') or result.get('total') or len(content)
                logging.info(f"[CLAIMS] Successfully fetched {len(content)} claims (total: {total})")
            
            return result
            
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code
            response_text = e.response.text[:500] if e.response.text else "No response body"
            logging.error(f"[CLAIMS] Endpoint failed with status {status_code}: {response_text}")
            
            if status_code == 403:
                # Return empty result with clear message for 403
                logging.error("[CLAIMS] 403 Forbidden - insufficient API permissions")
                return {
                    "content": [],
                    "totalElements": 0,
                    "totalPages": 0,
                    "page": page,
                    "size": size,
                    "error": "API erişim izni yok. Trendyol API anahtarınızın 'İade Talepleri' iznine sahip olduğundan emin olun."
                }
            
            # For other errors, raise
            raise
            
        except Exception as e:
            logging.error(f"[CLAIMS] Unexpected error: {e}")
            raise



    def get_questions(
        self, 
        page: int = 0, 
        size: int = 50,
        status: Optional[str] = "WAITING_FOR_ANSWER",
        barcode: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Fetch customer questions from Trendyol.
        status: 'WAITING_FOR_ANSWER', 'ANSWERED', 'REJECTED'
        """
        url = f"https://api.trendyol.com/sapigw/suppliers/{self.seller_id}/questions"
        params = {"page": page, "size": size}
        if status: params["status"] = status
        if barcode: params["barcode"] = barcode
        
        resp = self.session.get(url, auth=self.auth, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def answer_question(self, question_id: int, text: str) -> Dict[str, Any]:
        """
        Answer a customer question.
        """
        url = f"https://api.trendyol.com/sapigw/suppliers/{self.seller_id}/questions/{question_id}/answers"
        payload = {"text": text}
        
        resp = self.session.post(url, auth=self.auth, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json() if resp.text else {"success": True}

    def get_product_count(self) -> int:
        """
        Get total product count from Trendyol.
        Uses filterProducts with size=1.
        """
        url = f"https://apigw.trendyol.com/integration/product/sellers/{self.seller_id}/products"
        try:
            # size=1 to minimize data, just need totalElements
            resp = self.session.get(url, auth=self.auth, params={"page":0, "size":1}, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                return int(data.get("totalElements", 0))
            return 0
        except Exception as e:
            logging.error(f"Trendyol get_product_count error: {e}")
            return 0

def build_attributes_payload(attributes_response: Dict[str, Any], values_dict: Dict[str, Any], images_dict: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    payload = []
    for attr in attributes_response.get("categoryAttributes", []):
        attr_id = attr["attribute"]["id"]
        attr_name = attr["attribute"]["name"]
        allow_custom = attr.get("allowCustom", False)
        attr_values = attr.get("attributeValues", [])
        item: Dict[str, Any] = {"attributeId": attr_id}
        value_name = values_dict.get(attr_name)
        if attr_values and not allow_custom:
            if value_name is None and attr_values:
                item["attributeValueId"] = attr_values[0]["id"]
            else:
                matching = [v for v in attr_values if str(v.get("name", "")).lower() == str(value_name).lower() or str(v.get("id")) == str(value_name)]
                item["attributeValueId"] = (matching[0]["id"] if matching else attr_values[0]["id"]) if attr_values else None
        elif allow_custom and value_name is not None:
            item["customAttributeValue"] = str(value_name)
        if images_dict and attr_name in images_dict:
            item["image"] = images_dict[attr_name]
        payload.append(item)
    return payload

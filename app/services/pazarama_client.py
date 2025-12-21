import logging
import os
import time
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util import Retry
from app.utils.rate_limiter import pazarama_limiter

DEFAULT_TIMEOUT = 60
TOKEN_URL = "https://isortagimgiris.pazarama.com/connect/token"
BASE_URL = "https://isortagimapi.pazarama.com"


class PazaramaClient:
    """Lightweight wrapper around the Pazarama Partner API."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        timeout: int = DEFAULT_TIMEOUT,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.trust_env = False
        adapter = HTTPAdapter(
            max_retries=Retry(
                total=2,
                backoff_factor=0.5,
                status_forcelist=(500, 502, 503, 504),
                allowed_methods=("GET", "POST", "PUT", "DELETE"),
            )
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.6,en;q=0.4",
                "Connection": "keep-alive",
            }
        )
        self._token: Optional[str] = None
        self._token_expire: float = 0

    # ------------------------------------------------------------------
    # Token helpers
    # ------------------------------------------------------------------
    def _token_valid(self) -> bool:
        return bool(self._token) and time.time() < self._token_expire

    def _auth_headers(
        self,
        *,
        include_json_content_type: bool = False,
        extra: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        if not self._token:
            raise RuntimeError("Pazarama token henüz alınmadı.")
        headers: Dict[str, str] = {"Authorization": f"Bearer {self._token}"}
        if include_json_content_type:
            headers["Content-Type"] = "application/json"
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        retry: int = 2,
        raise_for_status: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        last_exc: Optional[Exception] = None
        for attempt in range(retry + 1):
            try:
                pazarama_limiter.wait()
                self.ensure_token()
                hdrs = self.session.headers.copy()
                # Allow caller to override injection of auth header (e.g. token refresh request)
                auth_header = kwargs.pop("_auth_header", None)
                if auth_header is None:
                    hdrs.update(self._auth_headers(include_json_content_type=False))
                else:
                    hdrs.update(auth_header)
                if headers:
                    hdrs.update(headers)
                response = self.session.request(method, url, headers=hdrs, timeout=self.timeout, **kwargs)
                if response.status_code == 401 and attempt < retry:
                    self.get_token()
                    continue
                if response.status_code in {403, 429} and attempt < retry:
                    wait_time = 5 * (attempt + 1)  # 5, 10, 15 seconds
                    logging.warning("Pazarama rate limit (429). Waiting %ds...", wait_time)
                    time.sleep(wait_time)
                    continue
                if raise_for_status and response.status_code >= 400:
                    response.raise_for_status()
                return response
            except Exception as exc:
                last_exc = exc
                wait_for = 5 * (attempt + 1)  # 5, 10, 15 seconds
                logging.warning("Pazarama request error (attempt %s/%s): %s — waiting %ds", attempt + 1, retry + 1, exc, wait_for)
                time.sleep(wait_for)
        if last_exc:
            raise last_exc
        raise RuntimeError("Pazarama request failed without exception.")

    def get_token(self) -> str:
        body = {"grant_type": "client_credentials", "scope": "merchantgatewayapi.fullaccess"}
        resp = self.session.post(
            TOKEN_URL,
            data=body,
            auth=HTTPBasicAuth(self.client_id, self.client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data") or payload
        token = data.get("accessToken") or data.get("access_token")
        if not token:
            raise RuntimeError("Pazarama token alınamadı.")
        expires_in = int(data.get("expiresIn") or data.get("expires_in") or 3600)
        # Renew one minute early to be safe
        self._token = token
        self._token_expire = time.time() + max(60, expires_in - 60)
        return token

    def ensure_token(self) -> None:
        if not self._token_valid():
            self.get_token()

    # ------------------------------------------------------------------
    # Brand helpers
    # ------------------------------------------------------------------
    def get_brands(self, page: int = 1, size: int = 100, name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch brands from Pazarama."""
        url = f"{BASE_URL}/brand/getBrands"
        params = {"Page": page, "Size": size}
        if name:
            params["Name"] = name
        
        # Pazarama brand endpoint might fail if no brands or auth issue, so handle gracefully if needed
        resp = self._request("GET", url, params=params)
        payload = resp.json()
        
        # Standardize return to list of dicts
        data = payload.get("data")
        if isinstance(data, list):
            return data
        return []

    # ------------------------------------------------------------------
    # Category helpers
    # ------------------------------------------------------------------
    def get_category_tree(self, only_leaf: bool = True) -> List[Dict[str, Any]]:
        url = f"{BASE_URL}/category/getCategoryTree"
        resp = self._request("GET", url)
        data = resp.json().get("data", [])
        if not only_leaf:
            return data
        return [c for c in data if c.get("leaf")]

    def get_category_with_attributes(self, category_id: str) -> Dict[str, Any]:
        url = f"{BASE_URL}/category/getCategoryWithAttributes"
        params = {"Id": category_id}
        resp = self._request("GET", url, params=params)
        return resp.json().get("data", {})

    # ------------------------------------------------------------------
    # Product flows
    # ------------------------------------------------------------------
    def create_products(self, products: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not products:
            raise ValueError("En az bir ürün gönderilmelidir.")
        url = f"{BASE_URL}/product/create"
        try:
            resp = self._request(
                "POST",
                url,
                json={"products": products},
                headers={"Content-Type": "application/json"},
            )
        except requests.HTTPError as err:
            response = err.response
            return {"status_code": response.status_code if response else None, "text": response.text if response else str(err)}
        return resp.json()

    def update_product(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not items:
            raise ValueError("En az bir ürün gönderilmelidir.")
        url = f"{BASE_URL}/product/update"
        try:
            resp = self._request(
                "PUT",
                url,
                json={"products": items},
                headers={"Content-Type": "application/json"},
            )
        except requests.HTTPError as err:
            response = err.response
            return {"status_code": response.status_code if response else None, "text": response.text if response else str(err)}
        return resp.json()

    def check_batch(self, batch_id: str) -> Dict[str, Any]:
        """
        Check the status of a product batch request using Pazarama API.
        
        According to Pazarama API documentation, the correct endpoint is:
        GET /product/getProductBatchResult?BatchRequestId={batch_id}
        
        Status values:
        - InProgress = 1
        - Done = 2
        - Error = 3
        
        Note: Batch results are available for 4 hours after creation.
        """
        if not batch_id:
            raise ValueError("Geçerli bir batch_id gerekli.")
        
        # Correct endpoint according to Pazarama API documentation
        url = f"{BASE_URL}/product/getProductBatchResult"
        params = {"BatchRequestId": batch_id}
        
        try:
            resp = self._request("GET", url, params=params, retry=3, raise_for_status=False)
        except Exception as e:
            logging.warning(f"Pazarama batch check failed: {e}")
            return {"status": "ERROR", "error": str(e), "success": 0, "failed": 0, "raw": {}}
        
        if resp.status_code == 404:
            return {"status": "NOT_FOUND", "error": "Batch bulunamadı veya süresi dolmuş (4 saat)", "success": 0, "failed": 0, "raw": resp.json() if resp.content else {}}
        if resp.status_code == 401:
            # Retry once with a fresh token
            self.get_token()
            try:
                resp = self._request("GET", url, params=params, retry=3, raise_for_status=False)
            except Exception as e:
                return {"status": "ERROR", "error": str(e), "success": 0, "failed": 0, "raw": {}}
        
        if resp.status_code >= 400:
            error_text = resp.text if resp.content else f"HTTP {resp.status_code}"
            return {"status": "ERROR", "error": error_text, "success": 0, "failed": 0, "raw": {}}
        
        payload = resp.json()
        data = payload.get("data") or {}
        error_block = payload.get("error") or data.get("error")
        
        # Map numeric status to string
        status_num = data.get("status") or payload.get("status")
        status_map = {1: "IN_PROGRESS", 2: "DONE", 3: "ERROR"}
        status_str = status_map.get(status_num, str(status_num) if status_num else "UNKNOWN")
        
        result: Dict[str, Any] = {
            "status": status_str,
            "status_code": status_num,
            "total": data.get("totalCount") or data.get("totalProductCount") or data.get("total") or 0,
            "success": data.get("successCount") or data.get("success") or 0,
            "failed": data.get("failedCount") or data.get("failed") or 0,
            "batch_result": data.get("batchResult") or [],
            "creation_date": data.get("creationDate"),
            "error": error_block,
            "raw": payload,
        }
        if isinstance(error_block, dict):
            result["errors"] = error_block.get("errors") or []
            if not result.get("error") and error_block.get("message"):
                result["error"] = error_block.get("message")
        return result

    def get_product_count(self) -> int:
        """
        Get total product count from Pazarama.
        """
        try:
            # list_products uses /product/products. We use the same but inspect root JSON for total.
            url = f"{BASE_URL}/product/products"
            params = {"Page": 1, "Size": 1}
            resp = self._request("GET", url, params=params, headers={"Accept": "application/json"})
            data = resp.json()
            
            # Check for total keys in root response
            if "totalCount" in data: return int(data["totalCount"])
            if "totalProductCount" in data: return int(data["totalProductCount"])
            if "total" in data: return int(data["total"])
            
            # Check in 'data' object if it exists and is a dict
            inner_data = data.get("data")
            if isinstance(inner_data, dict):
                if "totalCount" in inner_data: return int(inner_data["totalCount"])
                if "totalProductCount" in inner_data: return int(inner_data["totalProductCount"])
                if "total" in inner_data: return int(inner_data["total"])
            
            # If payload has 'paging' object
            paging = data.get("paging")
            if isinstance(paging, dict):
                 if "totalCount" in paging: return int(paging["totalCount"])
            
            # If no total count found, but we have data list, return its length
            # (If length < size, this IS the total)
            if isinstance(inner_data, list):
                 return len(inner_data)
            
            # Check the 'data' field directly if it's a list
            root_data = data.get("data")
            if isinstance(root_data, list):
                 return len(root_data)

            return 0
        except Exception as e:
            logging.error(f"Pazarama get_product_count error: {e}")
            return 0

    def list_products(
        self,
        approved: Optional[bool] = None,
        code: Optional[str] = None,
        page: int = 1,
        size: int = 200,
    ) -> Dict[str, Any]:
        """Fetch products from Pazarama catalog with optional filters."""
        params: Dict[str, Any] = {
            "Page": max(1, int(page or 1)),
            "Size": max(1, int(size or 1)),
        }
        if approved is not None:
            params["Approved"] = "true" if approved else "false"
        if code:
            params["Code"] = str(code).strip()

        url = f"{BASE_URL}/product/products"
        resp = self._request(
            "GET",
            url,
            params=params,
            headers={"Accept": "application/json"},
        )
        return resp.json()

    def get_product_detail(self, code: str) -> Dict[str, Any]:
        if not code:
            return {}
        payload = {"Code": str(code).strip()}
        url = f"{BASE_URL}/product/getProductDetail"
        resp = self._request(
            "POST",
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        data = resp.json()
        if isinstance(data, dict):
            return data.get("data") or {}
        return {}

    def update_price(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not items:
            raise ValueError("En az bir fiyat kaydı gönderilmelidir.")
        url = f"{BASE_URL}/product/updatePrice-v2"
        resp = self._request(
            "POST",
            url,
            json={"items": items},
            headers={"Content-Type": "application/json"},
        )
        return resp.json()

    def update_stock(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not items:
            raise ValueError("En az bir stok kaydı gönderilmelidir.")
        url = f"{BASE_URL}/product/updateStock-v2"
        resp = self._request(
            "POST",
            url,
            json={"items": items},
            headers={"Content-Type": "application/json"},
        )
        return resp.json()

    def check_listing_state(self, batch_id: str, page: int = 1, page_size: int = 1000) -> Dict[str, Any]:
        if not batch_id:
            raise ValueError("batch_id zorunludur.")
        page = max(1, int(page or 1))
        page_size = max(1, min(int(page_size or 1000), 3000))
        url = f"{BASE_URL}/listing-state/batch-id/{batch_id}/lake-projections"
        params = {"page": page, "pageSize": page_size}
        resp = self._request("GET", url, params=params)
        return resp.json()

    def get_orders(self, page: int = 1, size: int = 50, start_date: Optional[str] = None, end_date: Optional[str] = None) -> Dict[str, Any]:
        """
        Fetch orders from Pazarama using /order/getOrdersForApi.
        start_date and end_date format: YYYY-MM-DD
        """
        url = f"{BASE_URL}/order/getOrdersForApi"
        
        payload = {
            "pageSize": max(1, int(size or 50)),
            "pageNumber": max(1, int(page or 1))
        }
        
        if start_date:
            payload["startDate"] = start_date
        if end_date:
            payload["endDate"] = end_date
            
        resp = self._request(
            "POST",
            url,
            json=payload,
            headers={"Content-Type": "application/json"}
        )
        return resp.json()

    def update_order_items_status_bulk(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Update order items status in bulk.
        PUT /order/updateOrderItemsStatus
        
        items: List of dicts with keys: orderItemId, status, etc.
        """
        if not items:
            return {"success": False, "message": "Gönderilecek item bulunamadı"}
            
        url = f"{BASE_URL}/order/updateOrderItemsStatus"
        
        try:
            resp = self._request(
                "PUT",
                url,
                json={"items": items},
                headers={"Content-Type": "application/json"}
            )
            return resp.json()
        except Exception as e:
            logging.error(f"Pazarama update_order_items_status_bulk error: {e}")
            raise
    # Utility
    # ------------------------------------------------------------------
    def simple_ping(self) -> bool:
        """Small helper to verify credentials quickly."""
        try:
            self.ensure_token()
            return True
        except Exception:
            return False

    def split_order(self, order_id: str, split_items: List[Dict]) -> Dict[str, Any]:
        """
        POST /order/splitOrder
        Split an order into multiple packages.
        
        split_items: List of dicts defining how to split.
        """
        url = f"{BASE_URL}/order/splitOrder"
        
        payload = {
            "orderId": order_id,
            "splitItems": split_items
        }
        
        try:
            resp = self._request("POST", url, json=payload, headers={"Content-Type": "application/json"})
            return resp.json()
        except Exception as e:
            logging.error(f"Pazarama split order error: {e}")
            raise

    def get_product_questions(self, page: int = 1, size: int = 50) -> Dict[str, Any]:
        """
        GET /product/questions
        Fetch questions asked by customers about products.
        """
        url = f"{BASE_URL}/product/questions"
        params = {"page": page, "size": size}
        
        try:
            resp = self._request("GET", url, params=params)
            return resp.json()
        except Exception as e:
            logging.error(f"Pazarama questions error: {e}")
            raise

    def answer_product_question(self, question_id: str, answer: str) -> Dict[str, Any]:
        """
        POST /product/answerQuestion
        Answer a customer question.
        """
        url = f"{BASE_URL}/product/answerQuestion"
        
        payload = {
            "questionId": question_id,
            "answer": answer
        }
        
        try:
            resp = self._request("POST", url, json=payload, headers={"Content-Type": "application/json"})
            return resp.json()
        except Exception as e:
            logging.error(f"Pazarama answer question error: {e}")
            raise

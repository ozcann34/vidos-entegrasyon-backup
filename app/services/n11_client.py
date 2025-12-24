
import requests
import logging
import json
import time
from typing import Dict, Any, List, Optional
from datetime import datetime
from app.utils.rate_limiter import n11_limiter

class N11Client:
    """
    N11 REST API Client
    Documentation References:
    - Orders: GET https://api.n11.com/rest/delivery/v1/shipmentPackages
    - Products: GET https://api.n11.com/ms/product-query
    """
    
    BASE_URL = "https://api.n11.com/rest/delivery/v1"
    PRODUCT_BASE_URL = "https://api.n11.com/ms"
    CATEGORY_BASE_URL = "https://api.n11.com/cdn"
    
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.headers = {
            "appkey": self.api_key,
            "appsecret": self.api_secret,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        self.session = requests.Session()
        self.session.headers.update(self.headers)
        
        # Patch session.request
        original_request = self.session.request
        def rate_limited_request(method, url, *args, **kwargs):
            n11_limiter.wait()
            return original_request(method, url, *args, **kwargs)
        self.session.request = rate_limited_request

    def request(self, method, url, **kwargs):
        return self.session.request(method, url, **kwargs)

    def check_connection(self) -> bool:
        """Test connection by fetching categories (noauth but good specific check) or products"""
        try:
            # Try fetching 1 product to test auth
            res = self.get_products(size=1)
            return res is not None
        except Exception as e:
            logging.error(f"N11 connection check failed: {e}")
            return False

    def get_orders(self, start_date: int = None, end_date: int = None, 
                   status: str = None, page: int = 0, size: int = 100) -> Dict[str, Any]:
        """
        Fetch shipment packages (Orders)
        start_date, end_date: Millisecond timestamps (long)
        status: Created, Picking, Shipped, Cancelled, Delivered, UnPacked, UnSupplied
        """
        url = f"{self.BASE_URL}/shipmentPackages"
        params = {
            "page": page,
            "size": size,
            "orderByDirection": "DESC" # Newest first
        }
        
        if start_date:
            params["startDate"] = start_date
        if end_date:
            params["endDate"] = end_date
        if status:
            params["status"] = status
            
        try:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"N11 get_orders error: {e}")
            return {}

    def get_products(self, page: int = 0, size: int = 20, sale_status: str = None) -> Dict[str, Any]:
        """
        Fetch products using Product Query REST API
        """
        url = f"{self.PRODUCT_BASE_URL}/product-query"
        params = {
            "page": page,
            "size": size
        }
        if sale_status:
            params["saleStatus"] = sale_status
            
        try:
            # Note: pagination params might be different for product-query, doc says page/size in response but check request
            # Doc example: GET : https://api.n11.com/ms/product-query?id=&...
            # Response has "pageable": {"pageNumber": 0, "pageSize": 20}
            # So params usually match Spring Data naming: page, size
            
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"N11 get_products error: {e}")
            return {}

    def get_product_count(self) -> int:
        """Get total count of products"""
        try:
            res = self.get_products(page=0, size=1)
            if res and 'totalElements' in res:
                return int(res['totalElements'])
            return 0
        except:
            return 0

    def get_categories(self) -> List[Dict[str, Any]]:
        """Get all categories (No Auth required according to docs but using headers doesn't hurt)"""
        url = f"{self.CATEGORY_BASE_URL}/categories"
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"N11 get_categories error: {e}")
            return []

    def get_category_attributes(self, category_id: int) -> List[Dict[str, Any]]:
        """
        Get attributes for a specific category.
        Endpoint: https://api.n11.com/cdn/category/{categoryId}/attribute
        """
        # url = f"{self.PRODUCT_BASE_URL}/category/attributes"
        url = f"https://api.n11.com/cdn/category/{category_id}/attribute"
        
        try:
            # Try without params, ID is in path
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            res_data = response.json()
            
            # CDN response usually matches:
            # { "result": {"status": "SUCCESS"}, "categoryAttributes": [...] }
            # But sometimes just the dict.
            if isinstance(res_data, dict):
                 return res_data.get('categoryAttributes') or res_data.get('attributes') or []
            return []
        except Exception as e:
            # logging.error(f"N11 get_category_attributes error: {e}")
            return []


    def update_cargo_info(
        self, 
        shipment_package_id: str, 
        cargo_provider_code: str,
        tracking_number: str
    ) -> Dict[str, Any]:
        """
        Update cargo/tracking information for a shipment package.
        """
        url = f"{self.BASE_URL}/shipmentPackages/{shipment_package_id}/tracking"
        
        payload = {
            "cargoProviderCode": cargo_provider_code,
            "trackingNumber": tracking_number
        }
        
        try:
            logging.info(f"[N11] Updating cargo for package {shipment_package_id}")
            response = self.session.put(url, json=payload, timeout=30)
            response.raise_for_status()
            return response.json() if response.text else {"success": True}
        except Exception as e:
            logging.error(f"N11 update_cargo_info error: {e}")
            raise

    def send_invoice(
        self, 
        shipment_package_id: str, 
        invoice_number: str,
        invoice_date: str
    ) -> Dict[str, Any]:
        """
        Send invoice information for a shipment package.
        """
        url = f"{self.BASE_URL}/shipmentPackages/{shipment_package_id}/invoice"
        
        payload = {
            "invoiceNumber": invoice_number,
            "invoiceDate": invoice_date
        }
        
        try:
            logging.info(f"[N11] Sending invoice for package {shipment_package_id}")
            response = self.session.post(url, json=payload, timeout=30)
            response.raise_for_status()
            return response.json() if response.text else {"success": True}
        except Exception as e:
            logging.error(f"N11 send_invoice error: {e}")
            raise

    def update_order_status(
        self, 
        shipment_package_id: str, 
        status: str
    ) -> Dict[str, Any]:
        """
        Update shipment package status.
        """
        url = f"{self.BASE_URL}/shipmentPackages/{shipment_package_id}/status"
        
        payload = {"status": status}
        
        try:
            logging.info(f"[N11] Updating package {shipment_package_id} status to {status}")
            response = self.session.put(url, json=payload, timeout=30)
            response.raise_for_status()
            return response.json() if response.text else {"success": True}
        except Exception as e:
            logging.error(f"N11 update_order_status error: {e}")
            raise

    def get_shipment_companies(self) -> List[Dict[str, Any]]:
        """
        Get list of shipping companies.
        GET /shipmentCompanies
        """
        url = f"{self.BASE_URL}/shipmentCompanies"
        
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            res_data = response.json()
            return res_data.get('shipmentCompanies', []) if isinstance(res_data, dict) else res_data
        except Exception as e:
            logging.error(f"N11 get_shipment_companies error: {e}")
            return []

    def create_products(self, products: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Create products using N11 Product Loading REST API (Async Task).
        Endpoint: POST https://api.n11.com/ms/product/tasks/product-create
        """
        url = f"{self.PRODUCT_BASE_URL}/product/tasks/product-create"
        
        # Payload according to n11api.txt
        payload = {
            "payload": {
                "integrator": "Vidos",
                "skus": products
            }
        }
        
        try:
            logging.info(f"[N11] Creating {len(products)} products via REST.")
            response = self.session.post(url, json=payload, timeout=60)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"N11 create_products error: {e}")
            return {"result": {"status": "ERROR", "errorMessage": str(e)}}

    def check_task_status(self, task_id: str) -> Dict[str, Any]:
        """
        Check status of a product task.
        Endpoint: POST https://api.n11.com/ms/product/task-details/page-query
        """
        url = f"{self.PRODUCT_BASE_URL}/product/task-details/page-query"
        payload = {
            "taskId": int(task_id),
            "pageable": {
                "page": 0,
                "size": 100
            }
        }

        try:
            response = self.session.post(url, json=payload, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"N11 check_task_status error: {e}")
            return {"status": "ERROR", "reasons": [str(e)]}

    def delete_product_by_seller_code(self, seller_code: str) -> Dict[str, Any]:
        """
        Delete product by seller code.
        N11 REST API does NOT support DELETE method.
        We use Product Update service to set status = 'Suspended' (Unlisted).
        """
        logging.info(f"N11: Soft deleting {seller_code} via update status=Suspended")
        try:
            return self.update_products([{"stockCode": seller_code, "status": "Suspended"}])
        except Exception as e:
            logging.error(f"N11 delete product error: {e}")
            raise

    def update_products(self, products: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Update products (General Update - Status, Description etc.)
        Endpoint: POST https://api.n11.com/ms/product/tasks/product-update
        """
        url = f"{self.PRODUCT_BASE_URL}/product/tasks/product-update"
        payload = {
            "payload": {
                "integrator": "VidosEntegrasyon",
                "skus": products
            }
        }
        try:
            response = self.session.post(url, json=payload, timeout=60)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"N11 update product error: {e}")
            raise

    def update_stock_by_seller_code(self, seller_code: str, quantity: int) -> Dict[str, Any]:
        """
        Update product stock by seller code.
        Endpoint: POST https://api.n11.com/ms/product/stock/sellerCode/{sellerCode}
        """
        url = f"{self.PRODUCT_BASE_URL}/product/stock/sellerCode/{seller_code}"
        payload = {"quantity": quantity}
        try:
            response = self.session.post(url, json=payload, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"N11 update stock error: {e}")
            raise

    def update_price_by_seller_code(self, seller_code: str, price: float, currency_type: str = "TL") -> Dict[str, Any]:
        """
        Update product price by seller code.
        Endpoint: POST https://api.n11.com/ms/product/price/sellerCode/{sellerCode}
        """
        url = f"{self.PRODUCT_BASE_URL}/product/price/sellerCode/{seller_code}"
        payload = {
            "price": price,
            "currencyType": currency_type
        }
        try:
            response = self.session.post(url, json=payload, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"N11 update price error: {e}")
            raise

    def update_products_price_and_stock(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Update products price and stock (Bulk Async Task).
        Endpoint: POST https://api.n11.com/ms/product/tasks/price-stock-update
        Referenced in n11api.txt
        """
        url = f"{self.PRODUCT_BASE_URL}/product/tasks/price-stock-update"
        
        # Payload: { "payload": { "integrator": "...", "skus": [...] } }
        payload = {
            "payload": {
                "integrator": "Vidos",
                "skus": items
            }
        }
        
        try:
            logging.info(f"[N11] Bulk updating price/stock for {len(items)} items.")
            response = self.session.post(url, json=payload, timeout=60)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logging.error(f"N11 update_products_price_and_stock error: {e}")
            raise


def get_n11_client(user_id: int = None):
    from app.models import Setting
    from flask_login import current_user
    
    if user_id is None:
        try:
            user_id = current_user.id if current_user and current_user.is_authenticated else None
        except:
            user_id = None
    
    api_key = Setting.get("N11_API_KEY", user_id=user_id)
    api_secret = Setting.get("N11_API_SECRET", user_id=user_id)
    
    if not api_key or not api_secret:
        return None
        
    return N11Client(api_key, api_secret)

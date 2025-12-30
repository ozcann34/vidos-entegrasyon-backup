
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
            "appKey": self.api_key,
            "appSecret": self.api_secret,
            "appkey": self.api_key,
            "appsecret": self.api_secret,
            "apiKey": self.api_key,
            "apiSecret": self.api_secret,
            "Authorization": f"Basic {self.api_key}:{self.api_secret}", # Some REST variants use basic or custom auth
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
        """Bağlantıyı test et"""
        try:
            # REST Auth Test: Try a simple GET on product-query or orders
            # Some environments prefer CamelCase or all-lowercase headers.
            # We already send both in __init__, but let's try a real call.
            # shipmentPackages is usually more widely authorized than product-query for base integration keys
            url = f"{self.BASE_URL}/shipmentPackages?page=0&size=1"
            response = self.session.get(url, timeout=30)
            
            if response.status_code == 200:
                return True
            else:
                # If 401, try one more time with simple session headers
                logging.error(f"N11 connection test failed: {response.status_code} - {response.text}")
                return False
        except Exception as e:
            logging.error(f"N11 connection test exception: {str(e)}")
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
            
        response = self.session.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def get_product_count(self) -> int:
        """Get total count of products. Raises on auth error."""
        res = self.get_products(page=0, size=1)
        if res and 'totalElements' in res:
            return int(res['totalElements'])
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

    def get_questions(self, page: int = 0, size: int = 100) -> Dict[str, Any]:
        """
        Fetch product questions from N11 (SOAP).
        Service: ProductQuestionService
        Method: GetProductQuestionList
        """
        url = "https://api.n11.com/ws/ProductQuestionService.wsdl"
        
        xml = f"""<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:n11="http://www.n11.com/ws/schemas">
           <soapenv:Header/>
           <soapenv:Body>
              <n11:GetProductQuestionListRequest>
                 <auth>
                    <appKey>{self.api_key}</appKey>
                    <appSecret>{self.api_secret}</appSecret>
                 </auth>
                 <pagingData>
                    <currentPage>{page}</currentPage>
                    <pageSize>{size}</pageSize>
                 </pagingData>
              </n11:GetProductQuestionListRequest>
           </soapenv:Body>
        </soapenv:Envelope>"""
        
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": "GetProductQuestionList"
        }
        
        try:
            response = self.session.post(url, data=xml, headers=headers, timeout=30)
            response.raise_for_status()
            
            # Simple XML parsing (avoiding lxml/elementtree dependency if possible, but safe to use minidom or simple mapping)
            # For now returning raw text or trying to parse if simple
            # Better to use a parser helper if available.
            # We will return the XML text or convert to dict if we had a helper.
            # Assuming the caller (API route) might handle XML parsing or we do it here.
            # Let's try to return a simplified structure by hacking it or just return raw for now.
            # Actually, let's use a simple regex or minidom to extract questions.
            import xml.etree.ElementTree as ET
            root = ET.fromstring(response.content)
            
            # Namespaces
            ns = {'n11': 'http://www.n11.com/ws/schemas'}
            
            questions = []
            # Find productQuestionList ...
            # The structure is usually Body -> GetProductQuestionListResponse -> productQuestionList -> productQuestion
            
            # Strip namespaces for easier parsing
            for elem in root.iter():
                if '}' in elem.tag:
                    elem.tag = elem.tag.split('}', 1)[1]
            
            q_list = root.find(".//productQuestionList")
            if q_list is not None:
                for q in q_list.findall("productQuestion"):
                    questions.append({
                        "id": q.findtext("id"),
                        "text": q.findtext("content"),
                        "createdDate": q.findtext("createdDate"), # Format: 2023-10-25T...
                        "user": q.findtext("user/username") or "Misafir",
                        "public": q.findtext("isPublic"),
                        "answered": q.findtext("status") == "ANSWERED",
                        "product": {
                             "title": q.findtext("product/title"),
                             "n11Id": q.findtext("product/id")
                        }
                    })
                    
            return {"questions": questions, "total": len(questions)} # Paging info might be missing in simple parse
            
        except Exception as e:
            logging.error(f"N11 get_questions error: {e}")
            return {"questions": []}

    def answer_question(self, question_id: str, answer: str) -> Dict[str, Any]:
        """
        Answer a question on N11 (SOAP).
        Service: ProductQuestionService
        Method: SaveProductAnswer
        """
        url = "https://api.n11.com/ws/ProductQuestionService.wsdl"
        
        xml = f"""<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:n11="http://www.n11.com/ws/schemas">
           <soapenv:Header/>
           <soapenv:Body>
              <n11:SaveProductAnswerRequest>
                 <auth>
                    <appKey>{self.api_key}</appKey>
                    <appSecret>{self.api_secret}</appSecret>
                 </auth>
                 <productQuestionId>{question_id}</productQuestionId>
                 <content>{answer}</content>
              </n11:SaveProductAnswerRequest>
           </soapenv:Body>
        </soapenv:Envelope>"""
        
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": "SaveProductAnswer"
        }
        
        try:
            response = self.session.post(url, data=xml.encode('utf-8'), headers=headers, timeout=30)
            response.raise_for_status()
            
            # Success check
            if "status>SUCCESS</" in response.text or "status>OK</" in response.text:
                return {"success": True}
            return {"success": False, "raw": response.text}
            
        except Exception as e:
            logging.error(f"N11 answer_question error: {e}")
            return {"success": False, "error": str(e)}

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


    def approve_claim(self, claim_id: str) -> Dict[str, Any]:
        """
        Approve a claim on N11 (SOAP).
        Service: ClaimService
        Method: ApproveClaim
        """
        url = "https://api.n11.com/ws/ClaimService.wsdl"
        xml = f"""<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:n11="http://www.n11.com/ws/schemas">
           <soapenv:Header/>
           <soapenv:Body>
              <n11:ApproveClaimRequest>
                 <auth>
                    <appKey>{self.api_key}</appKey>
                    <appSecret>{self.api_secret}</appSecret>
                 </auth>
                 <claimId>{claim_id}</claimId>
              </n11:ApproveClaimRequest>
           </soapenv:Body>
        </soapenv:Envelope>"""
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "ApproveClaim"}
        try:
            response = self.session.post(url, data=xml, headers=headers, timeout=30)
            response.raise_for_status()
            if "status>SUCCESS</" in response.text or "status>OK</" in response.text:
                return {"success": True}
            return {"success": False, "raw": response.text}
        except Exception as e:
            logging.error(f"N11 approve_claim error: {e}")
            return {"success": False, "error": str(e)}

    def reject_claim(self, claim_id: str, reason_id: str, reason_text: str = None) -> Dict[str, Any]:
        """
        Reject a claim on N11 (SOAP).
        Service: ClaimService
        Method: RejectClaim
        """
        url = "https://api.n11.com/ws/ClaimService.wsdl"
        xml = f"""<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:n11="http://www.n11.com/ws/schemas">
           <soapenv:Header/>
           <soapenv:Body>
              <n11:RejectClaimRequest>
                 <auth>
                    <appKey>{self.api_key}</appKey>
                    <appSecret>{self.api_secret}</appSecret>
                 </auth>
                 <claimId>{claim_id}</claimId>
                 <rejectReasonId>{reason_id}</rejectReasonId>
                 <rejectDescription>{reason_text or ''}</rejectDescription>
              </n11:RejectClaimRequest>
           </soapenv:Body>
        </soapenv:Envelope>"""
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "RejectClaim"}
        try:
            response = self.session.post(url, data=xml, headers=headers, timeout=30)
            response.raise_for_status()
            if "status>SUCCESS</" in response.text or "status>OK</" in response.text:
                return {"success": True}
            return {"success": False, "raw": response.text}
        except Exception as e:
            logging.error(f"N11 reject_claim error: {e}")
            return {"success": False, "error": str(e)}

    def get_questions(self, page: int = 0, size: int = 100) -> Dict[str, Any]:
        """
        Fetch product questions from N11 (SOAP).
        """
        url = "https://api.n11.com/ws/ProductQuestionService.wsdl"
        xml = f"""<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:n11="http://www.n11.com/ws/schemas">
           <soapenv:Header/>
           <soapenv:Body>
              <n11:GetProductQuestionListRequest>
                 <auth>
                    <appKey>{self.api_key}</appKey>
                    <appSecret>{self.api_secret}</appSecret>
                 </auth>
                 <currentPage>{page}</currentPage>
                 <pageSize>{size}</pageSize>
              </n11:GetProductQuestionListRequest>
           </soapenv:Body>
        </soapenv:Envelope>"""
        
        headers = {"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "GetProductQuestionList"}
        
        try:
            response = self.session.post(url, data=xml, headers=headers, timeout=30)
            response.raise_for_status()
            
            import xml.etree.ElementTree as ET
            # Simplified parsing
            content = response.content
            # Remove namespaces for easier find
            content_clean = content.replace(b' xmlns="http://www.n11.com/ws/schemas"', b'')
            root = ET.fromstring(content_clean)
            
            questions = []
            q_list = root.findall(".//productQuestionList/productQuestion")
            for q in q_list:
                questions.append({
                    "id": q.findtext("id"),
                    "text": q.findtext("question"),
                    "user": q.findtext("userName"),
                    "createdDate": q.findtext("createDate"),
                    "answered": q.findtext("answer") is not None,
                    "product": {"title": q.findtext("productName")}
                })
            return {"questions": questions, "total": len(questions)}
        except Exception as e:
            logging.error(f"N11 get_questions error: {e}")
            return {"questions": [], "error": str(e)}

    def get_claims(self, page: int = 0, size: int = 50) -> Dict[str, Any]:
        """
        Fetch claims from N11 (SOAP).
        Service: ClaimService
        Method: GetClaimList
        """
        url = "https://api.n11.com/ws/ClaimService.wsdl"
        xml = f"""<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:n11="http://www.n11.com/ws/schemas">
           <soapenv:Header/>
           <soapenv:Body>
              <n11:GetClaimListRequest>
                 <auth>
                    <appKey>{self.api_key}</appKey>
                    <appSecret>{self.api_secret}</appSecret>
                 </auth>
                 <pagingData>
                    <currentPage>{page}</currentPage>
                    <pageSize>{size}</pageSize>
                 </pagingData>
              </n11:GetClaimListRequest>
           </soapenv:Body>
        </soapenv:Envelope>"""
        
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": "GetClaimList"
        }
        
        try:
            response = self.session.post(url, data=xml, headers=headers, timeout=30)
            response.raise_for_status()
            
            import xml.etree.ElementTree as ET
            root = ET.fromstring(response.content)
            
            # Strip namespaces
            for elem in root.iter():
                if '}' in elem.tag:
                    elem.tag = elem.tag.split('}', 1)[1]
            
            claims = []
            claim_list = root.find(".//claimList")
            if claim_list is not None:
                for c in claim_list.findall("claim"):
                    claims.append({
                        "id": c.findtext("id"),
                        "orderId": c.findtext("order/id"),
                        "orderNumber": c.findtext("order/orderNumber"),
                        "productName": c.findtext("orderItem/productName"),
                        "quantity": c.findtext("orderItem/quantity"),
                        "status": c.findtext("claimStatus"),
                        "reason": c.findtext("claimReason"),
                        "customerName": c.findtext("order/buyer/fullName"),
                        "amount": c.findtext("orderItem/price"),
                        "date": c.findtext("createDate")
                    })
            return {"claims": claims, "total": len(claims)}
        except Exception as e:
            logging.error(f"N11 get_claims error: {e}")
            return {"claims": []}

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

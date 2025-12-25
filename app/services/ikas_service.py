import logging
import requests
from typing import Dict, Any, List
from app.models import Setting

class IkasService:
    def __init__(self, user_id=None):
        self.user_id = user_id
        self.api_token = Setting.get("IKAS_API_TOKEN", user_id=user_id)
        self.shop_url = Setting.get("IKAS_SHOP_URL", user_id=user_id) # e.g. https://myshop.ikas.com
        
        # Ikas API usually requires shop domain and Bearer token
        self.api_url = f"{self.shop_url.rstrip('/')}/api/v1" if self.shop_url else "https://api.ikas.com/v1"
        
        self.headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def get_products(self, page: int = 1, limit: int = 50) -> Dict[str, Any]:
        if not self.api_token or not self.shop_url:
            return {"items": [], "total": 0}
            
        try:
            # Note: This is a placeholder for actual Ikas API endpoints
            # Real Ikas integration might use GraphQL. 
            # If the user previously had it, they might expect certain structure.
            params = {"page": page, "limit": limit}
            resp = requests.get(f"{self.api_url}/products", headers=self.headers, params=params, timeout=30)
            
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "items": data.get("data", []),
                    "total": data.get("meta", {}).get("total", 0)
                }
            return {"items": [], "total": 0}
        except Exception as e:
            logging.error(f"Ikas get_products error: {e}")
            return {"items": [], "total": 0}

    def get_product_count(self) -> int:
        if not self.api_token or not self.shop_url:
            return 0
        try:
            resp = requests.get(f"{self.api_url}/products/count", headers=self.headers, timeout=30)
            if resp.status_code == 200:
                return resp.json().get("count", 0)
            return 0
        except Exception:
            return 0

def get_ikas_service(user_id=None):
    return IkasService(user_id=user_id)

def get_ikas_client(user_id=None):
    """Alias for compatibility if needed"""
    return IkasService(user_id=user_id)

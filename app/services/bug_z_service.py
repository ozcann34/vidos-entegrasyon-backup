import requests
import json
from datetime import datetime
from flask import current_app
from app.models.settings import Setting
from app.models.order import Order

class BugZService:
    def __init__(self, user):
        self.user = user
        self.api_key = Setting.get('BUGZ_API_KEY', user_id=user.id)
        self.api_secret = Setting.get('BUGZ_API_SECRET', user_id=user.id)
        self.base_url = Setting.get('BUGZ_API_URL', 'https://bug-z.com/api/v2', user_id=user.id)

    def is_configured(self):
        return bool(self.api_key and self.api_secret)

    def check_connection(self):
        """
        Verify credentials by making a simple metadata or list request.
        """
        if not self.is_configured():
            return {"success": False, "message": "API Key veya API Secret eksik."}
            
        import requests
        headers = {
            "Content-Type": "application/json",
            "apikey": self.api_key,
            "apisecret": self.api_secret,
            "User-Agent": "Vidos-Integrator/1.0"
        }
        
        try:
            # Try to fetch orders list or metadata. 
            # Note: If /orders doesn't exist, we might need another endpoint from their docs.
            url = f"{self.base_url.rstrip('/')}/web_servis/order/filter" # Assuming filter exists as a generic check
            if not url.endswith('filter'): # simple fallback
                 url = f"{self.base_url.rstrip('/')}/web_servis/orders"
            # Using a small limit if possible to be light
            params = {"limit": 1}
            response = requests.get(url, headers=headers, params=params, timeout=15)
            
            if response.status_code == 200:
                return {"success": True, "message": "BUG-Z bağlantısı başarılı."}
            elif response.status_code == 401:
                return {"success": False, "message": "API Anahtarı veya Sırrı geçersiz."}
            else:
                return {"success": False, "message": f"Bağlantı hatası: HTTP {response.status_code}"}
        except Exception as e:
            return {"success": False, "message": f"İstisnai Hata: {str(e)}"}

    def create_order(self, vidos_order: Order):
        """
        Maps a Vidos marketplace order to BUG-Z (Quakasoft) API format.
        """
        if not self.is_configured():
            return {"success": False, "message": "BUG-Z API ayarları (Key/Secret) eksik."}

        headers = {
            "Content-Type": "application/json",
            "apikey": self.api_key,
            "apisecret": self.api_secret,
            "User-Agent": "Vidos-Integrator/1.0"
        }

        # Handle Customer Mapping
        customer_data = {
            "name": vidos_order.customer.first_name if vidos_order.customer else "İsimsiz",
            "lastname": vidos_order.customer.last_name if vidos_order.customer else "Müşteri",
            "email": vidos_order.customer.email if vidos_order.customer and vidos_order.customer.email else "",
            "phone": vidos_order.customer.phone if vidos_order.customer and vidos_order.customer.phone else "",
            "city": vidos_order.customer.city if vidos_order.customer else "",
            "district": vidos_order.customer.district if vidos_order.customer else "",
            "address": vidos_order.customer.address if vidos_order.customer else "",
            "country": "Türkiye"
        }

        # Handle Order Mapping
        order_data = {
            "paymentType": 38, # Cariden Ödeme
            "status": 1,      # Yeni Sipariş
            "note": f"Vidos - {vidos_order.marketplace.upper()} Siparişi: {vidos_order.order_number}",
            "createdAt": vidos_order.created_at.strftime("%Y-%m-%d %H:%M:%S") if vidos_order.created_at else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        # Handle Product Mapping (Assuming order has line items)
        products = []
        # Note: vidos_order.items should be a list of items. I'll check order model if needed.
        # For now assuming items attribute exists based on common patterns.
        for item in vidos_order.items:
            products.append({
                "code": item.sku or item.barcode, # SKU usually matches BUG-Z product code
                "name": item.product_name,
                "price": float(item.unit_price),
                "tax": float(item.vat_rate) if hasattr(item, 'vat_rate') else 20.0,
                "quantity": int(item.quantity)
            })

        payload = {
            "customer": customer_data,
            "order": order_data,
            "products": products
        }

        try:
            url = f"{self.base_url.rstrip('/')}/web_servis/order/create"
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            res_json = response.json()

            if response.status_code == 200 and res_json.get("code") == "SUCCESS":
                # Save the BUG-Z order number back to our DB if possible
                bugz_code = res_json.get("result", {}).get("code")
                # Store it in a hidden setting or note for now
                vidos_order.admin_note = f"BUG-Z Sipariş No: {bugz_code}\n{vidos_order.admin_note or ''}"
                from app import db
                db.session.commit()
                
                return {"success": True, "bugz_order_code": bugz_code}
            else:
                return {
                    "success": False, 
                    "message": f"API Hatası: {res_json.get('description', 'Bilinmeyen hata')}",
                    "details": res_json
                }
        except Exception as e:
            return {"success": False, "message": f"Bağlantı Hatası: {str(e)}"}

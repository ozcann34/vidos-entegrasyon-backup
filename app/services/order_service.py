import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)
from typing import List, Dict, Any, Optional

from app import db
from app.models import Order, OrderItem, Customer, Product
from flask_login import current_user
from app.services.trendyol_service import get_trendyol_client
from app.services.pazarama_service import get_pazarama_client

from app.services.hepsiburada_service import get_hepsiburada_client
from app.services.idefix_service import get_idefix_client
from app.services.bug_z_service import BugZService

# Hepsiburada status mapping
# Valid statuses: Listed, Unavailable, Created, UnPacked, Packed, Shipped, Delivered, UnDelivered, Cancelled, Returned
HB_STATUS_TR = {
    'Created': 'Oluşturuldu',
    'UnPacked': 'Bölünmüş',
    'Packed': 'Paketlendi',
    'Shipped': 'Kargoya Verildi',
    'Delivered': 'Teslim Edildi',
    'UnDelivered': 'Teslim Edilemedi',
    'Cancelled': 'İptal Edildi',
    'Returned': 'İade Edildi'
}

# Idefix status mapping (Generic guess, verify with API docs if possible, usually English)
IDEFIX_STATUS_TR = {
    'Created': 'Oluşturuldu',
    'Waiting': 'Bekliyor',
    'Preparation': 'Hazırlanıyor',
    'Shipped': 'Kargoya Verildi',
    'Delivered': 'Teslim Edildi',
    'Cancelled': 'İptal Edildi',
    'Returned': 'İade Edildi'
}

def sync_hepsiburada_orders(days_back: int = 30, user_id: int = None) -> Dict[str, Any]:
    """
    Fetch orders from Hepsiburada
    """
    client = get_hepsiburada_client(user_id=user_id)
    total_synced = 0
    errors = []
    
    # Omics API filtering by date is tricky, usually relies on 'limit'.
    # We will fetch last 100 orders and filter by date locally or just upsert all.
    # To be safe, fetch more if days_back is large.
    
    logging.info(f"Syncing Hepsiburada orders...")
    
    try:
        # Fetch generic last 100-200 orders
        if isinstance(resp, list):
            orders = resp
        elif isinstance(resp, dict):
            orders = resp.get("items") or resp.get("data") or []
        else:
            orders = []
            
        logging.info(f"Hepsiburada fetched {len(orders)} orders")
        
        for item in orders:
            try:
                _process_hepsiburada_order(item, user_id)
                total_synced += 1
            except Exception as e:
                o_num = item.get("orderNumber")
                errors.append(f"HB Order {o_num} error: {e}")
                
    except Exception as e:
        errors.append(f"HB Sync Error: {e}")
        
    return {"synced": total_synced, "errors": errors}

def _process_hepsiburada_order(data: Dict[str, Any], user_id: int = None):
    # Data is from Omics API
    order_number = str(data.get("orderNumber", ""))
    mp_order_id = str(data.get("id", ""))
    
    if not mp_order_id:
        return
        
    existing = Order.query.filter_by(marketplace='hepsiburada', marketplace_order_id=mp_order_id).first()
    if not existing:
        existing = Order(marketplace='hepsiburada', marketplace_order_id=mp_order_id, user_id=user_id)
        
    existing.order_number = order_number
    
    status_en = data.get("status", "Created")
    existing.status = HB_STATUS_TR.get(status_en, status_en)
    
    existing.total_price = float(data.get("totalPrice", {}).get("amount", 0.0))
    existing.currency = data.get("totalPrice", {}).get("currency", "TRY")
    existing.raw_data = json.dumps(data, ensure_ascii=False)
    
    # Date (format might be ISO)
    date_str = data.get("createdAt")
    if date_str:
        try:
            # Handle various ISO formats from HB
            dt_clean = date_str.replace("Z", "+00:00")
            if "." in dt_clean: # Handle microseconds if present
                parts = dt_clean.split(".")
                dt_clean = parts[0] + "+" + parts[1].split("+")[1]
            existing.created_at = datetime.fromisoformat(dt_clean)
        except Exception as e:
            logging.warning(f"HB Date parse error ({date_str}): {e}")
            existing.created_at = datetime.utcnow()
    
    # Save before triggering BUG-Z to ensure items/customer relationship
    db.session.add(existing)
    db.session.commit()
    
    existing.updated_at = datetime.utcnow()
    
    # Customer
    cust = data.get("customer", {})
    if cust:
        c_email = cust.get("email")
        if c_email:
            customer = Customer.query.filter_by(email=c_email).first()
            if not customer:
                customer = Customer(email=c_email)
                db.session.add(customer)
            customer.first_name = cust.get("name", "").split(" ")[0]
            customer.last_name = " ".join(cust.get("name", "").split(" ")[1:])
            existing.customer = customer
            
    db.session.add(existing)
    db.session.commit()
    
    # Items
    items = data.get("items", []) or data.get("lines", [])
    OrderItem.query.filter_by(order_id=existing.id).delete()
    
    for item in items:
        oi = OrderItem(order_id=existing.id)
        oi.product_name = item.get("productName") or item.get("name")
        oi.quantity = int(item.get("quantity", 1))
        # Price is nested usually
        p_info = item.get("price", {})
        if isinstance(p_info, dict):
            oi.unit_price = float(p_info.get("amount", 0.0))
        else:
             oi.unit_price = float(item.get("price", 0.0))
             
        oi.barcode = item.get("merchantSku") or item.get("sku")
        
        if oi.barcode:
            prod = Product.query.filter_by(barcode=oi.barcode).first()
            if prod:
                oi.product_id = prod.id
        
        # VAT Rate
        oi.vat_rate = float(item.get("vatRate", 20.0))
                
        db.session.add(oi)
    db.session.commit()

    # Trigger BUG-Z forward
    _trigger_bugz_push(existing, user_id)

def sync_idefix_orders(days_back: int = 30, user_id: int = None) -> Dict[str, Any]:
    """
    Fetch orders from Idefix
    """
    client = get_idefix_client(user_id=user_id)
    total_synced = 0
    errors = []
    
    logging.info(f"Syncing Idefix orders...")
    
    try:
        # Default fetch last 50
        resp = client.get_orders(size=50)
        # Check specific return key (content or items)
        orders = []
        if isinstance(resp, list):
            orders = resp
        elif isinstance(resp, dict):
            orders = resp.get("content") or resp.get("items") or []
            
        logging.info(f"Idefix fetched {len(orders)} orders")
        
        for item in orders:
            try:
                _process_idefix_order(item, user_id)
                total_synced += 1
            except Exception as e:
                errors.append(f"Idefix item error: {e}")
                
    except Exception as e:
        errors.append(f"Idefix Sync Error: {e}")
        
    return {"synced": total_synced, "errors": errors}

def _process_idefix_order(data: Dict[str, Any], user_id: int = None):
    order_number = str(data.get("orderNumber", ""))
    mp_order_id = str(data.get("id", ""))
    
    if not mp_order_id:
        return
        
    existing = Order.query.filter_by(marketplace='idefix', marketplace_order_id=mp_order_id).first()
    if not existing:
        existing = Order(marketplace='idefix', marketplace_order_id=mp_order_id, user_id=user_id)
        
    existing.order_number = order_number
    status_en = data.get("status", "Created")
    existing.status = IDEFIX_STATUS_TR.get(status_en, status_en)
    
    # Idefix OMS returns total_price, status, orderNumber etc. in shipment level
    existing.total_price = float(data.get("discountedTotalPrice", data.get("totalPrice", 0.0)))
    existing.currency = "TRY" 
    existing.raw_data = json.dumps(data, ensure_ascii=False)
    
    # Dates: orderDate (creation), updatedAt (last modified)
    date_str = data.get("orderDate") or data.get("createdAt")
    if date_str:
        try:
             # Idefix format: 2023-04-06T14:27:52+03:00
             if isinstance(date_str, int):
                 existing.created_at = datetime.fromtimestamp(date_str/1000)
             else:
                 # Strip timezone for naive UTC or use proper parsing
                 existing.created_at = datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=None)
        except:
            existing.created_at = datetime.utcnow()
    else:
        existing.created_at = datetime.utcnow()
        
    existing.updated_at = datetime.utcnow()
    
    db.session.add(existing)
    db.session.commit()
    
    # Items
    items = data.get("items", [])
    OrderItem.query.filter_by(order_id=existing.id).delete()
    
    for item in items:
        oi = OrderItem(order_id=existing.id)
        oi.product_name = item.get("name")
        oi.quantity = int(item.get("quantity", 1))
        oi.unit_price = float(item.get("price", 0.0))
        oi.barcode = item.get("barcode") or item.get("sku")
        
        if oi.barcode:
            prod = Product.query.filter_by(barcode=oi.barcode).first()
            if prod:
                oi.product_id = prod.id
        db.session.add(oi)
    db.session.commit()

    # Trigger BUG-Z forward
    _trigger_bugz_push(existing, user_id)




def sync_n11_orders(days_back: int = 30, user_id: int = None) -> Dict[str, Any]:
    """Sync orders from N11"""
    from app.services.n11_client import get_n11_client
    
    total_synced = 0
    errors = []
    
    try:
        logging.info("Syncing N11 orders...")
        client = get_n11_client(user_id=user_id)
        if not client:
             return {'success': False, 'message': 'N11 API client oluşturulamadı (Ayarlar eksik).'}
        
        # Date range
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days_back)
        
        # N11 expects timestamps in milliseconds
        start_ts = int(start_date.timestamp() * 1000)
        end_ts = int(end_date.timestamp() * 1000)
        
        # Fetch pages
        page = 0
        while True:
            resp = client.get_orders(start_date=start_ts, end_date=end_ts, page=page, size=100)
            if not resp or 'content' not in resp:
                break
            
            orders = resp['content']
            if not orders:
                break
                
            for item in orders:
                try:
                    _process_n11_order(item, user_id)
                    total_synced += 1
                except Exception as e:
                    errors.append(f"N11 Order {item.get('orderNumber')} error: {e}")
            
            # Check pagination
            total_pages = int(resp.get('totalPages', 0))
            # 'number' is current page index (0-based)
            current_page = int(resp.get('number', 0))
            
            if current_page >= total_pages - 1:
                break
                
            page += 1
            if page > 50: # Safety break
                break
                
    except Exception as e:
        logging.error(f"N11 sync error: {e}")
        errors.append(str(e))
        
    return {"synced": total_synced, "errors": errors}

def _process_n11_order(data: Dict[str, Any], user_id: int = None):
    # Data is "shipmentPackage" object
    order_number = str(data.get("orderNumber", ""))
    package_id = str(data.get("id", "")) # N11 uses package ID for tracking mostly
    
    if not order_number:
        return

    # Unique check: N11 can have multiple packages for same order number? 
    # Yes, but typically we treat "shipmentPackage" as the order unit for processing.
    # Let's use order_number for display, but maybe composite ID for uniqueness?
    # Or just stick to order_number if 1:1 usually. 
    # Actually N11 documentation says "Shipment List" -> usually grouped by package.
    
    existing = Order.query.filter_by(marketplace='n11', marketplace_order_id=package_id).first()
    if not existing:
        existing = Order.query.filter_by(marketplace='n11', order_number=order_number).first()

    status_map = {
        'Created': 'Oluşturuldu',
        'Picking': 'Toplanıyor',
        'Shipped': 'Kargolandı',
        'Delivered': 'Teslim Edildi', 
        'Cancelled': 'İptal',
        'UnSupplied': 'Tedarik Edilemedi',
        'UnPacked': 'Paket bozuldu'
    }
    
    status = data.get("shipmentPackageStatus", "Created")
    status_tr = status_map.get(status, status)
    
    if existing:
        if existing.status != status_tr:
            existing.status = status_tr
            existing.updated_at = datetime.utcnow()
            db.session.commit()
    else:
        # Create new
        # Dates are usually timestamps (long)
        # Assuming lastModifiedDate or use current if missing
        created_ts = data.get('agreedDeliveryDate') # or create date? N11 doesn't send create date explicitly in summary? 
        # Actually it doesn't clearly show 'orderDate' in the packet list response in docs, 
        # but docs say "orderDate" (string) in some examples or "createdDate".
        # Let's assume now if not found.
        # Wait, doc says: "agreedDeliveryDate": ... 
        # Retrying to find order date.
        # Docs say: "shipmentPackageStatus": ...
        # Let's use utcnow if not clear.
        
        created_at = datetime.utcnow()
        
        # Calculate total amount from lines
        total_price = 0.0
        currency = "TRY"
        
        lines = data.get("lines", [])
        if lines:
             # Sum up dueAmount or price*quantity
             # dueAmount = price * quantity - discounts usually
             for l in lines:
                 total_price += float(l.get("dueAmount", 0.0))
        
        cust_name = data.get("customerfullName", "") or data.get("receiverName", "") # Check docs
        
        existing = Order(
            user_id=user_id,
            marketplace='n11',
            order_number=order_number,
            marketplace_order_id=package_id,
            customer_name=cust_name,
            total_price=total_price,
            currency=currency,
            status=status_tr,
            created_at=created_at
        )
        
        # Customer
        c_email = data.get("customerEmail")
        if c_email:
            customer = Customer.query.filter_by(email=c_email).first()
            if not customer:
                 customer = Customer(email=c_email, first_name=cust_name) # simplified name parse
                 db.session.add(customer)
            existing.customer = customer
            
        db.session.add(existing)
        db.session.commit()
        
    # Sync Items
    # (Simplified: wipe and recreate items to handle updates)
    if existing.id:
        OrderItem.query.filter_by(order_id=existing.id).delete()
        for line in data.get("lines", []):
            item = OrderItem(order_id=existing.id)
            item.product_name = line.get("productName", "")
            item.quantity = int(line.get("quantity", 1))
            item.unit_price = float(line.get("price", 0.0))
            item.barcode = line.get("barcode", "") or line.get("stockCode", "")
            
            if item.barcode:
                 prod = Product.query.filter_by(barcode=item.barcode).first()
                 if prod:
                     item.product_id = prod.id
            db.session.add(item)
        db.session.commit()

    # Trigger BUG-Z forward
    _trigger_bugz_push(existing, user_id)


def sync_trendyol_orders(days_back: int = 30, user_id: int = None) -> Dict[str, Any]:
    """
    Fetch and sync orders from Trendyol.
    Handles 14-day date range limit by chunking.
    """
    results = {'success': False, 'count': 0, 'errors': []}
    
    try:
        logging.info("Syncing Trendyol orders...")
        client = get_trendyol_client(user_id=user_id)
        if not client:
             return {'success': False, 'message': 'Trendyol API client oluşturulamadı.'}
        
        if not hasattr(client, 'get_shipment_packages'):
             return {'success': False, 'message': 'Client method missing (get_shipment_packages)'}
             
        # Calculate date chunks (13 days to be safe)
        chunks = []
        end_date = datetime.now()
        total_days = days_back
        
        current_end = end_date
        while total_days > 0:
            chunk_days = min(total_days, 14) # Limit 14 days
            current_start = current_end - timedelta(days=chunk_days)
            chunks.append((current_start, current_end))
            current_end = current_start
            total_days -= chunk_days
            
        saved_count = 0
        
        for start_dt, end_dt in chunks:
             # Trendyol expects timestamps in milliseconds
            start_ts = int(start_dt.timestamp() * 1000)
            end_ts = int(end_dt.timestamp() * 1000)
            
            # Fetch pages
            page = 0
            size = 50
            while True:
                try:
                     orders_data = client.get_shipment_packages(
                         start_date=start_ts, 
                         end_date=end_ts,
                         status=None, # All statuses
                         page=page,
                         size=size
                     )
                     
                     if not orders_data or 'content' not in orders_data:
                         break
                         
                     content = orders_data['content']
                     if not content:
                         break
                         
                     for order_item in content:
                        if _process_trendyol_order(order_item, user_id):
                            saved_count += 1
                            
                     # Pagination
                     # Trendyol returns "totalPages" sometimes? Or just check content size
                     # Usually "totalPages" is in response
                     total_pages = orders_data.get('totalPages', 0)
                     if page >= total_pages - 1:
                         break
                         
                     page += 1
                     
                except Exception as e:
                    logging.error(f"Trendyol sync chunk error: {e}")
                    results['errors'].append(str(e))
                    break
        
        return {'success': True, 'count': saved_count}
        
    except Exception as e:
        logging.error(f"Trendyol sync error: {e}")
        return {'success': False, 'message': str(e)}

def _process_trendyol_order(data: Dict[str, Any], user_id: int = None) -> bool:
    """Process single Trendyol order data (Shipment Package)"""
    try:
        order_number = data.get('orderNumber')
        if not order_number:
            return False
            
        existing = Order.query.filter_by(order_number=str(order_number), marketplace='trendyol').first()
        
        # Mapping status
        status = data.get('status', 'Created')
        
        # Create new order if not exists
        if not existing:
             # Trendyol date is timestamp (ms)
            order_date_ts = data.get('orderDate', 0)
            created_at = datetime.fromtimestamp(order_date_ts / 1000) if order_date_ts else datetime.utcnow()
            
            customer_name = f"{data.get('customerFirstName', '')} {data.get('customerLastName', '')}".strip()
            
            # Use shipment package ID as marketplace_order_id
            package_id = str(data.get('id', ''))
            
            existing = Order(
                user_id=user_id,
                order_number=str(order_number),
                marketplace='trendyol',
                marketplace_order_id=package_id,  # Added
                customer_name=customer_name,
                total_price=float(data.get('totalPrice', 0)),
                status=status,
                created_at=created_at,
                shipment_package_id=package_id,
                currency=data.get('currencyCode', 'TRY'), # Assuming default TRY
                cargo_code=str(data.get('cargoTrackingNumber') or data.get('cargoSenderNumber') or '')
            )
            
            # Customer handling
            customer_email = data.get('customerEmail', '')
            if customer_email:
                customer = Customer.query.filter_by(email=customer_email).first()
                if not customer:
                    customer = Customer(
                        email=customer_email, 
                        first_name=data.get('customerFirstName', ''),
                        last_name=data.get('customerLastName', '')
                    )
                    db.session.add(customer)
                existing.customer = customer

            db.session.add(existing)
        else:
            # Update existing
            if existing.status != status:
                existing.status = status
                existing.updated_at = datetime.utcnow()
        
        # Always update items if details changed? Usually items don't change but status does.
        # But let's ensure items are there.
        if not existing.items:
             for line in data.get('lines', []):
                item = OrderItem(
                    product_name=line.get('productName', 'Unknown'),
                    sku=line.get('merchantSku', ''),
                    quantity=line.get('quantity', 1),
                    price=float(line.get('price', 0)),
                    currency=line.get('currencyCode', 'TRY'),
                    barcode=line.get('barcode', '')
                )
                if item.barcode:
                     prod = Product.query.filter_by(barcode=item.barcode).first()
                     if prod:
                         item.product_id = prod.id
                
                # VAT Rate (Trendyol usually provides 'vatRate' in line items)
                item.vat_rate = float(line.get('vatRate', 20.0))
                
                existing.items.append(item)
            
        db.session.commit()
        
        # Trigger BUG-Z forward
        _trigger_bugz_push(existing, user_id)
        
        return True
        
    except Exception as e:
        logging.error(f"Error processing trendyol order {data.get('orderNumber', 'unknown')}: {e}")
        db.session.rollback()
        return False
        
def sync_pazarama_orders(days_back: int = 30, user_id: int = None) -> Dict[str, Any]:
    """Sync orders from Pazarama"""
    from app.services.pazarama_client import PazaramaClient
    from app.models import Setting
    
    try:
        logging.info("Syncing Pazarama orders...")
        # Get user specific or first admin settings
        # Assuming current_user context or generic
        try:
             user_id = current_user.id
        except:
             user_id = None
             
        from app.services.pazarama_service import get_pazarama_client
        
        client = get_pazarama_client(user_id=user_id)
        
        if not client:
             return {'success': False, 'message': 'Pazarama API bilgileri eksik veya istemci oluşturulamadı.'}
        
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days_back)
        
        # Pazarama API: POST /order/getOrdersForApi
        # Format: { "startDate": "YYYY-MM-DD", "endDate": "YYYY-MM-DD", "pageSize": 100, "pageNumber": 1 }
        
        page = 1
        total_synced = 0
        
        while True:
            # Note: PazaramaClient.get_orders likely implemented to accept these args
            # Check if client method signature matches. 
            # If client was auto-generated or simplistic, we might need to adjust arguments.
            # Assuming standard keyword args are passed to json body.
            
            payload = {
                "startDate": start_date.strftime('%Y-%m-%d'),
                "endDate": end_date.strftime('%Y-%m-%d'),
                "pageSize": 50,
                "pageNumber": page
            }
            
            # Using generic call if get_orders supports payload or args
            # If get_orders signature is fixed, we might need to adapt.
            # Assuming client.get_orders(**payload) or similar.
            # Reverting to direct implementation assumption:
            orders_resp = client.get_orders(
                start_date=start_date.strftime('%Y-%m-%d'), 
                end_date=end_date.strftime('%Y-%m-%d'),
                size=50,
                page=page
            )
            
            if not orders_resp:
                break

            items = []
            if isinstance(orders_resp, dict):
                 items = orders_resp.get('items') or orders_resp.get('data', [])
                 
            if not items:
                break
                
            for item in items:
                if _process_pazarama_order(item, user_id):
                    total_synced += 1
            
            # Check pagination
            # Pazarama response usually has "totalCount" or similar to calc pages?
            # Or just break if items < pageSize
            if len(items) < 50:
                break
                
            page += 1
            if page > 50: break # Safety
                    
        return {'success': True, 'count': total_synced}
        
    except Exception as e:
        logging.error(f"Pazarama sync error: {e}")
        return {'success': False, 'message': str(e)}

def _process_pazarama_order(data: Dict[str, Any], user_id: int = None) -> bool:
    """Process single Pazarama order"""
    try:
        order_number = data.get('orderNumber') or data.get('code')
        if not order_number:
            return False
            
        existing = Order.query.filter_by(order_number=str(order_number), marketplace='pazarama').first()
        
        status = str(data.get('orderStatus', '')) # Pazarama returns int status usually?
        # Map Pazarama status codes if needed. 
        # 1: Created, 2: Approved, 3: Shipped... (Hypothetical, check docs)
        # Doc says: 3 -> ?
        
        if existing:
            # Update status if changed
            if existing.status != status:
                existing.status = status
                existing.updated_at = datetime.utcnow()
            order = existing
        else:
            order = Order(
                user_id=user_id,
                order_number=str(order_number),
                marketplace='pazarama',
                customer_name=f"{data.get('customerName', '')}",
                total_price=float(data.get('orderAmount', 0) if data.get('orderAmount') else 0),
                status=status,
                created_at=datetime.utcnow(),
                raw_data=json.dumps(data),
                cargo_code=str(data.get('cargoTrackingNumber') or data.get('shipmentTrackingNumber') or '')
            )
            db.session.add(order)
        
        db.session.commit()

        # Process Items
        items_data = data.get('items') or data.get('orderItems') or data.get('lines') or []
        
        # Simple policy: recreate items if not present
        if not order.items:
            for p_item in items_data:
                try:
                    oi = OrderItem(order_id=order.id)
                    oi.product_name = p_item.get('productName') or p_item.get('name') or "Pazarama Ürünü"
                    oi.quantity = int(p_item.get('quantity', 1))
                    price_val = p_item.get('listPrice') or p_item.get('price') or p_item.get('unitPrice') or 0
                    oi.unit_price = float(price_val)
                    oi.barcode = p_item.get('barcode') or p_item.get('stockCode') or p_item.get('code')
                    
                    if oi.barcode:
                        local_prod = Product.query.filter_by(barcode=oi.barcode).first()
                        if local_prod:
                            oi.product_id = local_prod.id
                    db.session.add(oi)
                except Exception as ie:
                    logging.error(f"Pazarama item parse error: {ie}")
            db.session.commit()

        # Trigger BUG-Z forward
        _trigger_bugz_push(order, user_id)

        return True
    except Exception as e:
        logging.error(f"Pazarama process error: {e}")
        db.session.rollback()
        return False

def sync_all_users_orders():
    """
    Finds all users who have order sync enabled
    and runs the sync task for each of them.
    """
    from app.models import User, Setting
    from app.services.subscription_service import check_usage_limit
    
    logger.info("Checking all users for Global Order sync...")
    
    # We could filter by Setting, but let's just get all users and check their setting
    # In a larger app, we'd query Setting table for users where ORDER_SYNC_ENABLED is true.
    users = User.query.all()
    
    success_count = 0
    total_active = 0
    
    for user in users:
        # Check if enabled for this user
        enabled = Setting.get('ORDER_SYNC_ENABLED', user_id=user.id) == 'true'
        if enabled:
            total_active += 1
            try:
                sync_all_orders(user_id=user.id)
                success_count += 1
            except Exception as e:
                logger.error(f"Order sync failed for user {user.id}: {e}")
                
    logger.info(f"Global Order sync finished. Total users: {total_active}, Success: {success_count}")
    return {'total': total_active, 'success': success_count}


def sync_all_orders(user_id: int = None):
    """Sync orders from ALL marketplaces"""
    results = {}
    
    # Trendyol
    try:
        results['trendyol'] = sync_trendyol_orders(user_id=user_id)
    except Exception as e:
        results['trendyol'] = {"error": str(e)}
        
    # Pazarama
    try:
        results['pazarama'] = sync_pazarama_orders(user_id=user_id)
    except Exception as e:
        results['pazarama'] = {"error": str(e)}
        
    # Idefix
    try:
        results['idefix'] = sync_idefix_orders(user_id=user_id)
    except Exception as e:
        results['idefix'] = {"error": str(e)}

    # N11
    try:
        results['n11'] = sync_n11_orders(user_id=user_id)
    except Exception as e:
        results['n11'] = {"error": str(e)}

    # Hepsiburada
    try:
        results['hepsiburada'] = sync_hepsiburada_orders(user_id=user_id)
    except Exception as e:
        results['hepsiburada'] = {"error": str(e)}
        
    return results

def sync_all_products(user_id: int = None):
    """Sync products from ALL marketplaces to local database"""
    results = {}
    
    # Trendyol
    try:
        from app.services.trendyol_service import refresh_trendyol_cache
        results['trendyol'] = refresh_trendyol_cache(user_id=user_id)
    except Exception as e:
        results['trendyol'] = {"error": str(e)}
        
    # Pazarama
    try:
        from app.services.pazarama_service import sync_pazarama_products
        results['pazarama'] = sync_pazarama_products(user_id=user_id)
    except Exception as e:
        results['pazarama'] = {"error": str(e)}
        
    # Idefix
    try:
        from app.services.idefix_service import sync_idefix_products
        results['idefix'] = sync_idefix_products(user_id=user_id)
    except Exception as e:
        results['idefix'] = {"error": str(e)}

    # N11
    try:
        from app.services.n11_service import sync_n11_products
        results['n11'] = sync_n11_products(user_id=user_id)
    except Exception as e:
        results['n11'] = {"error": str(e)}

    # Hepsiburada
    try:
        from app.services.hepsiburada_service import sync_hepsiburada_products
        results['hepsiburada'] = sync_hepsiburada_products(user_id=user_id)
    except Exception as e:
        results['hepsiburada'] = {"error": str(e)}
        
    return results

def get_orders(user_id: int, page: int = 1, per_page: int = 20, marketplace: Optional[str] = None, status: Optional[str] = None, search: Optional[str] = None, sort_by: Optional[str] = None, order: Optional[str] = 'desc'):
    query = Order.query.filter_by(user_id=user_id)

    if marketplace:
        query = query.filter(Order.marketplace == marketplace)
    
    if status:
        # Use ILIKE for case-insensitive matching and handle whitespace
        status_term = f"%{status.strip()}%"
        query = query.filter(Order.status.ilike(status_term))
    
    if search:
        search_term = f"%{search.strip()}%"
        query = query.join(Customer, isouter=True).filter(
            db.or_(
                Order.order_number.ilike(search_term),
                Order.marketplace_order_id.ilike(search_term),
                Customer.first_name.ilike(search_term),
                Customer.last_name.ilike(search_term)
            )
        )

    # Sorting logic
    if sort_by == 'total_price':
        if order == 'asc':
            query = query.order_by(Order.total_price.asc())
        else:
            query = query.order_by(Order.total_price.desc())
    elif sort_by == 'marketplace':
        if order == 'asc':
            query = query.order_by(Order.marketplace.asc())
        else:
            query = query.order_by(Order.marketplace.desc())
    else:
        # Default sort by created_at desc
        query = query.order_by(Order.created_at.desc())

    return query.paginate(page=page, per_page=per_page, error_out=False)


def get_order_detail(order_id: int):
    return Order.query.get_or_404(order_id)


def _trigger_bugz_push(order, user_id=None):
    """
    Triggers the BUG-Z order creation in BACKGROUND to prevent timeout.
    """
    try:
        if not user_id:
            user_id = order.user_id
            
        if not user_id:
            return

        order_id = order.id

        # Background Task
        def _bg_task(o_id, u_id):
            from flask import current_app
            from app.models import User, Order
            from app.services.bug_z_service import BugZService
            
            # Ensure app context
            with current_app.app_context():
                try:
                    user_obj = User.query.get(u_id)
                    if not user_obj: return
                    
                    order_obj = Order.query.get(o_id)
                    if not order_obj: return
                    
                    bugz = BugZService(user_obj)
                    if bugz.is_configured():
                        logger.info(f"BUG-Z Push (Background) started for Order #{order_obj.order_number}")
                        bugz.create_order(order_obj)
                except Exception as ex:
                    logger.error(f"Background BUG-Z Push Error: {ex}")

        from app.services.job_queue import MP_EXECUTOR
        MP_EXECUTOR.submit(_bg_task, order_id, user_id)
            
    except Exception as e:
        logger.error(f"Error initiating BUG-Z background push: {e}")

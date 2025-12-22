import time
import copy
import json
import xmltodict
from typing import Dict, Any, List, Optional
from app.models import SupplierXML, Setting
from app.utils.helpers import fetch_xml_from_url, to_int, to_float

_XML_SOURCE_CACHE: Dict[int, Any] = {}
_XML_SOURCE_CACHE_LOCK = None # Will be initialized if needed, or just use dict (assuming single worker for now or handled by GIL)
# Actually app.py used threading.Lock. Let's import it.
import threading
_XML_SOURCE_CACHE_LOCK = threading.Lock()
XML_SOURCE_CACHE_TTL_SECONDS = 300
XML_SOURCE_CACHE_MAX = 10

def load_supplier_xml_map(user_id: int = None):
    url = (Setting.get('SUPPLIER_XML_URL', '', user_id=user_id) or '').strip()
    if not url:
        raise ValueError("Tedarik XML adresi ayarlanmamış (SUPPLIER_XML_URL). Ayarlar'dan giriniz.")
    raw = fetch_xml_from_url(url)
    try:
        data = xmltodict.parse(raw)
    except Exception as e:
        raise ValueError(f"XML parse hatası: {e}")
    # Try to locate product list robustly
    candidates = []
    if isinstance(data, dict):
        # common patterns
        for key in ['products','Items','items','urunler','catalog','root','xml']:
            node = data.get(key)
            if isinstance(node, dict):
                for subk in ['product','item','urun']:
                    if subk in node:
                        candidates = node[subk]
                        break
            if candidates:
                break
    if candidates is None:
        candidates = []
    if not isinstance(candidates, list):
        candidates = [candidates] if candidates else []

    def _g(row, *names):
        for n in names:
            if isinstance(row, dict) and n in row and row[n] is not None:
                val = str(row[n]).strip()
                if val != '':
                    return val
        return ''

    mp = {}
    for p in candidates:
        if not isinstance(p, dict):
            continue
        barcode = _g(p, 'barcode','barcod','Barkod','BARKOD','productBarcode','ProductBarcode','Barcode')
        if not barcode:
            # sometimes stock code is used
            barcode = _g(p, 'stockCode','StockCode','sku','SKU','productCode','ProductCode')
        if not barcode:
            continue
        qty_s = _g(p, 'quantity','Quantity','stok','Stok','OnHand','stock') or '0'
        price_s = _g(p, 'price','Price','salePrice','SalePrice','unitPrice','UnitPrice') or '0'
        try:
            qty = int(float(qty_s.replace(',','.')))
        except Exception:
            qty = 0
        try:
            price = float(str(price_s).replace(',','.'))
        except Exception:
            price = 0.0
        mp[str(barcode)] = { 'quantity': qty, 'price': price }
    return mp

def load_xml_source_index(xml_source_id: Any) -> Dict[str, Dict[str, Any]]:
    """Build a lightweight index from SupplierXML source for quick overrides."""
    index: Dict[str, Dict[str, Any]] = {}
    if not xml_source_id:
        return index
    
    # Handle Excel sources (format: "excel:{file_id}")
    source_str = str(xml_source_id)
    if source_str.startswith('excel:'):
        try:
            # Resolve user_id for Excel index
            u_id = None
            from flask_login import current_user
            if current_user and current_user.is_authenticated:
                u_id = current_user.id
            
            excel_data = Setting.get('_EXCEL_TEMP_INDEX', '', user_id=u_id)
            if excel_data:
                excel_index = json.loads(excel_data)
                # Convert to standard format
                by_barcode = excel_index.get('by_barcode', {})
                items = excel_index.get('items', [])
                index['by_barcode'] = by_barcode
                index['__records__'] = items
                # Also add direct barcode lookups
                for bc, record in by_barcode.items():
                    index[bc] = record
                return index
        except Exception as e:
            import logging
            logging.warning(f"Failed to load Excel index: {e}")
        return index
    
    try:
        cache_key = int(xml_source_id)
    except Exception:
        cache_key = None

    now = time.time()
    ttl = XML_SOURCE_CACHE_TTL_SECONDS
    if cache_key is not None:
        with _XML_SOURCE_CACHE_LOCK:
            cached = _XML_SOURCE_CACHE.get(cache_key)
            if cached:
                ts, data = cached
                if ttl == 0 or (now - ts) <= ttl:
                    return data
                _XML_SOURCE_CACHE.pop(cache_key, None)

    try:
        src = SupplierXML.query.filter_by(id=int(xml_source_id)).first()
    except Exception:
        return index
    if not src or not src.url:
        return index
    try:
        raw_xml = fetch_xml_from_url(src.url)
        xml_obj = xmltodict.parse(raw_xml)
    except Exception:
        return index

    node = xml_obj.get('products') or xml_obj.get('Items') or xml_obj
    if isinstance(node, dict):
        for key in ['product', 'item', 'urun']:
            if key in node:
                node = node.get(key)
                break
    if node is None:
        return index
    items = node if isinstance(node, list) else [node]

    records: List[Dict[str, Any]] = []
    by_barcode: Dict[str, Dict[str, Any]] = {}

    def _g(row, *names):
        for n in names:
            if isinstance(row, dict) and n in row and row[n] is not None:
                val = str(row[n]).strip()
                if val:
                    return val
        return ''

    for row in items:
        if not isinstance(row, dict):
            continue
        barcode = _g(row, 'barcode', 'barcod', 'Barkod', 'BARKOD', 'productBarcode', 'ProductBarcode', 'Barcode')
        if not barcode:
            barcode = _g(row, 'stockCode', 'StockCode', 'sku', 'SKU', 'productCode', 'ProductCode')
        if not barcode:
            continue
        title = _g(row, 'name', 'Name', 'productName', 'ProductName', 'title', 'Title')
        description = _g(row, 'detail', 'Detail', 'description', 'Description')
        stock_code = _g(row, 'stockCode', 'StockCode', 'productCode', 'ProductCode') or barcode
        quantity_str = _g(row, 'quantity', 'Quantity', 'stok', 'Stok', 'OnHand', 'stock') or '0'
        price_str = _g(row, 'price', 'Price', 'salePrice', 'SalePrice', 'unitPrice', 'UnitPrice', 'listPrice', 'ListPrice') or '0'
        # Maliyet Fiyatı
        cost_str = _g(row, 'cost', 'Cost', 'costPrice', 'CostPrice', 'maliyet', 'Maliyet', 'alisFiyati', 'AlisFiyati', 'basePrice', 'BasePrice') or '0'
        
        vat_raw = _g(row, 'tax', 'Tax', 'taxRate', 'TaxRate')
        try:
            quantity = int(float(quantity_str.replace(',', '.')))
        except Exception:
            quantity = 0
        try:
            price = float(str(price_str).replace(',', '.'))
        except Exception:
            price = 0.0
        try:
            cost = float(str(cost_str).replace(',', '.'))
        except Exception:
            cost = 0.0
        try:
            vat_rate = float(str(vat_raw).replace(',', '.')) * (100 if vat_raw and float(str(vat_raw).replace(',', '.')) <= 1 else 1)
        except Exception:
            vat_rate = 20.0

        images: List[Dict[str, str]] = []
        # 1. Try standard Image1, Image2...
        for i in range(1, 10):
            img_val = _g(row, f'image{i}', f'Image{i}', f'Resim{i}', f'resim{i}')
            if img_val:
                images.append({'url': img_val})
        
        # 2. Try 'Images' or 'Resimler' list/dict
        if not images:
            img_node = row.get('Images') or row.get('images') or row.get('Resimler') or row.get('resimler')
            if img_node:
                # If it's a list of strings or dicts
                if isinstance(img_node, list):
                    for item in img_node:
                        if isinstance(item, str):
                            images.append({'url': item})
                        elif isinstance(item, dict):
                            u = item.get('Image') or item.get('url') or item.get('#text')
                            if u: images.append({'url': u})
                elif isinstance(img_node, dict):
                    # Maybe <Images><Image>url</Image></Images>
                    sub = img_node.get('Image') or img_node.get('image') or img_node.get('Resim')
                    if isinstance(sub, list):
                        for s in sub:
                            if isinstance(s, str): images.append({'url': s})
                            elif isinstance(s, dict): 
                                u = s.get('#text') or s.get('url')
                                if u: images.append({'url': u})
                    elif isinstance(sub, str):
                        images.append({'url': sub})
        
        # 3. Try single 'Image' or 'Resim'
        if not images:
             img_val = _g(row, 'Image', 'image', 'Resim', 'resim', 'picture', 'Picture')
             if img_val:
                 images.append({'url': img_val})

        record = {
            'title': title,
            'description': description,
            # detail etiketi - HTML aciklama (ornek XML'deki gibi)
            'details': _g(row, 'detail', 'Detail', 'details', 'Details', 'detay', 'Detay', 'uzunaciklama', 'UzunAciklama'),
            'stockCode': stock_code,
            'quantity': quantity,
            'price': price,
            'cost': cost,
            'vatRate': vat_rate,
            'category': _g(row, 'category', 'Category', 'top_category', 'TopCategory'),
            'images': images,
            'barcode': barcode,
            'title_normalized': title.lower(),
        }
        
        # Varyant bilgilerini cek (XML'de variants etiketi varsa)
        variants_node = row.get('variants') or row.get('Variants') or row.get('varyantlar') or row.get('Varyantlar')
        if variants_node:
            variants_list = []
            # variants icerisindeki variant etiketlerini bul
            variant_items = variants_node.get('variant') or variants_node.get('Variant') or variants_node
            if not isinstance(variant_items, list):
                variant_items = [variant_items] if variant_items else []
            
            for v in variant_items:
                if not isinstance(v, dict):
                    continue
                
                # name1/value1, name2/value2 formatini destekle (ornek XML'deki gibi)
                var_item = {
                    'name1': _g(v, 'name1', 'Name1'),
                    'value1': _g(v, 'value1', 'Value1'),
                    'name2': _g(v, 'name2', 'Name2'),
                    'value2': _g(v, 'value2', 'Value2'),
                    # Eski format icin de destekle
                    'name': _g(v, 'name', 'Name', 'variantName', 'VariantName'),
                    'value': _g(v, 'value', 'Value', 'variantValue', 'VariantValue'),
                    'barcode': _g(v, 'barcode', 'Barcode', 'barkod', 'Barkod'),
                    'stock': to_int(_g(v, 'stock', 'Stock', 'quantity', 'Quantity') or '0'),
                    'price': to_float(_g(v, 'price', 'Price') or '0'),
                }
                
                # En az bir varyant bilgisi olmali
                if var_item.get('barcode') or var_item.get('name1') or var_item.get('name'):
                    variants_list.append(var_item)
            
            if variants_list:
                record['variants'] = variants_list
        
        records.append(record)
        index[str(barcode)] = record
        by_barcode[str(barcode)] = record
        if stock_code and stock_code != barcode:
            index[f'stock::{stock_code.lower()}'] = record
    index['__records__'] = records
    index['by_barcode'] = by_barcode

    if cache_key is not None:
        with _XML_SOURCE_CACHE_LOCK:
            if len(_XML_SOURCE_CACHE) >= XML_SOURCE_CACHE_MAX:
                oldest_key = min(_XML_SOURCE_CACHE.items(), key=lambda item: item[1][0])[0]
                _XML_SOURCE_CACHE.pop(oldest_key, None)
            _XML_SOURCE_CACHE[cache_key] = (now, index)

    return index

    return None

def lookup_xml_record(xml_index: Dict[str, Dict[str, Any]], code: Optional[str] = None, stock_code: Optional[str] = None, title: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not xml_index:
        return None
    if code:
        rec = xml_index.get(str(code))
        if rec:
            return rec
    if stock_code:
        key = f'stock::{str(stock_code).strip().lower()}'
        rec = xml_index.get(key)
        if rec:
            return rec
    records = xml_index.get('__records__') or []
    if title:
        title_norm = str(title).strip().lower()
        for rec in records:
            if rec.get('title_normalized') == title_norm:
                return rec
    return None

def perform_xml_cost_update(job_id: str, xml_source_id: int):
    """
    Background job to update product cost_prices from an XML source.
    Useful when supplier provides cost prices in XML and user wants to sync them.
    """
    from app.services.job_queue import append_mp_job_log, update_mp_job
    from app.models import Product
    import logging
    
    append_mp_job_log(job_id, f"XML Kaynağından maliyet güncelleme başlatıldı... ID: {xml_source_id}")
    
    try:
        # 1. Load XML Index
        index = load_xml_source_index(xml_source_id)
        if not index or 'by_barcode' not in index:
            update_mp_job(job_id, status='error', error_message="XML verisi yüklenemedi veya boş.")
            return

        records = index['by_barcode']
        append_mp_job_log(job_id, f"XML'de {len(records)} ürün bulundu. Yerel veritabanıyla eşleştiriliyor...")

        # 2. Get User ID from job
        from app.services.job_queue import get_mp_job
        job = get_mp_job(job_id)
        user_id = job.get('user_id') if job else None

        if not user_id:
            update_mp_job(job_id, status='error', error_message="Kullanıcı kimliği bulunamadı.")
            return

        from app import db
        local_products = Product.query.filter_by(user_id=user_id).all()
        updated_count = 0
        skipped_count = 0
        
        for p in local_products:
            # Match by barcode or stockCode
            match = records.get(p.barcode)
            if not match:
                # try lowercase stockCode
                match = index.get(f"stock::{p.stockCode.lower()}" if p.stockCode else "")
            
            if match and match.get('cost') and match['cost'] > 0:
                # Update cost
                p.cost_price = match['cost']
                updated_count += 1
            else:
                skipped_count += 1

        db.session.commit()
        
        msg = f"Maliyetler güncellendi. {updated_count} ürün güncellendi, {skipped_count} ürün XML'de bulunamadı veya maliyetsiz."
        append_mp_job_log(job_id, msg)
        update_mp_job(job_id, status='completed', progress=100)
        
    except Exception as e:
        from app import db
        db.session.rollback()
        logging.exception(f"XML Cost Update Error: {e}")
        update_mp_job(job_id, status='error', error_message=str(e))

import os
from datetime import datetime
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
import logging
logger = logging.getLogger(__name__)
_XML_SOURCE_CACHE_LOCK = threading.Lock()
_XML_PARSING_LOCK = threading.Lock() # Prevent concurrent heavy parsing
XML_SOURCE_CACHE_TTL_SECONDS = 0  # Cache geçici olarak kapalı
XML_SOURCE_CACHE_MAX = 5
CACHE_DIR = os.path.join(os.getcwd(), 'cache')
os.makedirs(CACHE_DIR, exist_ok=True)

def load_supplier_xml_map():
    url = (Setting.get('SUPPLIER_XML_URL', '') or '').strip()
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

def load_xml_source_index(xml_source_id: Any, force: bool = False) -> Dict[str, Dict[str, Any]]:
    """Build a lightweight index from SupplierXML source for quick overrides."""
    index: Dict[str, Dict[str, Any]] = {}
    if not xml_source_id:
        return index
    
    # Handle Excel sources (format: "excel:{file_id}")
    source_str = str(xml_source_id)
    if source_str.startswith('excel:'):
        try:
            excel_data = Setting.get('_EXCEL_TEMP_INDEX', '')
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
                    # Removed deepcopy for performance with large (30k+) XML datasets.
                    # Callers must treat this as read-only.
                    return data
                _XML_SOURCE_CACHE.pop(cache_key, None)

    # Disk Cache devre dışı (geçici)
    # cache_path = os.path.join(CACHE_DIR, f'xml_index_{xml_source_id}.json')
    # if not force and os.path.exists(cache_path):
    #     ...

    with _XML_PARSING_LOCK:
        # Re-verify cache inside lock to avoid redundant work
        if cache_key is not None:
            with _XML_SOURCE_CACHE_LOCK:
                cached = _XML_SOURCE_CACHE.get(cache_key)
                if cached:
                    ts, data = cached
                    if (now - ts) <= ttl: return data

        try:
            src = SupplierXML.query.filter_by(id=int(xml_source_id)).first()
        except Exception:
            return index
        if not src or not src.url:
            return index

        try:
            logger.info(f"XML Source {xml_source_id}: Downloading from {src.url}...")
            raw_xml = fetch_xml_from_url(src.url)
            logger.info(f"XML Source {xml_source_id}: Downloaded {len(raw_xml)} bytes. Parsing with xmltodict...")
            xml_obj = xmltodict.parse(raw_xml)
            logger.info(f"XML Source {xml_source_id}: Parse complete.")
        except Exception as e:
            logger.error(f"XML Source {xml_source_id}: Error downloading or parsing: {e}")
            index['_error'] = f"İndirme/Parse Hatası: {str(e)}"
            return index

    def find_product_list(data):
        # 1. Direct match for User's known structure (root -> product)
        if isinstance(data, dict):
            if 'root' in data:
                root = data['root']
                if isinstance(root, dict) and 'product' in root:
                    return root['product']
                    
            # 2. Direct keys at top level
            for key in ['products', 'product', 'Items', 'items', 'Urunler', 'urunler', 'Urun', 'urun']:
                if key in data:
                    val = data[key]
                    # If it's a list, great
                    if isinstance(val, list):
                        return val
                    # If it's a dict, check if it contains a sub-list (e.g. products -> product)
                    if isinstance(val, dict):
                        for sub in ['product', 'Product', 'item', 'Item', 'urun', 'Urun']:
                            if sub in val:
                                return val[sub]
                    # If straightforward dict (single item or container), return it to be listified
                    return val

        # 3. Fallback: Return original data to be wrapped in list
        return data

    node = find_product_list(xml_obj)
    
    if node is None:
        index['_error'] = "XML formatı tanınamadı (Ürün listesi bulunamadı). Lütfen XML yapısını kontrol edin."
        return index

    # Updated Indexing for Stock Code Priority
    start_time = time.time()
    items = node if isinstance(node, list) else [node]
    logger.info(f"XML Source {xml_source_id}: Processing {len(items)} items using Simple Lookup...")

    records: List[Dict[str, Any]] = []
    by_barcode: Dict[str, Dict[str, Any]] = {}
    by_stock_code: Dict[str, Dict[str, Any]] = {}

    def _g(row, *names):
        for n in names:
            if isinstance(row, dict) and n in row and row[n] is not None:
                val = str(row[n]).strip()
                if val:
                    return val
        return ''

    # Pre-load brand mapping to avoid 37k DB queries
    mapping_data = Setting.get('XML_BRAND_MAPPING', user_id=src.user_id)
    brand_mapping = {}
    if mapping_data:
        try:
            brand_mapping = json.loads(mapping_data)
        except Exception: pass
    
    for i, row in enumerate(items):
        if not isinstance(row, dict):
            continue
            
        # DEBUG: Log first few items
        if i < 3:
            logger.info(f"Processing XML Item #{i}. Keys: {list(row.keys())[:10]}")
        
        product_code = _g(row, 'productCode', 'ProductCode', 'product_code', 'Product_Code', 'code', 'Code')
        model_code = _g(row, 'modelCode', 'ModelCode', 'model_code', 'Model_Code', 'groupCode', 'GroupCode')
        barcode = _g(row, 'barcode', 'barcod', 'Barkod', 'BARKOD', 'productBarcode', 'ProductBarcode', 'Barcode')

        # DEBUG: Log extracted identifiers
        if i < 3:
            logger.info(f"Item #{i} IDs - Barcode: {barcode}, PCode: {product_code}, MCode: {model_code}")

        # Logic to handle generic/bad barcodes (Fix for "Bgz" issue)
        unique_id = barcode
        if product_code:
             bad_values = ['bgz', 'barkodsuz', 'yok', 'null', 'nan', 'undefined', 'boş']
             if not barcode or len(str(barcode)) < 3 or str(barcode).lower() in bad_values:
                 unique_id = product_code
        
        if not unique_id:
            if i < 3: logger.info(f"Item #{i} skipped: No valid ID found (uniq={unique_id}, bar={barcode}, pc={product_code})")
            continue

        barcode = unique_id # Use the chosen ID as the effective barcode
        
        # Extract fields
        title = _g(row, 'name', 'Name', 'productName', 'ProductName', 'title', 'Title')
        description = _g(row, 'detail', 'Detail', 'description', 'Description')
        
        # Stock Code Priority: 
        # 1. Explicit stockCode field
        # 2. productCode field
        # 3. barcode (fallback)
        stock_code = _g(row, 'stockCode', 'StockCode')
        if not stock_code: stock_code = product_code
        if not stock_code: stock_code = barcode
        
        quantity_str = _g(row, 'quantity', 'Quantity', 'stok', 'Stok', 'OnHand', 'stock') or '0'
        price_str = _g(row, 'price', 'Price', 'salePrice', 'SalePrice', 'unitPrice', 'UnitPrice', 'listPrice', 'ListPrice') or '0'
        vat_raw = _g(row, 'tax', 'Tax', 'taxRate', 'TaxRate')
        brand_raw = _g(row, 'brand', 'Brand', 'marka', 'Marka', 'manufacturer', 'Manufacturer')
        
        quantity = to_int(quantity_str, 0)
        price = to_float(price_str, 0.0)
        
        try:
            vat_rate = float(str(vat_raw).replace(',', '.')) * (100 if vat_raw and float(str(vat_raw).replace(',', '.')) <= 1 else 1)
        except Exception:
            vat_rate = 20.0
            
        # Apply Brand Mapping (Pre-loaded to avoid N queries)
        brand = apply_brand_mapping(brand_raw, src.user_id, mapping_dict=brand_mapping)

        # Images extraction (Simplified for brevity, assuming helper or same logic)
        images: List[Dict[str, str]] = []
        # 1. Try standard Image1, Image2...
        for k in range(1, 10):
            img_val = _g(row, f'image{k}', f'Image{k}', f'Resim{k}', f'resim{k}')
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

        link = _g(row, 'link', 'Link', 'url', 'Url', 'LİNK', 'Linkler', 'web', 'Web', 'productUrl', 'ProductUrl')
        
        record = {
            'title': title,
            'link': link,
            'description': description,
            'details': _g(row, 'detail', 'Detail', 'details', 'Details', 'detay', 'Detay', 'uzunaciklama', 'UzunAciklama'),
            'stockCode': stock_code,
            'quantity': quantity,
            'price': price,
            'vatRate': vat_rate,
            'brand': brand,
            'category': _g(row, 'category', 'Category', 'top_category', 'TopCategory'),
            'images': images,
            'barcode': barcode,
            'productCode': product_code,
            'modelCode': model_code,
            'title_normalized': title.lower() if title else "",
        }
        
        # Varyant bilgilerini cek (XML'de variants etiketi varsa)
        variants_node = row.get('variants') or row.get('Variants') or row.get('varyantlar') or row.get('Varyantlar')
        if variants_node:
            # variants icerisindeki variant etiketlerini bul
            variant_items = variants_node.get('variant') or variants_node.get('Variant') or variants_node
            if not isinstance(variant_items, list):
                variant_items = [variant_items] if variant_items else []
            
            for v in variant_items:
                if not isinstance(v, dict):
                    continue
                
                v_barcode = _g(v, 'barcode', 'Barcode', 'barkod', 'Barkod')
                if not v_barcode:
                    continue

                # Ana urun bilgilerini kopyala ve varyant ozellikleri ile guncelle
                v_record = record.copy()
                # Images listesini paylasabiliriz (cunku icerigini degistirmiyoruz)
                v_record['barcode'] = v_barcode
                v_record['parent_barcode'] = barcode
                v_record['productCode'] = product_code # Carry model level productCode
                v_record['modelCode'] = model_code # Carry model level modelCode
                v_record['details'] = record.get('details') # Carry HTML description
                v_record['quantity'] = to_int(_g(v, 'stock', 'Stock', 'quantity', 'Quantity') or '0')
                v_record['price'] = to_float(_g(v, 'price', 'Price') or str(price))
                
                # Varyant ozelliklerini sakla (Eslesme icin kritik)
                v_attrs = []
                for k_attr in range(1, 4): # support name1..name3
                    v_n = _g(v, f'name{k_attr}', f'Name{k_attr}')
                    v_v = _g(v, f'value{k_attr}', f'Value{k_attr}')
                    if v_n and v_v:
                        v_attrs.append({'name': v_n, 'value': v_v})
                
                if v_attrs:
                    v_record['variant_attributes'] = v_attrs
                    # Title'i guncelle (Eger varyant degeri varsa sona ekle)
                    v_record['title'] = f"{title} ({', '.join([a['value'] for a in v_attrs])})"
                    v_record['title_normalized'] = v_record['title'].lower() if v_record['title'] else ""
                
                records.append(v_record)
                index[str(v_barcode)] = v_record
                by_barcode[str(v_barcode)] = v_record
                if stock_code: # Variants might share the parent's stock code or have their own
                    by_stock_code[str(stock_code).strip()] = v_record # Use parent's stock code for variant if no specific variant stock code
        else:
            # Varyant yoksa sadece ana urunu ekle (Zaten eklenmisti, sadece mantiksal ayrim)
            records.append(record)
            index[str(barcode)] = record
            by_barcode[str(barcode)] = record
            if stock_code:
                by_stock_code[str(stock_code).strip()] = record

        # The original code had this outside the variant block, which is fine for the main record.
        # For variants, we want to ensure the variant record is indexed by stock_code if applicable.
        # if stock_code and stock_code != barcode:
        #     index[f'stock::{stock_code.lower()}'] = record
    index['__records__'] = records
    index['by_barcode'] = by_barcode
    index['by_stock_code'] = by_stock_code # New Index
    
    logger.info(f"XML Source {xml_source_id}: Finished processing {len(records)} records in {time.time() - start_time:.2f} seconds.")

    if cache_key is not None:
        with _XML_SOURCE_CACHE_LOCK:
            if len(_XML_SOURCE_CACHE) >= XML_SOURCE_CACHE_MAX:
                oldest_key = min(_XML_SOURCE_CACHE.items(), key=lambda item: item[1][0])[0]
                _XML_SOURCE_CACHE.pop(oldest_key, None)
            _XML_SOURCE_CACHE[cache_key] = (now, index)

    # Disk Cache kaydetme devre dışı (geçici)
    # try:
    #     with open(cache_path, 'w', encoding='utf-8') as f:
    #         json.dump(index, f)
    # except Exception:
    #     pass

    return index

def generate_random_barcode() -> str:
    """Generate a random 13-digit EAN-like barcode."""
    import random
    return "".join([str(random.randint(0, 9)) for _ in range(13)])

def refresh_xml_cache(xml_source_id: int, job_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Download XML, parse it, and save to partitioned SQLite database.
    This is the core of the High-Frequency sync.
    """
    from app import db
    from app.services.xml_db_manager import xml_db_manager, CachedXmlProduct
    from app.services.job_queue import append_mp_job_log
    
    src = SupplierXML.query.get(xml_source_id)
    if not src:
        return {'success': False, 'message': 'XML kaynağı bulunamadı.'}

    msg = f"XML Cache yenileniyor: {src.name}"
    logger.info(f"[XML-CACHE] {msg}")
    if job_id: append_mp_job_log(job_id, msg)

    try:
        # 1. Load XML into memory (index format)
        index = load_xml_source_index(xml_source_id, force=True)
        if '_error' in index:
            return {'success': False, 'message': index['_error']}
            
        records = index.get('__records__', [])
        if not records:
            return {'success': False, 'message': 'XML içerisinde ürün bulunamadı.'}

        # 2. Get partitioned DB session
        session = xml_db_manager.get_session(xml_source_id)
        
        try:
            # 3. Clear old cache for this source
            session.query(CachedXmlProduct).delete()
            
            # 4. Prepare bulk records
            bulk_items = []
            for r in records:
                bulk_items.append(CachedXmlProduct(
                    stock_code=r.get('stockCode'),
                    barcode=r.get('barcode'),
                    title=r.get('title'),
                    price=r.get('price', 0.0),
                    quantity=r.get('quantity', 0),
                    brand=r.get('brand'),
                    category=r.get('category'),
                    images_json=json.dumps(r.get('images', [])),
                    raw_data=json.dumps(r)
                ))
            
            # 5. Bulk insert (Fast for SQLite)
            session.bulk_save_objects(bulk_items)
            session.commit()
            
            # 6. Update last_cached_at in main DB
            src.last_cached_at = datetime.now()
            db.session.commit()
            
            msg = f"XML Cache başarıyla güncellendi. {len(records)} ürün veritabanına işlendi."
            logger.info(f"[XML-CACHE] {msg}")
            if job_id: append_mp_job_log(job_id, msg)
            
            return {'success': True, 'count': len(records)}
            
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    except Exception as e:
        msg = f"XML Cache yenilenirken hata oluştu: {str(e)}"
        logger.error(f"[XML-CACHE] {msg}")
        if job_id: append_mp_job_log(job_id, msg, level='error')
        return {'success': False, 'message': msg}

def search_xml_cache(xml_source_id: int, stock_code: str) -> Optional[Dict[str, Any]]:
    """
    Search for a product in the partitioned XML cache by stock code.
    """
    from app.services.xml_db_manager import xml_db_manager, CachedXmlProduct
    
    session = xml_db_manager.get_session(xml_source_id)
    try:
        product = session.query(CachedXmlProduct).filter_by(stock_code=stock_code).first()
        if product:
            # Reconstruct the dict format used by the app
            return json.loads(product.raw_data)
        return None
    finally:
        session.close()

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

def apply_brand_mapping(original_brand: str, user_id: int, mapping_dict: Optional[Dict[str, str]] = None) -> str:
    """Apply brand mapping rules for a user."""
    if not original_brand:
        return ""
    
    mapping = mapping_dict
    if mapping is None:
        mapping_data = Setting.get('XML_BRAND_MAPPING', user_id=user_id)
        if not mapping_data:
            return original_brand
        try:
            mapping = json.loads(mapping_data)
        except Exception:
            return original_brand
        
    # Check for exact match
    if original_brand in mapping:
        return mapping[original_brand]
        
    # Check for case-insensitive match
    orig_lower = original_brand.lower()
    for k, v in mapping.items():
        if k.lower() == orig_lower:
            return v
            
    return original_brand

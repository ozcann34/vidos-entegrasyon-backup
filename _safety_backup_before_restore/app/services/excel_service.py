"""
Excel Product Import Service
- Parse Excel/CSV files
- Smart column mapping
- Barcode/Stock code generation
"""
import os
import uuid
import json
import random
import string
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime
import logging

# Excel parsing
try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False

# Column mapping aliases
COLUMN_ALIASES = {
    'barcode': ['barkod', 'barcode', 'upc', 'ean', 'gtin', 'partner id'],
    'title': ['ürün adı', 'ürün ismi', 'product name', 'başlık', 'title', 'ad'],
    'description': ['ürün açıklaması', 'açıklama', 'description', 'detay'],
    'price': ['piyasa satış fiyatı', 'piyasa satış fiyatı (kdv dahil)', 'fiyat', 'price', 'liste fiyatı', 'list price'],
    'sale_price': ["trendyol'da satılacak fiyat", "trendyol'da satılacak fiyat (kdv dahil)", 'satış fiyatı', 'sale price', 'indirimli fiyat'],
    'quantity': ['ürün stok adedi', 'stok', 'stok adeti', 'stock', 'miktar', 'adet', 'quantity'],
    'brand': ['marka', 'brand'],
    'category': ['kategori ismi', 'kategori', 'category', 'kategori adı'],
    'model_code': ['model kodu', 'model', 'sku'],
    'stock_code': ['tedarikçi stok kodu', 'stok kodu', 'stock code', 'supplier code'],
    'color': ['ürün rengi', 'renk', 'color'],
    'size': ['beden', 'size', 'boyut', 'boyut/ebat'],
    'gender': ['cinsiyet', 'gender'],
    'desi': ['desi', 'hacim', 'weight', 'ağırlık'],
    'vat_rate': ['kdv oranı', 'kdv', 'vat', 'vat rate'],
    'commission': ['komisyon oranı', 'komisyon', 'commission'],
    'image1': ['görsel 1', 'image 1', 'resim 1', 'görsel1', 'image1'],
    'image2': ['görsel 2', 'image 2', 'resim 2', 'görsel2', 'image2'],
    'image3': ['görsel 3', 'image 3', 'resim 3', 'görsel3', 'image3'],
    'image4': ['görsel 4', 'image 4', 'resim 4', 'görsel4', 'image4'],
    'image5': ['görsel 5', 'image 5', 'resim 5', 'görsel5', 'image5'],
    'image6': ['görsel 6', 'image 6', 'resim 6', 'görsel6', 'image6'],
    'image7': ['görsel 7', 'image 7', 'resim 7', 'görsel7', 'image7'],
    'image8': ['görsel 8', 'image 8', 'resim 8', 'görsel8', 'image8'],
    'shipping_days': ['sevkiyat süresi', 'shipping time', 'kargo süresi'],
    'shipping_type': ['sevkiyat tipi', 'shipping type', 'kargo tipi'],
    'status': ['durum', 'status'],
    'status_desc': ['durum açıklaması', 'status description'],
    'link': ["trendyol.com linki", 'link', 'url'],
}

# In-memory storage for uploaded Excel files
# Structure: { user_id: { file_id: cache_entry } }
_EXCEL_USER_CACHES: Dict[int, Dict[str, Dict[str, Any]]] = {}
EXCEL_CACHE_MAX_PER_USER = 10

def get_user_excel_cache(user_id: int) -> Dict[str, Dict[str, Any]]:
    """Get or initialize the Excel cache for a specific user."""
    if user_id not in _EXCEL_USER_CACHES:
        _EXCEL_USER_CACHES[user_id] = {}
    return _EXCEL_USER_CACHES[user_id]


def turkish_lower(text: str) -> str:
    """
    Convert to lowercase with Turkish character support.
    İ -> i, I -> ı, Ş -> ş, etc.
    """
    if not text:
        return ""
    result = text.lower()
    result = result.replace('İ'.lower(), 'i')  # Python's İ.lower() gives 'i̇', fix it
    result = result.replace('ı', 'i')  # I -> ı in Turkish, but we want 'i'
    return result


def smart_map_columns(headers: List[str]) -> Dict[str, str]:
    """
    Map Excel headers to standard field names using aliases.
    Returns: {standard_field: excel_column_name}
    """
    mapping = {}
    headers_normalized = {turkish_lower(h.strip()): h for h in headers}
    
    logging.info(f"Column mapping - headers normalized: {list(headers_normalized.keys())[:10]}")
    
    for std_field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            alias_normalized = turkish_lower(alias)
            if alias_normalized in headers_normalized:
                mapping[std_field] = headers_normalized[alias_normalized]
                logging.info(f"Column mapped: {std_field} -> {mapping[std_field]}")
                break
    
    logging.info(f"Final column mapping: {mapping}")
    return mapping


def parse_excel_file(file_path: str, user_id: int, original_filename: str = None) -> Tuple[str, Dict[str, Any]]:
    """
    Parse Excel or CSV file and return file_id and metadata.
    Now also saves to disk and database for persistence, isolated by user_id.
    """
    if not PANDAS_AVAILABLE:
        raise ImportError("pandas kütüphanesi yüklü değil. pip install pandas openpyxl")
    
    file_id = str(uuid.uuid4())
    
    # Determine file type
    ext = os.path.splitext(file_path)[1].lower()
    
    if ext == '.csv':
        df = pd.read_csv(file_path, encoding='utf-8-sig')
    elif ext in ['.xlsx', '.xls']:
        df = pd.read_excel(file_path, engine='openpyxl' if ext == '.xlsx' else 'xlrd')
    else:
        raise ValueError(f"Desteklenmeyen dosya tipi: {ext}")
    
    # Clean column names
    df.columns = [str(c).strip() for c in df.columns]
    
    # Map columns
    column_mapping = smart_map_columns(df.columns.tolist())
    
    # Convert to records
    records = df.fillna('').to_dict('records')
    
    # Save to persistent storage - USER ISOLATED
    import shutil
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    excel_dir = os.path.join(base_dir, 'excel_uploads', str(user_id))
    os.makedirs(excel_dir, exist_ok=True)
    
    saved_filename = f"{file_id}{ext}"
    saved_path = os.path.join(excel_dir, saved_filename)
    shutil.copy2(file_path, saved_path)
    
    # Save to database
    try:
        from app.models import ExcelFile
        from app import db
        
        excel_record = ExcelFile(
            user_id=user_id,
            file_id=file_id,
            filename=saved_filename,
            original_filename=original_filename or os.path.basename(file_path),
            total_products=len(records),
            matched_columns=len(column_mapping),
            total_columns=len(df.columns),
            column_mapping=json.dumps(column_mapping)
        )
        db.session.add(excel_record)
        db.session.commit()
    except Exception as e:
        logging.warning(f"Failed to save Excel to database: {e}")
    
    # Store in cache
    user_cache = get_user_excel_cache(user_id)
    cache_entry = {
        'file_id': file_id,
        'filename': original_filename or os.path.basename(file_path),
        'uploaded_at': datetime.utcnow().isoformat(),
        'total_products': len(records),
        'columns': df.columns.tolist(),
        'column_mapping': column_mapping,
        'matched_columns': len(column_mapping),
        'total_columns': len(df.columns),
        'records': records,
        'saved_path': saved_path,
    }
    
    # Manage cache size
    if len(user_cache) >= EXCEL_CACHE_MAX_PER_USER:
        oldest = min(user_cache.keys(), key=lambda k: user_cache[k]['uploaded_at'])
        del user_cache[oldest]
    
    user_cache[file_id] = cache_entry
    
    return file_id, {
        'file_id': file_id,
        'filename': cache_entry['filename'],
        'total_products': cache_entry['total_products'],
        'columns': cache_entry['columns'],
        'column_mapping': cache_entry['column_mapping'],
        'matched_columns': cache_entry['matched_columns'],
        'total_columns': cache_entry['total_columns'],
    }

def get_excel_metadata(file_id: str, user_id: int) -> Optional[Dict[str, Any]]:
    """Get metadata for an uploaded Excel file."""
    # First check cache
    user_cache = get_user_excel_cache(user_id)
    entry = user_cache.get(file_id)
    if entry:
        return {
            'file_id': entry['file_id'],
            'filename': entry['filename'],
            'total_products': entry['total_products'],
            'columns': entry['columns'],
            'column_mapping': entry['column_mapping'],
            'matched_columns': entry['matched_columns'],
            'total_columns': entry['total_columns'],
        }
    
    # Try to load from database
    try:
        from app.models import ExcelFile
        excel_record = ExcelFile.get_by_file_id(file_id, user_id=user_id)
        if excel_record:
            # Load from disk if not in cache
            load_saved_excel(file_id, user_id=user_id)
            entry = user_cache.get(file_id)
            if entry:
                return {
                    'file_id': entry['file_id'],
                    'filename': entry['filename'],
                    'total_products': entry['total_products'],
                    'columns': entry.get('columns', []),
                    'column_mapping': entry['column_mapping'],
                    'matched_columns': entry['matched_columns'],
                    'total_columns': entry['total_columns'],
                }
    except Exception as e:
        logging.warning(f"Failed to load Excel from database: {e}")
    
    return None


def list_saved_excel_files(user_id: int) -> List[Dict[str, Any]]:
    """List all saved Excel files from database for a specific user."""
    try:
        from app.models import ExcelFile
        files = ExcelFile.get_all(user_id=user_id)
        return [f.to_dict() for f in files]
    except Exception as e:
        logging.warning(f"Failed to list Excel files: {e}")
        return []


def load_saved_excel(file_id: str, user_id: int) -> bool:
    """Load a saved Excel file from disk into cache for a specific user."""
    user_cache = get_user_excel_cache(user_id)
    # Already in cache?
    if file_id in user_cache:
        return True
    
    try:
        from app.models import ExcelFile
        excel_record = ExcelFile.get_by_file_id(file_id, user_id=user_id)
        if not excel_record:
            return False
        
        # Find the file on disk
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        excel_dir = os.path.join(base_dir, 'excel_uploads', str(user_id))
        file_path = os.path.join(excel_dir, excel_record.filename)
        
        if not os.path.exists(file_path):
            logging.warning(f"Excel file not found on disk: {file_path}")
            return False
        
        # Parse and load into cache
        ext = os.path.splitext(file_path)[1].lower()
        
        if ext == '.csv':
            df = pd.read_csv(file_path, encoding='utf-8-sig')
        elif ext in ['.xlsx', '.xls']:
            df = pd.read_excel(file_path, engine='openpyxl' if ext == '.xlsx' else 'xlrd')
        else:
            return False
        
        df.columns = [str(c).strip() for c in df.columns]
        records = df.fillna('').to_dict('records')
        column_mapping = json.loads(excel_record.column_mapping) if excel_record.column_mapping else {}
        
        cache_entry = {
            'file_id': file_id,
            'filename': excel_record.original_filename,
            'uploaded_at': excel_record.created_at.isoformat() if excel_record.created_at else datetime.utcnow().isoformat(),
            'total_products': len(records),
            'columns': df.columns.tolist(),
            'column_mapping': column_mapping,
            'matched_columns': len(column_mapping),
            'total_columns': len(df.columns),
            'records': records,
            'saved_path': file_path,
        }
        
        # Manage cache size
        if len(user_cache) >= EXCEL_CACHE_MAX_PER_USER:
            oldest = min(user_cache.keys(), key=lambda k: user_cache[k]['uploaded_at'])
            del user_cache[oldest]
        
        user_cache[file_id] = cache_entry
        return True
        
    except Exception as e:
        logging.warning(f"Failed to load Excel file: {e}")
        return False


def delete_excel_file(file_id: str, user_id: int) -> bool:
    """Delete a saved Excel file from database and disk for a specific user."""
    try:
        from app.models import ExcelFile
        from app import db
        
        excel_record = ExcelFile.get_by_file_id(file_id, user_id=user_id)
        if not excel_record:
            return False
        
        # Delete from disk
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        excel_dir = os.path.join(base_dir, 'excel_uploads', str(user_id))
        file_path = os.path.join(excel_dir, excel_record.filename)
        if os.path.exists(file_path):
            os.remove(file_path)
        
        # Delete from database
        db.session.delete(excel_record)
        db.session.commit()
        
        # Remove from cache
        user_cache = get_user_excel_cache(user_id)
        user_cache.pop(file_id, None)
        
        return True
    except Exception as e:
        logging.warning(f"Failed to delete Excel file: {e}")
        return False


def generate_all_random_codes(file_id: str, user_id: int, prefix: str = '', code_type: str = 'both', title_prefix: str = '') -> Dict[str, Any]:
    """
    Generate random barcodes and/or stock codes for ALL products in Excel file.
    """
    user_cache = get_user_excel_cache(user_id)
    entry = user_cache.get(file_id)
    
    # Try to load from disk if not in cache
    if not entry:
        if load_saved_excel(file_id, user_id=user_id):
            entry = user_cache.get(file_id)
    
    if not entry:
        return {'success': False, 'message': 'Excel dosyası bulunamadı'}
    
    records = entry['records']
    mapping = entry['column_mapping']
    title_col = mapping.get('title', '')
    updated = 0
    
    for record in records:
        if code_type == 'both':
            # Generate distinct codes for each
            rnd1 = ''.join(random.choices(string.digits, k=11))
            rnd2 = ''.join(random.choices(string.digits, k=11))
            record['_custom_barcode'] = f"{prefix}{rnd1}"
            record['_custom_stock_code'] = f"{prefix}{rnd2}"
            
        elif code_type == 'barcode':
            rnd = ''.join(random.choices(string.digits, k=11))
            record['_custom_barcode'] = f"{prefix}{rnd}"
            
        elif code_type == 'stock':
            rnd = ''.join(random.choices(string.digits, k=11))
            record['_custom_stock_code'] = f"{prefix}{rnd}"
        
        # Apply title prefix if provided
        if title_prefix and title_col:
            original_title = str(record.get(title_col, '')).strip()
            if original_title:
                # Store custom prefixed title
                record['_custom_title'] = f"{title_prefix}{original_title}"
        
        updated += 1
    
    # Re-save to database
    try:
        from app.models import ExcelFile
        from app import db
        
        excel_record = ExcelFile.get_by_file_id(file_id, user_id=user_id)
        if excel_record:
            # Update the stored data with new codes
            excel_record.column_mapping = json.dumps(entry.get('column_mapping', {}))
            db.session.commit()
    except Exception as e:
        logging.warning(f"Failed to save updated Excel: {e}")
    
    return {
        'success': True,
        'updated': updated,
        'message': f'{updated} ürün için kod oluşturuldu (Kod öneki: {prefix}' + (f', Başlık öneki: {title_prefix}' if title_prefix else '') + ')'
    }


def get_excel_products(file_id: str, user_id: int, page: int = 1, per_page: int = 25, search: str = '') -> Dict[str, Any]:
    """
    Get paginated products from uploaded Excel file for a specific user.
    """
    user_cache = get_user_excel_cache(user_id)
    entry = user_cache.get(file_id)
    if not entry:
        # Try to load from disk
        if load_saved_excel(file_id, user_id=user_id):
            entry = user_cache.get(file_id)
            
    if not entry:
        return {'success': False, 'message': 'Dosya bulunamadı'}
    
    records = entry['records']
    mapping = entry['column_mapping']
    
    # Apply search filter
    if search:
        search_lower = search.lower()
        filtered = []
        for r in records:
            # Search in barcode, title, stock_code
            barcode_col = mapping.get('barcode', '')
            title_col = mapping.get('title', '')
            stock_col = mapping.get('stock_code', '')
            
            barcode = str(r.get(barcode_col, '')).lower() if barcode_col else ''
            title = str(r.get(title_col, '')).lower() if title_col else ''
            stock = str(r.get(stock_col, '')).lower() if stock_col else ''
            
            if search_lower in barcode or search_lower in title or search_lower in stock:
                filtered.append(r)
        records = filtered
    
    # Pagination
    total = len(records)
    total_pages = (total + per_page - 1) // per_page
    start = (page - 1) * per_page
    end = start + per_page
    page_records = records[start:end]
    
    # Normalize records to standard fields
    normalized = []
    for idx, r in enumerate(page_records, start=start+1):
        item = {'_index': idx, '_raw': r}
        for std_field, excel_col in mapping.items():
            # Prioritize custom generated codes and titles
            if std_field == 'barcode' and '_custom_barcode' in r and r['_custom_barcode']:
                item[std_field] = r['_custom_barcode']
            elif std_field == 'stock_code' and '_custom_stock_code' in r and r['_custom_stock_code']:
                item[std_field] = r['_custom_stock_code']
            elif std_field == 'title' and '_custom_title' in r and r['_custom_title']:
                item[std_field] = r['_custom_title']
            else:
                item[std_field] = r.get(excel_col, '')
        normalized.append(item)
    
    return {
        'success': True,
        'products': normalized,
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': total_pages,
        'column_mapping': mapping,
    }


def get_products_by_indices(file_id: str, user_id: int, indices: List[int]) -> List[Dict[str, Any]]:
    """Get specific products by their indices with user isolation."""
    user_cache = get_user_excel_cache(user_id)
    entry = user_cache.get(file_id)
    if not entry:
        if load_saved_excel(file_id, user_id=user_id):
            entry = user_cache.get(file_id)
            
    if not entry:
        return []
    
    records = entry['records']
    mapping = entry['column_mapping']
    
    result = []
    for idx in indices:
        if 0 <= idx < len(records):
            r = records[idx]
            item = {'_index': idx, '_raw': r}
            for std_field, excel_col in mapping.items():
                # Prioritize custom generated codes and titles (same logic as get_excel_products)
                if std_field == 'barcode' and '_custom_barcode' in r and r['_custom_barcode']:
                    item[std_field] = r['_custom_barcode']
                elif std_field == 'stock_code' and '_custom_stock_code' in r and r['_custom_stock_code']:
                    item[std_field] = r['_custom_stock_code']
                elif std_field == 'title' and '_custom_title' in r and r['_custom_title']:
                    item[std_field] = r['_custom_title']
                else:
                    item[std_field] = r.get(excel_col, '')
            result.append(item)
    
    return result


def generate_barcode(prefix: str = '', length: int = 13) -> str:
    """
    Generate a random barcode.
    If prefix provided, use it and fill rest with random digits.
    """
    prefix = prefix.upper()[:2] if prefix else ''
    remaining = length - len(prefix)
    
    if remaining > 0:
        random_part = ''.join(random.choices(string.digits, k=remaining))
        return prefix + random_part
    return prefix[:length]


def generate_stock_code(prefix: str = '', length: int = 10) -> str:
    """
    Generate a random stock code.
    Format: [PREFIX][RANDOM_ALPHANUMERIC]
    """
    prefix = prefix.upper()[:2] if prefix else ''
    remaining = length - len(prefix)
    
    if remaining > 0:
        chars = string.ascii_uppercase + string.digits
        random_part = ''.join(random.choices(chars, k=remaining))
        return prefix + random_part
    return prefix[:length]


def update_product_codes(file_id: str, user_id: int, indices: List[int], barcode: Optional[str] = None, stock_code: Optional[str] = None) -> Dict[str, Any]:
    """
    Update barcode and/or stock code for specific products with user isolation.
    """
    user_cache = get_user_excel_cache(user_id)
    entry = user_cache.get(file_id)
    if not entry:
        if load_saved_excel(file_id, user_id=user_id):
            entry = user_cache.get(file_id)
            
    if not entry:
        return {'success': False, 'message': 'Dosya bulunamadı'}
    
    mapping = entry['column_mapping']
    barcode_col = mapping.get('barcode', '')
    stock_col = mapping.get('stock_code', '')
    
    updated = 0
    for idx in indices:
        if 0 <= idx < len(entry['records']):
            if barcode and barcode_col:
                entry['records'][idx][barcode_col] = barcode
            if stock_code and stock_col:
                entry['records'][idx][stock_col] = stock_code
            updated += 1
    
    return {'success': True, 'updated': updated}


def bulk_generate_codes(file_id: str, user_id: int, indices: List[int], prefix: str = '', generate_barcode_flag: bool = True, generate_stock_flag: bool = True) -> Dict[str, Any]:
    """
    Generate unique codes for multiple products with user isolation.
    """
    user_cache = get_user_excel_cache(user_id)
    entry = user_cache.get(file_id)
    if not entry:
        if load_saved_excel(file_id, user_id=user_id):
            entry = user_cache.get(file_id)
            
    if not entry:
        return {'success': False, 'message': 'Dosya bulunamadı'}
    
    mapping = entry['column_mapping']
    barcode_col = mapping.get('barcode', '')
    stock_col = mapping.get('stock_code', '')
    
    updated = 0
    
    # Ensure prefix is string
    prefix = (prefix or '').upper()[:2]
    
    for idx in indices:
        if 0 <= idx < len(entry['records']):
            # Generate consistent random part (11 digits)
            random_part = ''.join(random.choices(string.digits, k=11))
            new_code = f"{prefix}{random_part}"
            
            if generate_barcode_flag and barcode_col:
                entry['records'][idx][barcode_col] = new_code
            if generate_stock_flag and stock_col:
                entry['records'][idx][stock_col] = new_code
            updated += 1
    
    return {'success': True, 'updated': updated}


def build_excel_index(file_id: str, user_id: int, title_prefix: str = '') -> Optional[Dict[str, Any]]:
    """
    Build an XML-compatible index from Excel data for a specific user.
    """
    user_cache = get_user_excel_cache(user_id)
    entry = user_cache.get(file_id)
    
    # If not in cache, try to load from disk
    if not entry:
        logging.info(f"Excel {file_id} not in cache for user {user_id}, loading from disk...")
        if load_saved_excel(file_id, user_id=user_id):
            entry = user_cache.get(file_id)
    
    if not entry:
        logging.error(f"Excel {file_id} could not be loaded")
        return None
    
    logging.info(f"Building Excel index for {file_id} with title_prefix='{title_prefix}'")
    
    records = entry['records']
    mapping = entry['column_mapping']
    
    by_barcode = {}
    by_stock_code = {}
    items = []
    
    for r in records:
        # Get mapped values - PRIORITY: Use custom generated codes if they exist
        barcode = str(r.get('_custom_barcode', '')).strip() or str(r.get(mapping.get('barcode', ''), '')).strip()
        stock_code = str(r.get('_custom_stock_code', '')).strip() or str(r.get(mapping.get('stock_code', ''), '')).strip()
        title = str(r.get('_custom_title', '')).strip() or str(r.get(mapping.get('title', ''), '')).strip()
        
        # Apply title prefix if provided
        if title_prefix and title:
            title = f"{title_prefix}{title}"
        
        description = str(r.get(mapping.get('description', ''), '')).strip()
        brand = str(r.get(mapping.get('brand', ''), '')).strip()
        category = str(r.get(mapping.get('category', ''), '')).strip()
        
        # Brand ID will be resolved via Trendyol API during send (in perform_trendyol_send_products)
        # We just store the brand name here
        brand_id = 0
        
        # Price handling
        price_raw = r.get(mapping.get('price', ''), 0)
        sale_price_raw = r.get(mapping.get('sale_price', ''), 0)
        try:
            price = float(str(price_raw).replace(',', '.').replace('₺', '').replace('TL','').strip() or 0)
        except:
            price = 0
        try:
            sale_price = float(str(sale_price_raw).replace(',', '.').replace('₺', '').replace('TL','').strip() or 0)
        except:
            sale_price = price
        
        # Quantity
        qty_raw = r.get(mapping.get('quantity', ''), 0)
        try:
            quantity = int(float(str(qty_raw).replace(',', '.').strip() or 0))
        except:
            quantity = 0
        
        # Images - format as dict with 'url' key for Trendyol compatibility
        images = []
        for i in range(1, 9):
            img_col = mapping.get(f'image{i}', '')
            if img_col:
                img_url = str(r.get(img_col, '')).strip()
                if img_url and img_url.startswith('http'):
                    images.append({'url': img_url})
        
        # Build product record (XML-compatible format)
        # Resolve category_id from saved mappings
        category_id = 0
        if category:
            try:
                category_mappings_json = Setting.get('EXCEL_CATEGORY_MAPPINGS', '', user_id=current_user.id)
                if category_mappings_json:
                    category_mappings = json_lib.loads(category_mappings_json)
                    category_id = category_mappings.get(category.lower(), 0)
            except:
                pass
        
        product = {
            'barcode': barcode,
            'stock_code': stock_code,
            'stockCode': stock_code,  # Alias for compatibility
            'title': title,
            'description': description or title,
            'brand': brand,
            'brand_id': brand_id,  # Pre-resolved Trendyol brand ID
            'brandId': brand_id,  # Alias for compatibility
            'vendor': brand,  # Alias
            'category': category,
            'category_id': category_id,  # Pre-resolved Trendyol category ID
            'categoryId': category_id,  # Alias for compatibility
            'top_category': category.split('>')[0].strip() if '>' in category else category,
            'price': sale_price or price,
            'list_price': price,
            'quantity': quantity,
            'images': images,
            'color': str(r.get(mapping.get('color', ''), '')).strip(),
            'size': str(r.get(mapping.get('size', ''), '')).strip(),
            'gender': str(r.get(mapping.get('gender', ''), '')).strip(),
            'desi': str(r.get(mapping.get('desi', ''), '')).strip(),
            'vat_rate': str(r.get(mapping.get('vat_rate', ''), '')).strip(),
        }
        
        items.append(product)
        
        if barcode:
            by_barcode[barcode] = product
        if stock_code:
            by_stock_code[stock_code] = product
    
    return {
        'items': items,
        'by_barcode': by_barcode,
        'by_stock_code': by_stock_code,
        'total': len(items),
        'source_type': 'excel',
        'file_id': file_id,
    }


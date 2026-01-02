from flask import Blueprint, render_template, request, jsonify
from flask_login import login_required, current_user
from app import db
from app.models.sync_exception import SyncException

sync_exceptions_bp = Blueprint('sync_exceptions', __name__)

@sync_exceptions_bp.route('/sync-exceptions')
@login_required
def index():
    return render_template('sync_exceptions.html')

@sync_exceptions_bp.route('/api/sync-exceptions', methods=['GET'])
@login_required
def get_exceptions():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    
    # Limit per_page to reasonable values
    per_page = min(max(per_page, 10), 200)
    
    query = SyncException.query.filter_by(user_id=current_user.id)
    total = query.count()
    
    exceptions = query.order_by(SyncException.created_at.desc())\
        .offset((page - 1) * per_page)\
        .limit(per_page)\
        .all()
    
    return jsonify({
        'items': [e.to_dict() for e in exceptions],
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': (total + per_page - 1) // per_page
    })

@sync_exceptions_bp.route('/api/sync-exceptions', methods=['POST'])
@login_required
def add_exception():
    data = request.get_json()
    value = data.get('value', '').strip()
    match_type = data.get('match_type', 'stock_code')
    note = data.get('note', '')
    
    if not value:
        return jsonify({'success': False, 'message': 'Değer boş olamaz.'}), 400
        
    try:
        # Check existing
        existing = SyncException.query.filter_by(
            user_id=current_user.id, 
            value=value,
            match_type=match_type
        ).first()
        
        if existing:
            return jsonify({'success': False, 'message': 'Bu kayıt zaten var.'}), 400
            
        new_exc = SyncException(
            user_id=current_user.id,
            value=value,
            match_type=match_type,
            note=note
        )
        db.session.add(new_exc)
        db.session.commit()
        return jsonify({'success': True, 'item': new_exc.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

@sync_exceptions_bp.route('/api/sync-exceptions/<int:id>', methods=['DELETE'])
@login_required
def delete_exception(id):
    exc = SyncException.query.filter_by(id=id, user_id=current_user.id).first()
    if not exc:
        return jsonify({'success': False, 'message': 'Kayıt bulunamadı.'}), 404
        
    try:
        db.session.delete(exc)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500


@sync_exceptions_bp.route('/api/sync-exceptions/upload', methods=['POST'])
@login_required
def upload_sync_exceptions():
    """Excel dosyasından toplu istisna yükleme"""
    import logging
    try:
        if 'file' not in request.files:
            return jsonify({'success': False, 'message': 'Dosya bulunamadı'}), 400
        
        file = request.files['file']
        
        if file.filename == '':
            return jsonify({'success': False, 'message': 'Dosya seçilmedi'}), 400
        
        if not file.filename.endswith(('.xlsx', '.xls')):
            return jsonify({'success': False, 'message': 'Sadece Excel dosyaları (.xlsx, .xls) kabul edilir'}), 400
        
        # Parse Excel
        import pandas as pd
        import io
        
        try:
            # Read with limited rows first to validate format (performance optimization)
            file_content = file.read()
            df = pd.read_excel(io.BytesIO(file_content), nrows=5000)  # Limit to 5000 rows max
            
            if len(df) == 0:
                return jsonify({'success': False, 'message': 'Excel dosyası boş'}), 400
                
        except Exception as e:
            return jsonify({'success': False, 'message': f'Excel dosyası okunamadı: {str(e)}'}), 400
        
        # Normalize column names (case-insensitive)
        df.columns = df.columns.str.strip().str.lower()
        
        # Check for required columns
        has_stock_code = 'stok kodu' in df.columns or 'stock_code' in df.columns or 'stokkodu' in df.columns
        has_barcode = 'barkod' in df.columns or 'barcode' in df.columns
        
        if not has_stock_code and not has_barcode:
            return jsonify({'success': False, 'message': 'Excel dosyası "Stok Kodu" veya "Barkod" sütunu içermelidir'}), 400
        
        # Find column names (flexible matching)
        stock_code_col = None
        barcode_col = None
        note_col = None
        
        for col in df.columns:
            if 'stok' in col and 'kod' in col:
                stock_code_col = col
            elif 'barcode' in col or 'barkod' in col:
                barcode_col = col
            elif 'not' in col or 'note' in col or 'açıklama' in col:
                note_col = col
        
        
        # PERFORMANCE FIX: Fetch ALL existing exceptions for this user ONCE (not per row!)
        existing_exceptions = SyncException.query.filter_by(user_id=current_user.id).all()
        existing_set = {(e.match_type, e.value) for e in existing_exceptions}
        
        added = 0
        skipped = 0
        new_exceptions = []  # Collect all new items for bulk insert
        
        for index, row in df.iterrows():
            stock_code = str(row.get(stock_code_col, '')).strip() if stock_code_col and pd.notna(row.get(stock_code_col)) else ''
            barcode = str(row.get(barcode_col, '')).strip() if barcode_col and pd.notna(row.get(barcode_col)) else ''
            note = str(row.get(note_col, '')).strip() if note_col and pd.notna(row.get(note_col)) else ''
            
            # Skip empty rows
            if not stock_code and not barcode:
                continue
            
            # Check stock code
            if stock_code:
                key = ('stock_code', stock_code)
                if key not in existing_set:
                    new_exceptions.append(SyncException(
                        user_id=current_user.id,
                        match_type='stock_code',
                        value=stock_code,
                        note=note or None
                    ))
                    existing_set.add(key)  # Prevent duplicates within same upload
                    added += 1
                else:
                    skipped += 1
            
            # Check barcode (if different from stock code)
            if barcode and barcode != stock_code:
                key = ('barcode', barcode)
                if key not in existing_set:
                    new_exceptions.append(SyncException(
                        user_id=current_user.id,
                        match_type='barcode',
                        value=barcode,
                        note=note or None
                    ))
                    existing_set.add(key)
                    added += 1
                else:
                    skipped += 1
        
        # Bulk insert all new exceptions at once
        if new_exceptions:
            db.session.bulk_save_objects(new_exceptions)
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'added': added,
            'skipped': skipped,
            'message': f'{added} kayıt eklendi, {skipped} kayıt zaten mevcut.'
        })
        
    except Exception as e:
        logging.exception("Excel upload error")
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500

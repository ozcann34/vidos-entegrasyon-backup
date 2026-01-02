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
    exceptions = SyncException.query.filter_by(user_id=current_user.id).all()
    return jsonify([e.to_dict() for e in exceptions])

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
        
        added = 0
        skipped = 0
        
        # Batch process for better performance
        batch_size = 100
        batch = []
        
        for index, row in df.iterrows():
            stock_code = str(row.get(stock_code_col, '')).strip() if stock_code_col and pd.notna(row.get(stock_code_col)) else ''
            barcode = str(row.get(barcode_col, '')).strip() if barcode_col and pd.notna(row.get(barcode_col)) else ''
            note = str(row.get(note_col, '')).strip() if note_col and pd.notna(row.get(note_col)) else ''
            
            # Skip empty rows
            if not stock_code and not barcode:
                continue
            
            # Try to add stock code if exists
            if stock_code:
                existing = SyncException.query.filter_by(
                    user_id=current_user.id,
                    match_type='stock_code',
                    value=stock_code
                ).first()
                
                if not existing:
                    exc = SyncException(
                        user_id=current_user.id,
                        match_type='stock_code',
                        value=stock_code,
                        note=note or None
                    )
                    db.session.add(exc)
                    added += 1
                else:
                    skipped += 1
            
            # Try to add barcode if exists and different from stock code
            if barcode and barcode != stock_code:
                existing = SyncException.query.filter_by(
                    user_id=current_user.id,
                    match_type='barcode',
                    value=barcode
                ).first()
                
                if not existing:
                    exc = SyncException(
                        user_id=current_user.id,
                        match_type='barcode',
                        value=barcode,
                        note=note or None
                    )
                    db.session.add(exc)
                    added += 1
                else:
                    skipped += 1
            
            # Commit in batches for performance
            if len(batch) >= batch_size:
                db.session.commit()
                batch = []
        
        # Commit remaining
        if batch:
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

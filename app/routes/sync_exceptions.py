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

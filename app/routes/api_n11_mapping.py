"""N11 Kategori Mapping API Endpoints"""
import json
import logging
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from app.models import Setting
from app.services.n11_service import _N11_CATEGORY_CACHE, fetch_and_cache_n11_categories

n11_mapping_bp = Blueprint('n11_mapping', __name__)

@n11_mapping_bp.route('/api/n11/category_mapping', methods=['GET'])
@login_required
def get_n11_category_mapping():
    """Get current N11 category mapping for user"""
    try:
        mapping_json = Setting.get('N11_CATEGORY_MAPPING', user_id=current_user.id)
        mapping = json.loads(mapping_json) if mapping_json else {}
        return jsonify({'success': True, 'mapping': mapping, 'count': len(mapping)})
    except Exception as e:
        logging.exception("Error getting N11 category mapping")
        return jsonify({'success': False, 'message': str(e)}), 500


@n11_mapping_bp.route('/api/n11/category_mapping', methods=['POST'])
@login_required
def save_n11_category_mapping():
    """Save N11 category mapping for user"""
    try:
        data = request.get_json()
        mapping = data.get('mapping', {})
        
        # Validate format: {string: int}
        for key, value in mapping.items():
            if not isinstance(key, str) or not isinstance(value, (int, str)):
                return jsonify({
                    'success': False, 
                    'message': f'Geçersiz format: {key} -> {value}'
                }), 400
        
        # Save
        Setting.set('N11_CATEGORY_MAPPING', json.dumps(mapping), user_id=current_user.id)
        
        return jsonify({
            'success': True, 
            'message': f'{len(mapping)} kategori eşleşmesi kaydedildi',
            'count': len(mapping)
        })
        
    except Exception as e:
        logging.exception("Error saving N11 category mapping")
        return jsonify({'success': False, 'message': str(e)}), 500


@n11_mapping_bp.route('/api/n11/categories', methods=['GET'])
@login_required
def get_n11_categories():
    """Get N11 category list"""
    try:
        # Ensure categories are loaded
        if not _N11_CATEGORY_CACHE["loaded"] or not _N11_CATEGORY_CACHE["list"]:
            fetch_and_cache_n11_categories(user_id=current_user.id)
        
        categories = _N11_CATEGORY_CACHE["list"]
        
        return jsonify({
            'success': True, 
            'categories': categories,
            'count': len(categories)
        })
    except Exception as e:
        logging.exception("Error getting N11 categories")
        return jsonify({'success': False, 'message': str(e)}), 500

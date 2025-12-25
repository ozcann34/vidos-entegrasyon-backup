from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
import logging
from datetime import datetime

# Services import
from app.services.pazarama_service import get_pazarama_client
from app.services.n11_service import get_n11_client
from app.services.trendyol_service import get_trendyol_client

api_returns_bp = Blueprint('api_returns', __name__)
logger = logging.getLogger(__name__)

@api_returns_bp.route('/api/returns', methods=['GET'])
@login_required
def get_returns():
    marketplace = request.args.get('marketplace', 'all')
    user_id = current_user.id
    returns = []
    
    # --- PAZARAMA ---
    if marketplace in ['all', 'pazarama']:
        try:
            client = get_pazarama_client(user_id=user_id)
            if client:
                resp = client.get_returns(page=1, size=50)
                data = resp.get('data', {})
                content = data.get('refundList', [])
                for item in content:
                    returns.append({
                        'id': item.get('refundId'),
                        'marketplace': 'pazarama',
                        'order_number': str(item.get('orderNumber')),
                        'product_name': item.get('productName'),
                        'customer_name': item.get('customerName'),
                        'quantity': 1,
                        'amount': item.get('refundAmount', {}).get('value', 0),
                        'status': item.get('refundStatusName'),
                        'status_code': item.get('refundStatus'),
                        'date': item.get('refundDate'),
                        'reason': item.get('refundType'),
                        'image_url': None
                    })
        except Exception as e:
            logger.error(f"Pazarama fetch returns error: {e}")

    # --- TRENDYOL ---
    if marketplace in ['all', 'trendyol']:
        try:
            client = get_trendyol_client(user_id=user_id)
            if client:
                page = request.args.get('page', 0, type=int)
                resp = client.get_claims(page=page, size=50)
                content = resp.get('content', [])
                for item in content:
                    returns.append({
                        'id': item.get('id'),
                        'marketplace': 'trendyol',
                        'order_number': item.get('orderNumber'),
                        'product_name': item.get('productName') or (item.get('items', [{}])[0].get('orderLine', {}).get('productName') if item.get('items') else None),
                        'customer_name': item.get('customerFirstName', '') + ' ' + (item.get('customerLastName') or ''),
                        'quantity': item.get('quantity') or 1,
                        'amount': item.get('claimPrice'),
                        'status': item.get('claimStatus'),
                        'date': item.get('creationDate'),
                        'reason': item.get('reasonName'),
                        'image_url': item.get('imageUrl')
                    })
        except Exception as e:
            logger.error(f"Trendyol fetch returns error: {e}")

    # --- N11 ---
    if marketplace in ['all', 'n11']:
        try:
            client = get_n11_client(user_id=user_id)
            if client:
                res = client.get_claims(page=0, size=50)
                content = res.get('claims', [])
                for item in content:
                    returns.append({
                        'id': item.get('id'),
                        'marketplace': 'n11',
                        'order_number': item.get('orderNumber'),
                        'product_name': item.get('productName'),
                        'customer_name': item.get('customerName'),
                        'quantity': item.get('quantity'),
                        'amount': item.get('amount'),
                        'status': item.get('status'),
                        'date': item.get('date'),
                        'reason': item.get('reason'),
                        'image_url': None
                    })
        except Exception as e:
            logger.error(f"N11 fetch claims error: {e}")

    # --- HEPSIBURADA ---
    if marketplace in ['all', 'hepsiburada']:
        try:
            from app.services.hepsiburada_service import get_hepsiburada_client
            client = get_hepsiburada_client(user_id=user_id)
            if client:
                res = client.get_claims(status='NewRequest')
                for item in res:
                    returns.append({
                        'id': item.get('claimNumber'),
                        'marketplace': 'hepsiburada',
                        'order_number': item.get('orderNumber'),
                        'product_name': f"{item.get('sku')} ({item.get('claimType', '')})",
                        'customer_name': item.get('customerName') or 'Müşteri',
                        'quantity': item.get('quantity') or 1,
                        'amount': item.get('totalPriceAmount', 0),
                        'status': item.get('status'),
                        'date': item.get('claimDate'),
                        'reason': item.get('explanation'),
                        'image_url': None
                    })
        except Exception as e:
            logger.error(f"Hepsiburada fetch claims error: {e}")

    # Sort descending
    try:
        returns.sort(key=lambda x: str(x.get('date') or ''), reverse=True)
    except:
        pass
        
    return jsonify({
        'success': True,
        'data': returns
    })

@api_returns_bp.route('/api/returns/approve', methods=['POST'])
@login_required
def approve_return():
    data = request.json
    r_id = data.get('id')
    marketplace = data.get('marketplace')
    
    if not r_id or not marketplace:
        return jsonify({'success': False, 'message': 'Eksik bilgi.'})
        
    user_id = current_user.id
    try:
        if marketplace == 'pazarama':
            client = get_pazarama_client(user_id=user_id)
            res = client.update_return(r_id, status=2)
            return jsonify({'success': True, 'response': res})
        elif marketplace == 'trendyol':
            client = get_trendyol_client(user_id=user_id)
            res = client.accept_claim(r_id)
            return jsonify({'success': True, 'response': res})
        elif marketplace == 'n11':
            client = get_n11_client(user_id=user_id)
            res = client.approve_claim(r_id)
            return jsonify({'success': True, 'response': res})
        elif marketplace == 'hepsiburada':
            from app.services.hepsiburada_service import get_hepsiburada_client
            client = get_hepsiburada_client(user_id=user_id)
            res = client.approve_claim(r_id)
            return jsonify({'success': True, 'response': res})
            
        return jsonify({'success': False, 'message': 'Geçersiz pazaryeri.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

@api_returns_bp.route('/api/returns/reject', methods=['POST'])
@login_required
def reject_return():
    data = request.json
    r_id = data.get('id')
    marketplace = data.get('marketplace')
    reason_id = data.get('reason_id')
    reason_text = data.get('reason_text')
    
    if not r_id or not marketplace:
        return jsonify({'success': False, 'message': 'Eksik bilgi.'})
        
    user_id = current_user.id
    try:
        if marketplace == 'pazarama':
            client = get_pazarama_client(user_id=user_id)
            res = client.update_return(r_id, status=3, reject_type=int(reason_id))
            return jsonify({'success': True, 'response': res})
        elif marketplace == 'trendyol':
            client = get_trendyol_client(user_id=user_id)
            res = client.reject_claim(r_id, int(reason_id), reason_text)
            return jsonify({'success': True, 'response': res})
        elif marketplace == 'n11':
            client = get_n11_client(user_id=user_id)
            res = client.reject_claim(r_id, reason_id, reason_text)
            return jsonify({'success': True, 'response': res})
        elif marketplace == 'hepsiburada':
            from app.services.hepsiburada_service import get_hepsiburada_client
            client = get_hepsiburada_client(user_id=user_id)
            res = client.reject_claim(r_id, reason_id, reason_text)
            return jsonify({'success': True, 'response': res})
            
        return jsonify({'success': False, 'message': 'Geçersiz pazaryeri.'})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})

from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
import logging
from datetime import datetime

# Services import
from app.services.pazarama_service import get_pazarama_client
from app.services.n11_service import get_n11_client
from app.services.trendyol_service import get_trendyol_client

api_questions_bp = Blueprint('api_questions', __name__)
logger = logging.getLogger(__name__)

@api_questions_bp.route('/api/questions', methods=['GET'])
@login_required
def get_questions():
    marketplace = request.args.get('marketplace', 'all')
    user_id = current_user.id
    
    questions = []
    
    # --- PAZARAMA ---
    if marketplace in ['all', 'pazarama']:
        try:
            client = get_pazarama_client(user_id=user_id)
            if client:
                # API expects page/size
                resp = client.get_product_questions(page=1, size=50) 
                
                # Check response structure
                content = resp.get('items') or resp.get('content') or resp.get('data') or []
                
                for item in content:
                    # Map Pazarama to unified format
                    # Structure usually: { questionId, questionText, createDate, customerName, isAnswered, productName, productImageUrl, ... }
                    q_text = item.get('questionText') or item.get('text')
                    # If answered usually not shown in "waiting" list, but let's check flag
                    is_answered = item.get('isAnswered') or (item.get('answerText') is not None)
                    
                    if not is_answered: # Only show unanswered usually? Or all? Let's show all but sort/filter in UI
                        pass
                        
                    questions.append({
                        'id': item.get('questionId') or item.get('id'),
                        'marketplace': 'pazarama',
                        'product_name': item.get('productName') or 'Ürün Bilgisi Yok',
                        'image_url': item.get('imageUrl') or item.get('productImageUrl'),
                        'text': q_text,
                        'date': item.get('createdDate') or item.get('createDate'),
                        'username': item.get('customerName') or item.get('userName'),
                        'answered': is_answered
                    })
        except Exception as e:
            logger.error(f"Pazarama fetch questions error: {e}")

    # --- TRENDYOL ---
    if marketplace in ['all', 'trendyol']:
        try:
            client = get_trendyol_client(user_id=user_id)
            if client:
                # get_questions(status='WAITING_FOR_ANSWER') usually
                resp = client.get_questions(page=0, size=50, status='WAITING_FOR_ANSWER')
                content = resp.get('content', [])
                
                for item in content:
                     questions.append({
                        'id': str(item.get('id')),
                        'marketplace': 'trendyol',
                        'product_name': item.get('productName'),
                        'image_url': item.get('imageUrl'),
                        'text': item.get('text'),
                        'date': item.get('creationDate'),
                        'username': item.get('userName') or (item.get('userFirstName') + ' ' + item.get('userLastName') if item.get('userFirstName') else None),
                        'answered': item.get('status') == 'ANSWERED'
                    })
        except Exception as e:
            logger.error(f"Trendyol fetch questions error: {e}")

    # --- N11 ---
    if marketplace in ['all', 'n11']:
        try:
            client = get_n11_client(user_id=user_id)
            if client:
                res = client.get_questions(page=0, size=50) # Assuming newly added method
                content = res.get('questions', [])
                
                for item in content:
                    # N11 XML parsed items
                    # We usually want UNANSWERED ones.
                    answered = item.get('answered')
                    
                    questions.append({
                        'id': item.get('id'),
                        'marketplace': 'n11',
                        'product_name': item.get('product', {}).get('title'),
                        'image_url': None, # N11 question list might not return image URL directly
                        'text': item.get('text'),
                        'date': item.get('createdDate'),
                        'username': item.get('user'),
                        'answered': answered
                    })
        except Exception as e:
            logger.error(f"N11 fetch questions error: {e}")

    # Sort by date descending
    try:
        # Filter out invalid dates
        questions.sort(key=lambda x: str(x.get('date') or '0'), reverse=True)
    except Exception as se:
        logger.warning(f"Sort questions error: {se}")

    return jsonify({'success': True, 'data': questions})


@api_questions_bp.route('/api/questions/answer', methods=['POST'])
@login_required
def answer_question():
    data = request.json
    q_id = data.get('id')
    marketplace = data.get('marketplace')
    answer = data.get('answer')
    
    if not all([q_id, marketplace, answer]):
        return jsonify({'success': False, 'message': 'Eksik bilgi.'})
        
    user_id = current_user.id
    
    try:
        if marketplace == 'pazarama':
            client = get_pazarama_client(user_id=user_id)
            if not client: raise Exception("Pazarama API bilgileri eksik")
            res = client.answer_product_question(q_id, answer)
            return jsonify({'success': True, 'response': res})
            
        elif marketplace == 'trendyol':
            client = get_trendyol_client(user_id=user_id)
            if not client: raise Exception("Trendyol API bilgileri eksik")
            res = client.answer_question(int(q_id), answer)
            return jsonify({'success': True, 'response': res})
            
        elif marketplace == 'n11':
            client = get_n11_client(user_id=user_id)
            if not client: raise Exception("N11 API bilgileri eksik")
            res = client.answer_question(q_id, answer)
            if res.get('success'):
                return jsonify({'success': True})
            return jsonify({'success': False, 'message': str(res.get('error') or res.get('raw'))})
            
    except Exception as e:
        logger.error(f"Answer error {marketplace}: {e}")
        return jsonify({'success': False, 'message': str(e)})

    return jsonify({'success': False, 'message': 'Geçersiz pazaryeri.'})

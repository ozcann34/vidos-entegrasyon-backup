from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_required, current_user
from app.services.payment_service import (
    get_plan_details,
    create_payment,
    complete_payment,
    get_payment_gateway
)
from app.models.payment import Payment

payment_bp = Blueprint('payment', __name__, url_prefix='/payment')

@payment_bp.route('/')
@login_required
def payment_page():
    """Ödeme sayfasını gösterir."""
    plan = request.args.get('plan', 'basic')
    plan_details = get_plan_details(plan)
    
    if not plan_details:
        flash('Geçersiz paket seçimi.', 'warning')
        return redirect(url_for('auth.landing'))
        
    return render_template('payment.html', plan=plan, plan_details=plan_details)

@payment_bp.route('/initiate', methods=['POST'])
@login_required
def initiate_payment():
    """Formdan gelen verilerle ödemeyi başlatır."""
    plan = request.form.get('plan')
    billing_period = request.form.get('billing_period', 'monthly')
    
    # 1. Ödeme Kaydı Oluştur (Fiyat sunucuda hesaplanır)
    payment = create_payment(
        user_id=current_user.id,
        plan=plan,
        billing_cycle=billing_period,
        ip_address=request.remote_addr,
        user_agent=request.headers.get('User-Agent')
    )
    
    if not payment:
        flash('Ödeme kaydı oluşturulurken bir hata oluştu.', 'danger')
        return redirect(url_for('payment.payment_page', plan=plan))
        
    # 2. Shopier Adaptörünü Çağır (V2 - Base64 JSON Yöntemi)
    adapter = get_payment_gateway('shopier')
    result = adapter.initiate_payment_v2(payment)
    
    # 3. Sonuç Başarılıysa Yönlendirme Sayfasını Render Et
    if result.get('success'):
        return render_template(
            'shopier_redirect.html',
            post_url=result['post_url'],
            params=result['params']
        )
    else:
        # Hata durumunda (API Key eksik vb.)
        flash(result.get('message', 'Ödeme başlatılamadı.'), 'danger')
        return redirect(url_for('payment.payment_page', plan=plan))

@payment_bp.route('/callback', methods=['POST'])
def payment_callback():
    """Shopier'den gelen sonucu işler."""
    data = request.form.to_dict()
    
    # Shopier platform_order_id parametresini bizim payment_reference ile eşleştirir
    # Önemli: payment_service.py içinde platform_order_id = f"VID_{payment.payment_reference}" yapılmıştı.
    platform_order_id = data.get('platform_order_id', '')
    
    # Debug logging
    print(f"DEBUG: Shopier Callback received for Order ID: {platform_order_id}")
    
    payment_ref = platform_order_id
    if platform_order_id.startswith('VID_'):
        payment_ref = platform_order_id.replace('VID_', '')
        
    payment = Payment.query.filter_by(payment_reference=payment_ref).first()
    
    if not payment:
        print(f"ERROR: Payment not found for reference: {payment_ref}")
        return "Ödeme bulunamadı", 404
        
    adapter = get_payment_gateway('shopier')
    
    # İmzayı doğrula
    if adapter.verify_callback(data):
        status = data.get('status', '').lower()
        if status == 'success':
            complete_payment(payment.id, data.get('payment_id'), 'shopier')
            flash('Ödemeniz başarıyla alındı! Teşekkürler.', 'success')
            return redirect(url_for('main.dashboard'))
        else:
            flash('Ödeme başarısız oldu veya iptal edildi.', 'danger')
            return redirect(url_for('payment.payment_page'))
    else:
        return "Güvenlik doğrulaması başarısız (Invalid Signature)", 403

@payment_bp.route('/success')
@login_required
def success():
    return render_template('payment_success.html')

@payment_bp.route('/cancel')
def cancel():
    flash('Ödeme işlemi iptal edildi.', 'info')
    return redirect(url_for('auth.landing'))

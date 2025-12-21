"""Payment routes for handling subscription payments."""
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_required, current_user
from app.services.payment_service import (
    get_plan_details,
    create_payment,
    complete_payment,
    get_payment_gateway,
    SUBSCRIPTION_PLANS
)
from app.models.payment import Payment
from app import db

payment_bp = Blueprint('payment', __name__, url_prefix='/payment')


@payment_bp.route('/')
@login_required
def payment_page():
    """Display payment page."""
    # Get selected plan and cycle from query parameters
    plan = request.args.get('plan', 'basic')
    cycle = request.args.get('cycle', 'monthly')
    
    # Validate plan
    plan_details = get_plan_details(plan, cycle)
    
    if not plan_details:
        flash('Geçersiz plan seçimi.', 'danger')
        return redirect(url_for('main.landing'))
    
    return render_template('payment.html', plan=plan, cycle=cycle, plan_details=plan_details)


@payment_bp.route('/initiate', methods=['POST'])
@login_required
def initiate_payment():
    """Ödeme işlemini başlat"""
    from flask import current_app
    plan = request.form.get('plan')
    cycle = request.form.get('cycle', 'monthly') # monthly or annual
    marketplaces = request.form.getlist('marketplaces')
    gateway_name = 'iyzico' # Force Iyzico as requested
    
    if not plan:
        flash('Lütfen bir plan seçin.', 'warning')
        return redirect(url_for('main.landing'))
    
    # Plan detaylarını al
    plan_details = get_plan_details(plan, cycle)
    
    if not plan_details:
        flash('Geçersiz plan seçimi.', 'danger')
        return redirect(url_for('payment.payment_page'))
    
    # Ödeme kaydı oluştur
    payment = create_payment(
        user_id=current_user.id,
        plan=plan,
        cycle=cycle,
        ip_address=request.remote_addr,
        user_agent=request.headers.get('User-Agent'),
        metadata={'marketplaces': marketplaces}
    )
    
    if not payment:
        flash('Ödeme oluşturulamadı. Lütfen tekrar deneyin.', 'danger')
        return redirect(url_for('payment.payment_page'))
    
    # Gateway adaptörünü al
    gateway = get_payment_gateway(gateway_name)
    
    # Test Modu Check (Geliştirme için)
    if request.form.get('test_mode') == '1' and current_app.config.get('DEBUG'):
        from app.services.subscription_service import activate_subscription
        complete_payment(payment.id, transaction_id='TEST-' + payment.payment_reference, gateway='mock')
        flash(f'{plan_details["name"]} ({cycle}) başarıyla aktifleştirildi!', 'success')
        return redirect(url_for('main.dashboard'))

    # Iyzico ödemeyi başlat
    result = gateway.initiate_payment(payment)
    
    if result.get('success'):
        return render_template('iyzico_checkout.html', 
                               checkout_content=result.get('checkout_content'),
                               payment=payment,
                               plan_details=plan_details)
    else:
        flash(result.get('message', 'Ödeme başlatılamadı.'), 'danger')
        return redirect(url_for('payment.payment_page', plan=plan))


@payment_bp.route('/callback', methods=['POST'])
def callback():
    """Iyzico ödeme callback"""
    token = request.form.get('token')
    
    if not token:
        flash('Geçersiz ödeme bildirimi.', 'danger')
        return redirect(url_for('main.landing'))
        
    gateway = get_payment_gateway('iyzico')
    result = gateway.verify_callback(token)
    
    if result.get('success'):
        # Payment reference logic
        from app.models.payment import Payment
        # Result reference might contain common parts
        ref = result.get('reference')
        payment = Payment.query.filter_by(payment_reference=ref).first()
        
        if payment:
            # Get cycle from note
            cycle = 'monthly'
            if payment.note and 'annual' in payment.note:
                cycle = 'annual'
                
            from app.services.subscription_service import activate_subscription
            complete_payment(payment.id, transaction_id=result.get('payment_id'), gateway='iyzico')
            
            flash('Ödemeniz başarıyla tamamlandı! Aboneliğiniz aktifleştirildi.', 'success')
            return redirect(url_for('main.dashboard'))
    
    flash(result.get('message', 'Ödeme doğrulanırken bir hata oluştu.'), 'danger')
    return redirect(url_for('main.dashboard'))


@payment_bp.route('/success')
@login_required
def success():
    """Ödeme başarılı sayfası."""
    return render_template('payment_success.html')


@payment_bp.route('/cancel')
@login_required
def cancel():
    """Ödeme iptal edildi."""
    flash('Ödeme işlemi iptal edildi.', 'warning')
    return redirect(url_for('main.dashboard'))

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
    # Get selected plan from session or query parameter
    plan = session.get('selected_plan') or request.args.get('plan', 'basic')
    
    try:
        # Validate plan
        plan_details = get_plan_details(plan)
        
        if not plan_details:
            flash('Geçersiz plan seçimi.', 'danger')
            return redirect(url_for('auth.landing'))
        
        return render_template('payment.html', plan=plan, plan_details=plan_details)
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"Payment Page Error: {str(e)}")
        try:
            import os
            from datetime import datetime
            with open('payment_error.log', 'a') as f:
                f.write(f"\\n[{datetime.utcnow()}] Error in payment_page:\\n{error_details}\\n")
        except: pass
        flash(f'Ödeme sayfası yüklenirken bir hata oluştu: {str(e)}', 'danger')
        return redirect(url_for('auth.landing'))


@payment_bp.route('/initiate', methods=['POST'])
@login_required
def initiate_payment():
    """Initiate payment process."""
    print(f"DEBUG: initiate_payment called by user {current_user.id}")
    print(f"DEBUG: Form data: {request.form}")
    
    plan = request.form.get('plan', 'basic')
    billing_cycle = request.form.get('billing_period', 'monthly')
    gateway = 'shopier' # Force shopier for debugging
    
    print(f"DEBUG: Plan: {plan}, Cycle: {billing_cycle}, Gateway: {gateway}")
    
    # Validate plan
    plan_details = get_plan_details(plan)
    
    if not plan_details:
        flash('Geçersiz plan seçimi.', 'danger')
        return redirect(url_for('payment.payment_page'))
    
    # Create payment record
    payment = create_payment(
        user_id=current_user.id,
        plan=plan,
        billing_cycle=billing_cycle,
        ip_address=request.remote_addr,
        user_agent=request.headers.get('User-Agent')
    )
    
    if not payment:
        flash('Ödeme oluşturulamadı. Lütfen tekrar deneyin.', 'danger')
        return redirect(url_for('payment.payment_page'))
    
    # Integrate with actual payment gateway
    gw_adapter = get_payment_gateway(gateway)
    
    # Dynamic callback URL
    callback_url = url_for('payment.payment_callback', _external=True)
    
    init_res = gw_adapter.initiate_payment(payment, callback_url=callback_url)
    
    if init_res.get('success'):
        if init_res.get('post_url'):
            # Return a simple auto-submit form
            return f"""
            <form id="shopier_form" method="post" action="{init_res['post_url']}">
                {''.join([f'<input type="hidden" name="{k}" value="{v}">' for k, v in init_res['params'].items()])}
            </form>
            <script type="text/javascript">document.getElementById('shopier_form').submit();</script>
            """
        elif init_res.get('redirect_url'):
            return redirect(init_res['redirect_url'])
        else:
            # Auto-complete for mock/test if no URL
            complete_payment(payment.id, transaction_id='TEST-' + payment.payment_reference, gateway=gateway)
            flash(f'{plan_details["name"]} planı başarıyla aktifleştirildi!', 'success')
            return redirect(url_for('main.dashboard'))
    else:
        flash(init_res.get('message', 'Ödeme başlatılamadı.'), 'danger')
        return redirect(url_for('payment.payment_page', plan=plan))


@payment_bp.route('/callback', methods=['GET', 'POST'])
def payment_callback():
    """Handle payment gateway callback."""
    # Shopier sends data via POST usually
    data = request.form.to_dict()
    if not data:
        data = request.args.to_dict()
        
    payment_ref = data.get('platform_order_id')
    
    if not payment_ref:
        flash('Geçersiz ödeme callback.', 'danger')
        return redirect(url_for('auth.landing'))
    
    # Find payment by reference
    payment = Payment.query.filter_by(payment_reference=payment_ref).first()
    
    if not payment:
        flash('Ödeme kaydı bulunamadı.', 'danger')
        return redirect(url_for('auth.landing'))
    
    # Verify callback using gateway adapter
    gw_adapter = get_payment_gateway('shopier')
    if not gw_adapter.verify_callback(data):
        flash('Ödeme doğrulaması başarısız (Geçersiz imza).', 'danger')
        return redirect(url_for('payment.cancel'))

    # Mark as completed
    status = data.get('status', '').lower()
    if status == 'success':
        success = complete_payment(payment.id, transaction_id=data.get('payment_id', 'SHOP-TRX'), gateway='shopier')
        if success:
            flash('Ödemeniz başarıyla tamamlandı! Aboneliğiniz aktifleştirildi.', 'success')
            return redirect(url_for('payment.success'))
    
    flash('Ödeme işlemi tamamlanamadı veya reddedildi.', 'danger')
    return redirect(url_for('payment.cancel'))



@payment_bp.route('/success')
@login_required
def success():
    """Payment success page."""
    return render_template('payment_success.html')


@payment_bp.route('/cancel')
def cancel():
    """Payment cancelled page."""
    flash('Ödeme işlemi iptal edildi.', 'warning')
    return redirect(url_for('auth.landing'))

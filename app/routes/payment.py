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
    
    # Validate plan
    plan_details = get_plan_details(plan)
    
    if not plan_details:
        flash('Geçersiz plan seçimi.', 'danger')
        return redirect(url_for('auth.landing'))
    
    return render_template('payment.html', plan=plan, plan_details=plan_details)


@payment_bp.route('/initiate', methods=['POST'])
@login_required
def initiate_payment():
    """Initiate payment process."""
    plan = request.form.get('plan', 'basic')
    billing_cycle = request.form.get('billing_period', 'monthly')
    gateway = 'shopier' # Sadece shopier kullanılacak
    
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
    init_res = gw_adapter.initiate_payment(payment)
    
    if init_res.get('success'):
        if init_res.get('redirect_url'):
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
    # TODO: Implement actual gateway callback handling
    # This will be called by Shopier/Iyzico after payment
    
    transaction_id = request.args.get('transaction_id') or request.form.get('transaction_id')
    payment_ref = request.args.get('payment_ref') or request.form.get('payment_ref')
    
    if not transaction_id or not payment_ref:
        flash('Geçersiz ödeme callback.', 'danger')
        return redirect(url_for('auth.landing'))
    
    # Find payment by reference (Shopier may send it as platform_order_id)
    payment = Payment.query.filter_by(payment_reference=payment_ref).first()
    
    if not payment:
        flash('Ödeme kaydı bulunamadı.', 'danger')
        return redirect(url_for('auth.landing'))
    
    # Verify callback using gateway adapter
    gw_adapter = get_payment_gateway('shopier')
    if not gw_adapter.verify_callback(request.form):
        flash('Ödeme doğrulaması başarısız (Geçersiz imza).', 'danger')
        return redirect(url_for('payment.cancel'))

    # Mark as completed
    success = complete_payment(payment.id, transaction_id=transaction_id, gateway='shopier')
    
    if success:
        flash('Ödemeniz başarıyla tamamlandı! Aboneliğiniz aktifleştirildi.', 'success')
        return redirect(url_for('payment.success'))
    else:
        flash('Ödeme doğrulanamadı.', 'danger')
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

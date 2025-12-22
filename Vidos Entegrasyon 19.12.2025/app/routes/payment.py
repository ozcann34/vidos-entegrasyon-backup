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
    gateway = request.form.get('gateway', 'shopier')  # shopier or iyzico
    
    # Validate plan
    plan_details = get_plan_details(plan)
    
    if not plan_details:
        flash('Geçersiz plan seçimi.', 'danger')
        return redirect(url_for('payment.payment_page'))
    
    # Create payment record
    payment = create_payment(
        user_id=current_user.id,
        plan=plan,
        ip_address=request.remote_addr,
        user_agent=request.headers.get('User-Agent')
    )
    
    if not payment:
        flash('Ödeme oluşturulamadı. Lütfen tekrar deneyin.', 'danger')
        return redirect(url_for('payment.payment_page'))
    
    # TODO: Integrate with actual payment gateway
    # For now, redirect to placeholder success page
    flash('Ödeme gateway entegrasyonu yakında aktif olacak. Şimdilik test modunda çalışıyoruz.', 'info')
    
    # For development: Auto-complete payment
    if request.form.get('test_mode') == '1':
        complete_payment(payment.id, transaction_id='TEST-' + payment.payment_reference, gateway=gateway)
        flash(f'{plan_details["name"]} planı başarıyla aktifleştirildi!', 'success')
        return redirect(url_for('main.dashboard'))
    
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
    
    # Find payment by reference
    payment = Payment.query.filter_by(payment_reference=payment_ref).first()
    
    if not payment:
        flash('Ödeme kaydı bulunamadı.', 'danger')
        return redirect(url_for('auth.landing'))
    
    # Verify callback (TODO: implement gateway-specific verification)
    # For now, mark as completed
    success = complete_payment(payment.id, transaction_id=transaction_id)
    
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

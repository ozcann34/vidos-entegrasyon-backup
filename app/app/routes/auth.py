"""Authentication routes for login, register, logout, password reset."""
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_user, logout_user, login_required, current_user
from app.services.user_service import authenticate_user, create_user
from app.models import User
from app import db

auth_bp = Blueprint('auth', __name__)



@auth_bp.route('/landing')
def landing():
    """Landing page for the application."""
    # If user is already logged in, redirect to dashboard
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    return render_template('landing.html')


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    """User login page."""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        remember = request.form.get('remember', False)
        
        if not email or not password:
            flash('Email ve şifre gereklidir.', 'danger')
            return render_template('auth/login.html')
        
        user = authenticate_user(email, password)
        
        if user:
            if user.is_banned:
                flash(f'Hesabınız askıya alınmıştır. Sebep: {user.ban_reason or "Belirtilmemiş"}', 'danger')
                return render_template('auth/login.html')
            
            login_user(user, remember=bool(remember))
            flash(f'Hoş geldiniz, {user.full_name or user.email}!', 'success')
            
            # Redirect to next page if exists
            next_page = request.args.get('next')
            if next_page:
                return redirect(next_page)
            return redirect(url_for('main.dashboard'))
        else:
            flash('Geçersiz email veya şifre.', 'danger')
    
    return render_template('auth/login.html')


@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    """User registration page."""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    
    # Get selected plan from URL parameter
    selected_plan = request.args.get('plan', 'basic')
    
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        password_confirm = request.form.get('password_confirm', '')
        first_name = request.form.get('first_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        tc_no = request.form.get('tc_no', '').strip()
        company_title = request.form.get('company_title', '').strip()
        tax_office = request.form.get('tax_office', '').strip()
        tax_no = request.form.get('tax_no', '').strip()
        phone = request.form.get('phone', '').strip()
        city = request.form.get('city', '').strip()
        district = request.form.get('district', '').strip()
        address = request.form.get('address', '').strip()
        plan = request.form.get('plan', 'basic')  # Get plan from form
        
        # Validation
        if not email or not password:
            flash('Email ve şifre gereklidir.', 'danger')
            return render_template('auth/register.html', selected_plan=plan)
        
        if password != password_confirm:
            flash('Şifreler eşleşmiyor.', 'danger')
            return render_template('auth/register.html', selected_plan=plan)
        
        if len(password) < 6:
            flash('Şifre en az 6 karakter olmalıdır.', 'danger')
            return render_template('auth/register.html', selected_plan=plan)
        
        # Create user
        user = create_user(email, password, 
                           first_name=first_name, 
                           last_name=last_name, 
                           tc_no=tc_no,
                           company_title=company_title, 
                           tax_office=tax_office, 
                           tax_no=tax_no, 
                           phone=phone,
                           city=city,
                           district=district,
                           address=address)
        
        if user:
            flash('Hesabınız oluşturuldu!', 'success')
            # Auto login
            login_user(user)
            
            # Store selected plan in session
            session['selected_plan'] = plan
            
            # Redirect to payment if not free plan
            if plan != 'free':
                return redirect(url_for('payment.payment_page', plan=plan))
            else:
                return redirect(url_for('main.dashboard'))
        else:
            flash('Bu email adresi zaten kayıtlı.', 'danger')
    
    # Get plan details for display
    from app.services.payment_service import get_plan_details
    plan_details = get_plan_details(selected_plan)
    
    return render_template('auth/register.html', selected_plan=selected_plan, plan_details=plan_details)


@auth_bp.route('/logout')
@login_required
def logout():
    """Logout user."""
    logout_user()
    flash('Başarıyla çıkış yaptınız.', 'info')
    return redirect(url_for('auth.login'))


@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    """Password reset request page."""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        
        if not email:
            flash('Email adresi gereklidir.', 'danger')
            return render_template('auth/forgot_password.html')
        
        # Find user by email
        user = User.query.filter_by(email=email.lower()).first()
        
        if user:
            from app.services.email_service import send_password_reset_email
            send_password_reset_email(user)
        
        # Always show success message for security (don't reveal if email exists)
        flash('Eğer bu email adresi kayıtlıysa, şifre sıfırlama linki gönderildi.', 'success')
        return redirect(url_for('auth.login'))
    
    return render_template('auth/forgot_password.html')


@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    """Password reset page."""
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))
    
    from app.services.email_service import verify_reset_token, clear_reset_token
    
    # Validate token
    user = verify_reset_token(token)
    
    if not user:
        flash('Geçersiz veya süresi dolmuş şifre sıfırlama linki.', 'danger')
        return redirect(url_for('auth.forgot_password'))
    
    if request.method == 'POST':
        password = request.form.get('password', '')
        password_confirm = request.form.get('password_confirm', '')
        
        if not password:
            flash('Yeni şifre gereklidir.', 'danger')
            return render_template('auth/reset_password.html', token=token)
        
        if password != password_confirm:
            flash('Şifreler eşleşmiyor.', 'danger')
            return render_template('auth/reset_password.html', token=token)
        
        if len(password) < 6:
            flash('Şifre en az 6 karakter olmalıdır.', 'danger')
            return render_template('auth/reset_password.html', token=token)
        
        # Update password
        user.set_password(password)
        clear_reset_token(user)
        db.session.commit()
        
        flash('Şifreniz başarıyla güncellendi! Giriş yapabilirsiniz.', 'success')
        return redirect(url_for('auth.login'))
    
    return render_template('auth/reset_password.html', token=token, email=user.email)


@auth_bp.route('/banned')
def banned():
    """Banned user page."""
    # Get ban info from session
    user_email = session.get('banned_email', 'Bilinmiyor')
    ban_reason = session.get('ban_reason', 'Belirtilmemiş')
    
    # Clear session data
    session.pop('banned_email', None)
    session.pop('ban_reason', None)
    
    return render_template('auth/banned.html', user_email=user_email, ban_reason=ban_reason)

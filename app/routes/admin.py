from datetime import datetime
import logging

from functools import wraps

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, jsonify

from flask_login import login_required, current_user

from app import db

from app.models import User, Subscription, AdminLog, Announcement, UserActivityLog, Payment

from app.services.user_service import ban_user, unban_user, update_subscription, get_all_users



# Admin panel at special path

admin_bp = Blueprint('admin', __name__, url_prefix='/admin-secret-panel')
logger = logging.getLogger(__name__)





def admin_required(f):

    """Decorator to require admin access and localhost."""

    @wraps(f)

    def decorated_function(*args, **kwargs):

        # Check if user is authenticated and admin

        if not current_user.is_authenticated:

            flash('Bu sayfaya erişmek için giriş yapmalısınız.', 'warning')

            return redirect(url_for('auth.login'))

        

        if not current_user.is_admin:
            abort(403)
        
        return f(*args, **kwargs)

    return decorated_function


def super_admin_required(f):
    """Decorator to require super admin access."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login'))
        
        if not current_user.is_super_admin:
            flash('Bu işlem için ana yönetici yetkisi gereklidir.', 'danger')
            return redirect(url_for('admin.dashboard'))
            
        return f(*args, **kwargs)
    return decorated_function





@admin_bp.route('/')

@admin_required

def dashboard():

    """Admin dashboard."""

    from app.models import Product, Order, SupplierXML, BatchLog

    

    # Statistics

    stats = {

        'total_users': User.query.count(),

        'active_users': User.query.filter_by(is_active=True, is_banned=False).count(),

        'banned_users': User.query.filter_by(is_banned=True).count(),

        'total_products': Product.query.count(),

        'total_orders': Order.query.count(),

        'total_xml_sources': SupplierXML.query.count(),

    }

    

    # Recent users

    recent_users = User.query.order_by(User.created_at.desc()).limit(5).all()

    

    # Recent admin logs

    recent_logs = AdminLog.query.order_by(AdminLog.created_at.desc()).limit(10).all()

    

    return render_template('admin/dashboard.html', stats=stats, recent_users=recent_users, recent_logs=recent_logs)





@admin_bp.route('/users')

@admin_required

def users():

    """List all users."""

    page = request.args.get('page', 1, type=int)

    per_page = request.args.get('per_page', 20, type=int)

    search = request.args.get('search', '')

    

    query = User.query

    

    if search:

        query = query.filter(

            db.or_(

                User.email.ilike(f'%{search}%'),

                User.full_name.ilike(f'%{search}%')

            )

        )

    

    users = query.order_by(User.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)

    

    return render_template('admin/users.html', users=users, search=search)





@admin_bp.route('/users/<int:user_id>')

@admin_required

def user_detail(user_id):

    """User detail page."""

    user = User.query.get_or_404(user_id)

    

    # Get user statistics

    from app.models import Product, Order, SupplierXML, BatchLog

    

    stats = {

        'products': Product.query.filter_by(user_id=user_id).count(),

        'orders': Order.query.filter_by(user_id=user_id).count(),

        'xml_sources': SupplierXML.query.filter_by(user_id=user_id).count(),

        'batch_logs': BatchLog.query.filter_by(user_id=user_id).count(),

    }

    

    # Get recent activity

    recent_logs = BatchLog.query.filter_by(user_id=user_id).order_by(BatchLog.id.desc()).limit(10).all()

    

    return render_template('admin/user_detail.html', user=user, stats=stats, recent_logs=recent_logs)


@admin_bp.route('/users/<int:user_id>/update-profile', methods=['POST'])
@admin_required
def update_user_profile(user_id):
    """
    Update normal user profile information (Admin only).
    """
    from app.models import User
    
    user = User.query.get_or_404(user_id)
    
    # Prevent editing super admin by others
    if user.is_super_admin and current_user.id != user.id:
        flash('Ana yönetici hesabı düzenlenemez.', 'danger')
        return redirect(url_for('admin.user_detail', user_id=user_id))
        
    try:
        # Update basics
        user.full_name = request.form.get('full_name')
        user.first_name = request.form.get('first_name')
        user.last_name = request.form.get('last_name')
        user.email = request.form.get('email')
        user.phone = request.form.get('phone')
        
        # Company Info
        user.company_title = request.form.get('company_title')
        user.tc_no = request.form.get('tc_no')
        user.tax_no = request.form.get('tax_no')
        user.tax_office = request.form.get('tax_office')
        
        # Address
        user.city = request.form.get('city')
        user.district = request.form.get('district')
        user.address = request.form.get('address')
        
        db.session.commit()
        
        # Log action
        from app.models import AdminLog
        AdminLog.log_action(current_user.id, 'update_user_profile', details=f"User {user.email} updated")
        
        flash('Kullanıcı bilgileri başarıyla güncellendi.', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f'Güncelleme hatası: {str(e)}', 'danger')
        
    return redirect(url_for('admin.user_detail', user_id=user_id))






@admin_bp.route('/users/<int:user_id>/ban', methods=['POST'])
@super_admin_required
def ban_user_route(user_id):

    """Ban a user."""

    reason = request.form.get('reason', '')

    

    if ban_user(user_id, reason):

        # Log action

        AdminLog.log_action(

            admin_id=current_user.id,

            action='ban',

            target_user_id=user_id,

            details=f'Reason: {reason}',

            ip_address=request.remote_addr

        )

    return redirect(url_for('admin.user_detail', user_id=user_id))








@admin_bp.route('/users/<int:user_id>/verify', methods=['POST'])
@admin_required
def verify_user_route(user_id):
    """Manually verify a user's email."""
    user = User.query.get_or_404(user_id)
    user.is_email_verified = True
    user.email_otp = None
    user.otp_expiry = None
    db.session.commit()
    
    # Log action
    AdminLog.log_action(
        admin_id=current_user.id,
        action='verify_email_manual',
        target_user_id=user_id,
        details=f'User {user.email} manually verified by admin.',
        ip_address=request.remote_addr
    )
    
    flash(f'{user.email} başarıyla doğrulandı.', 'success')
    return redirect(url_for('admin.user_detail', user_id=user_id))

@admin_bp.route('/users/<int:user_id>/unban', methods=['POST'])
@super_admin_required
def unban_user_route(user_id):

    """Unban a user."""

    if unban_user(user_id):

        # Log action

        AdminLog.log_action(

            admin_id=current_user.id,

            action='unban',

            target_user_id=user_id,

            ip_address=request.remote_addr

        )

        flash('Kullanıcı banı kaldırıldı.', 'success')

    else:

        flash('Ban kaldırılamadı.', 'danger')

    

    return redirect(url_for('admin.user_detail', user_id=user_id))





@admin_bp.route('/users/<int:user_id>/delete', methods=['POST'])
@super_admin_required
def delete_user_route(user_id):

    """Delete a user and all their data."""

    user = User.query.get_or_404(user_id)

    

    # Prevent self-deletion

    if user.id == current_user.id:

        flash('Kendi hesabınızı silemezsiniz.', 'danger')

        return redirect(url_for('admin.user_detail', user_id=user_id))

    

    # Store user info for logging before deletion

    user_email = user.email

    user_name = user.full_name or user_email

    

    try:

        # Delete related data
        from app.models import Product, Order, SupplierXML, BatchLog, Payment, MarketplaceProduct, Notification, UserActivityLog, SupportTicket, Expense, AdminLog, Setting, ExcelFile, Blacklist, AutoSync, PushSubscription

        # 1. Product & Catalog Data
        MarketplaceProduct.query.filter_by(user_id=user_id).delete()
        Product.query.filter_by(user_id=user_id).delete()
        SupplierXML.query.filter_by(user_id=user_id).delete()
        ExcelFile.query.filter_by(user_id=user_id).delete()
        Blacklist.query.filter_by(user_id=user_id).delete()
        
        # 2. Operational Data
        Order.query.filter_by(user_id=user_id).delete()
        BatchLog.query.filter_by(user_id=user_id).delete()
        Notification.query.filter_by(user_id=user_id).delete()
        PushSubscription.query.filter_by(user_id=user_id).delete()
        UserActivityLog.query.filter_by(user_id=user_id).delete()
        Expense.query.filter_by(user_id=user_id).delete()
        AutoSync.query.filter_by(user_id=user_id).delete()
        
        # 3. Settings (user-specific)
        Setting.query.filter_by(user_id=user_id).delete()
        
        # Clean up Admin Logs where user was the actor
        AdminLog.query.filter_by(admin_id=user_id).delete()

        # 4. Financial & Account Data
        Payment.query.filter_by(user_id=user_id).delete()
        Subscription.query.filter_by(user_id=user_id).delete()
        SupportTicket.query.filter_by(user_id=user_id).delete()

        

        # Delete user

        db.session.delete(user)

        

        # Log action before committing

        AdminLog.log_action(

            admin_id=current_user.id,

            action='delete_user',

            target_user_id=user_id,

            details=f'Deleted user: {user_name} ({user_email})',

            ip_address=request.remote_addr

        )

        

        db.session.commit()

        flash(f'Kullanıcı "{user_name}" ve tüm verileri başarıyla silindi.', 'success')

    except Exception as e:

        db.session.rollback()

        flash(f'Kullanıcı silinirken hata oluştu: {str(e)}', 'danger')

        return redirect(url_for('admin.user_detail', user_id=user_id))

    

    return redirect(url_for('admin.users'))







@admin_bp.route('/users/<int:user_id>/toggle_admin', methods=['POST'])
@super_admin_required
def toggle_admin(user_id):
    """Toggle admin status for a user."""
    # Prevent self-demotion
    if user_id == current_user.id:
        flash('Kendi yöneticilik yetkinizi alamazsınız.', 'warning')
        return redirect(url_for('admin.users'))
        
    user = User.query.get_or_404(user_id)
    user.is_admin = not user.is_admin
    
    # Log action
    action = 'promote_admin' if user.is_admin else 'demote_admin'
    AdminLog.log_action(
        admin_id=current_user.id,
        action=action,
        target_user_id=user_id,
        details=f'Admin status changed to {user.is_admin}',
        ip_address=request.remote_addr
    )
    
    db.session.commit()
    
    status_msg = "Yönetici yapıldı" if user.is_admin else "Yöneticilik yetkisi alındı"
    flash(f'{user.full_name or user.email} kullanıcısı {status_msg}.', 'success')
    return redirect(url_for('admin.users'))


@admin_bp.route('/users/create_admin', methods=['GET', 'POST'])
@super_admin_required
def create_admin_view():
    """Create a new user or admin with full details."""
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        full_name = request.form.get('full_name', '').strip()
        is_admin_check = request.form.get('is_admin') == 'on'
        
        if not email or not password:
            flash('Email ve şifre zorunludur.', 'danger')
            return render_template('admin/create_admin.html')
            
        # Check if user exists
        if User.query.filter_by(email=email).first():
            flash('Bu email adresi zaten kayıtlı.', 'warning')
            return render_template('admin/create_admin.html')
            
        try:
            # Prepare user data
            user_data = {
                'first_name': full_name.split(' ')[0] if ' ' in full_name else full_name,
                'last_name': ' '.join(full_name.split(' ')[1:]) if ' ' in full_name else '',
                'company_title': request.form.get('company_title'),
                'tax_no': request.form.get('tax_no'),
                'tax_office': request.form.get('tax_office'),
                'phone': request.form.get('phone'),
                'city': request.form.get('city'),
                'district': request.form.get('district'),
                'address': request.form.get('address'),
                'is_admin': is_admin_check,
                'plan': 'enterprise' if is_admin_check else 'free'
            }
            
            from app.services.user_service import create_user
            user = create_user(email, password, **user_data)
            
            if user:
                AdminLog.log_action(
                    admin_id=current_user.id,
                    action='create_user_admin',
                    target_user_id=user.id,
                    details=f'Created new user (Admin: {is_admin_check}): {email}',
                    ip_address=request.remote_addr
                )
                
                flash(f'{"Yönetici" if is_admin_check else "Kullanıcı"} başarıyla oluşturuldu.', 'success')
                return redirect(url_for('admin.users'))
            else:
                flash('Kullanıcı oluşturulamadı.', 'danger')
            
        except Exception as e:
            db.session.rollback()
            flash(f'Sistem Hatası: {str(e)}', 'danger')
            
    return render_template('admin/create_admin.html')


@admin_bp.route('/users/<int:user_id>/subscription', methods=['POST'])
@super_admin_required
def update_subscription_route(user_id):

    """Update user subscription."""

    plan = request.form.get('plan', 'free')

    

    # Parse end date if provided

    end_date_str = request.form.get('end_date')

    end_date = None

    if end_date_str:

        from datetime import datetime

        try:

            end_date = datetime.strptime(end_date_str, '%Y-%m-%d')

        except ValueError:

            pass

    

    # Parse overrides
    max_products = request.form.get('max_products')
    max_xml_sources = request.form.get('max_xml_sources')
    max_marketplaces = request.form.get('max_marketplaces')
    
    # Convert empty strings to None, otherwise int
    try:
        max_products = int(max_products) if max_products and max_products.strip() != '' else None
    except ValueError:
        max_products = None
        
    try:
        max_xml_sources = int(max_xml_sources) if max_xml_sources and max_xml_sources.strip() != '' else None
    except ValueError:
        max_xml_sources = None

    try:
        max_marketplaces = int(max_marketplaces) if max_marketplaces and max_marketplaces.strip() != '' else None
    except ValueError:
        max_marketplaces = None

    if update_subscription(user_id, plan, end_date, max_products=max_products, max_xml_sources=max_xml_sources, max_marketplaces=max_marketplaces):

        # Log action

        AdminLog.log_action(

            admin_id=current_user.id,

            action='subscription_update',

            target_user_id=user_id,

            details=f'Plan: {plan}, End: {end_date_str or "Unlimited"}',

            ip_address=request.remote_addr

        )

        flash('Abonelik güncellendi.', 'success')

    else:
        flash('Abonelik güncellenemedi.', 'danger')

    return redirect(url_for('admin.user_detail', user_id=user_id))


@admin_bp.route('/logs')
@admin_required
def logs():
    """View admin action logs with filtering."""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    
    # Filters
    action_filter = request.args.get('action')
    admin_id_filter = request.args.get('admin_id', type=int)
    
    query = AdminLog.query
    
    if action_filter:
        query = query.filter(AdminLog.action == action_filter)
        
    if admin_id_filter:
        query = query.filter(AdminLog.admin_id == admin_id_filter)
    
    logs = query.order_by(AdminLog.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    
    # Get distinct actions for filter dropdown
    actions = db.session.query(AdminLog.action).distinct().all()
    actions = [a[0] for a in actions]
    
    # Get all admins for filter dropdown
    admins = User.query.filter_by(is_admin=True).all()
    
    return render_template('admin/logs.html', logs=logs, actions=actions, admins=admins, 
                           current_action=action_filter, current_admin_id=admin_id_filter)


@admin_bp.route('/subscriptions')
@admin_required
def subscriptions():
    """View all subscriptions."""
    page = request.args.get('page', 1, type=int)

    per_page = request.args.get('per_page', 20, type=int)

    plan_filter = request.args.get('plan', '')

    

    query = Subscription.query

    

    if plan_filter:

        query = query.filter_by(plan=plan_filter)

    

    subscriptions = query.order_by(Subscription.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)

    

    return render_template('admin/subscriptions.html', subscriptions=subscriptions, plan_filter=plan_filter)


@admin_bp.route('/payments')
@admin_required
def payments():
    """View payment history (Restricted)."""
    # Access allowed for all admins (Support Team included)
        
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    
    payments = Payment.query.order_by(Payment.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    
    return render_template('admin/payments.html', payments=payments)


@admin_bp.route('/subscriptions/<int:sub_id>/approve', methods=['POST'])
@admin_required
def approve_subscription_route(sub_id):
    """Approve a pending subscription."""
    # Strict Access Control
    if current_user.email != 'bugraerkaradeniz34@gmail.com':
        flash('Bu işlem için yetkiniz yok.', 'danger')
        return redirect(url_for('admin.dashboard'))
        
    subscription = Subscription.query.get_or_404(sub_id)
    subscription.is_approved = True
    db.session.commit()
    
    # Log action
    AdminLog.log_action(
        admin_id=current_user.id,
        action='approve_subscription',
        target_user_id=subscription.user_id,
        details=f'Subscription ID {sub_id} approved. Plan: {subscription.plan}',
        ip_address=request.remote_addr
    )
    
    flash(f'Abonelik başarıyla onaylandı. Kullanıcı artık sisteme erişebilir.', 'success')
    return redirect(request.referrer or url_for('admin.payments'))


@admin_bp.route('/global-settings', methods=['GET', 'POST'])
@super_admin_required
def global_settings():
    """Manage global system-wide settings (Kill-Switches)."""
    from app.models.settings import Setting
    
    # Define features that can be globally disabled
    features = {
        'assistant': 'Vidos Asistan',
        'trendyol': 'Trendyol Entegrasyonu',
        'pazarama': 'Pazarama Entegrasyonu',
        'hepsiburada': 'Hepsiburada Entegrasyonu',
        'idefix': 'İdefix Entegrasyonu',
        'n11': 'N11 Entegrasyonu',
        'order_sync': 'Sipariş Senkronizasyonu',
        'stock_update': 'Stok Güncelleme',
        'price_update': 'Fiyat Güncelleme'
    }
    
    if request.method == 'POST':
        # Save feature toggles
        for key in features.keys():
            value = 'true' if request.form.get(f'global_{key}_enabled') == 'on' else 'false'
            Setting.set(f'global_{key}_enabled', value, user_id=None)
        
        # Save Shopier API settings (super admin only)
        shopier_key = request.form.get('shopier_api_key', '').strip()
        shopier_secret = request.form.get('shopier_api_secret', '').strip()
        shopier_callback = request.form.get('shopier_callback_url', '').strip()
        shopier_index = request.form.get('shopier_website_index', '1').strip()
        
        if shopier_key:
            Setting.set('SHOPIER_API_KEY', shopier_key, user_id=None)
        if shopier_secret:
            Setting.set('SHOPIER_API_SECRET', shopier_secret, user_id=None)
        if shopier_callback:
            Setting.set('SHOPIER_CALLBACK_URL', shopier_callback, user_id=None)
        if shopier_index:
            Setting.set('SHOPIER_WEBSITE_INDEX', shopier_index, user_id=None)
            
        flash('Küresel ayarlar başarıyla güncellendi.', 'success')
        return redirect(url_for('admin.global_settings'))
    
    # Get current values
    current_values = {}
    for key in features.keys():
        current_values[key] = Setting.get(f'global_{key}_enabled', 'true', user_id=None) == 'true'
    
    # Get Shopier settings
    shopier_settings = {
        'api_key': Setting.get('SHOPIER_API_KEY', '', user_id=None),
        'api_secret': Setting.get('SHOPIER_API_SECRET', '', user_id=None),
        'callback_url': Setting.get('SHOPIER_CALLBACK_URL', '', user_id=None),
        'website_index': Setting.get('SHOPIER_WEBSITE_INDEX', '1', user_id=None)
    }
        
    return render_template('admin/global_settings.html', 
                         features=features, 
                         current_values=current_values,
                         shopier_settings=shopier_settings)





@admin_bp.route('/users/<int:user_id>/permissions', methods=['GET', 'POST'])

@admin_required

def user_permissions(user_id):

    """Manage user permissions."""

    user = User.query.get_or_404(user_id)

    

    # Available permission pages with Turkish labels

    available_permissions = {

        'dashboard': 'Kontrol Paneli',

        'xml_products': 'XML Ürünleri',

        'excel_products': 'Excel Ürünleri',

        'trendyol': 'Trendyol',

        'pazarama': 'Pazarama',

        'hepsiburada': 'Hepsiburada',

        'idefix': 'İdefix',

        'batch_logs': 'Batch Logları',

        'orders': 'Siparişler',

        'auto_sync': 'Otomatik Senkronizasyon',

        'settings': 'Ayarlar',

        'n11': 'N11',
        
        'assistant': 'Vidos Asistanı',
        
        'documentation': 'Kullanım Kılavuzu',
        
        'faq': 'S.S.S.',
        
        'support_tickets': 'Destek Talepleri',

    }

    

    if request.method == 'POST':

        # Get submitted permissions

        new_permissions = {}

        for perm_key in available_permissions.keys():

            new_permissions[perm_key] = request.form.get(perm_key) == 'on'

        

        # Update user permissions

        user.permissions = new_permissions

        db.session.commit()

        

        # Log action

        AdminLog.log_action(

            admin_id=current_user.id,

            action='permissions_update',

            target_user_id=user_id,

            details=f'Restricted: {user.get_restricted_pages()}',

            ip_address=request.remote_addr

        )

        

        flash('Kullanıcı izinleri güncellendi.', 'success')

        return redirect(url_for('admin.user_detail', user_id=user_id))

    

    return render_template(

        'admin/user_permissions.html',

        user=user,

        available_permissions=available_permissions,

        current_permissions=user.permissions

    )





@admin_bp.route('/support')

@admin_required

def support_tickets():

    """List all support tickets for admin."""

    from app.services.support_service import get_all_tickets_admin

    tickets = get_all_tickets_admin()

    return render_template('admin/support_list.html', tickets=tickets)



@admin_bp.route('/support/<int:ticket_id>', methods=['GET', 'POST'])

@admin_required

def support_ticket_detail(ticket_id):

    """Admin view and reply for support ticket."""

    from app.services.support_service import get_ticket_detail, add_message, update_ticket_status

    from app.services.email_service import send_support_ticket_reply_email, send_support_ticket_resolved_email

    

    ticket = get_ticket_detail(ticket_id)

    if not ticket:

        flash('Talep bulunamadı.', 'danger')

        return redirect(url_for('admin.support_tickets'))

        

    if request.method == 'POST':

        if 'reply' in request.form:

            message = request.form.get('message')

            file = request.files.get('file')

            

            if message:

                add_message(ticket_id, current_user.id, message, is_admin=True, file=file)

                send_support_ticket_reply_email(ticket.user, ticket, message)

                flash('Yanıt gönderildi.', 'success')

        elif 'resolve' in request.form:

            update_ticket_status(ticket_id, 'resolved')

            send_support_ticket_resolved_email(ticket.user, ticket)

            flash('Talep çözüldü olarak işaretlendi.', 'success')

        elif 'close' in request.form:

            update_ticket_status(ticket_id, 'closed')

            flash('Talep kapatıldı.', 'secondary')

            

        return redirect(url_for('admin.support_ticket_detail', ticket_id=ticket_id))

        

    return render_template('admin/support_detail.html', ticket=ticket)



# ---------------- Announcement Management ----------------



@admin_bp.route('/announcements')

@admin_required

def announcements():

    """List all announcements."""

    page = request.args.get('page', 1, type=int)

    announcements = Announcement.query.order_by(Announcement.created_at.desc()).paginate(page=page, per_page=10)

    return render_template('admin/announcements.html', announcements=announcements)



@admin_bp.route('/announcements/new', methods=['GET', 'POST'])

@admin_required

def new_announcement():

    """Create a new announcement."""

    if request.method == 'POST':

        title = request.form.get('title')

        content = request.form.get('content')

        priority = request.form.get('priority', 'normal')

        is_active = request.form.get('is_active') == 'on'

        expires_at_str = request.form.get('expires_at')

        

        expires_at = None

        if expires_at_str:

            try:

                expires_at = datetime.strptime(expires_at_str, '%Y-%m-%d')

            except ValueError:

                pass

        

        announcement = Announcement(

            title=title,

            content=content,

            priority=priority,

            is_active=is_active,

            expires_at=expires_at

        )

        

        db.session.add(announcement)

        db.session.commit()

        

        AdminLog.log_action(current_user.id, 'create_announcement', details=f'Created: {title}')

        flash('Duyuru oluxturuldu.', 'success')

        return redirect(url_for('admin.announcements'))

        

    return render_template('admin/announcement_form.html')



@admin_bp.route('/announcements/<int:id>/edit', methods=['GET', 'POST'])

@admin_required

def edit_announcement(id):

    """Edit an announcement."""

    announcement = Announcement.query.get_or_404(id)

    

    if request.method == 'POST':

        announcement.title = request.form.get('title')

        announcement.content = request.form.get('content')

        announcement.priority = request.form.get('priority', 'normal')

        announcement.is_active = request.form.get('is_active') == 'on'

        expires_at_str = request.form.get('expires_at')

        

        if expires_at_str:

            try:

                announcement.expires_at = datetime.strptime(expires_at_str, '%Y-%m-%d')

            except ValueError:

                pass

        else:

            announcement.expires_at = None

            

        db.session.commit()

        AdminLog.log_action(current_user.id, 'edit_announcement', details=f'Edited: {announcement.title}')

        flash('Duyuru güncellendi.', 'success')

        return redirect(url_for('admin.announcements'))

        

    return render_template('admin/announcement_form.html', announcement=announcement)



@admin_bp.route('/announcements/<int:id>/delete', methods=['POST'])

@admin_required

def delete_announcement(id):

    """Delete an announcement."""

    announcement = Announcement.query.get_or_404(id)

    title = announcement.title

    db.session.delete(announcement)

    db.session.commit()

    

    AdminLog.log_action(current_user.id, 'delete_announcement', details=f'Deleted: {title}')

    flash('Duyuru silindi.', 'success')

    return redirect(url_for('admin.announcements'))



# ---------------- User Creation ----------------



@admin_bp.route('/users/new', methods=['GET', 'POST'])

@admin_required

def new_user():

    """Create a new user manually."""

    if request.method == 'POST':

        full_name = request.form.get('full_name')

        email = request.form.get('email')

        password = request.form.get('password')

        role = request.form.get('role', 'user')

        subscription_plan = request.form.get('subscription_plan', 'free')

        

        # Validation

        if User.query.filter_by(email=email).first():

            flash('Bu email adresi zaten kullanılıyor.', 'danger')

            return redirect(url_for('admin.new_user'))

            

        user = User(

            full_name=full_name,

            email=email,

            is_admin=(role == 'admin'),
            is_active=True,
            is_email_verified=True  # Auto-verify admin created users
        )
        user.set_password(password)
        
        db.session.add(user)
        db.session.commit()
        
        # Create subscription
        from app.models import Subscription
        sub = Subscription(
            user_id=user.id, 
            plan=subscription_plan,
            is_approved=True,  # Auto-approve admin created users
            status='active'
        )
        db.session.add(sub)

        db.session.commit()

        

        AdminLog.log_action(current_user.id, 'create_user', target_user_id=user.id, details=f'Created user: {email}')

        flash(f'Kullanıcı "{full_name}" baxarıyla oluxturuldu.', 'success')

        return redirect(url_for('admin.users'))

        

    return render_template('admin/user_form.html')



# ---------------- Impersonate User ----------------



@admin_bp.route('/users/<int:user_id>/impersonate', methods=['POST'])

@admin_required

def impersonate_user(user_id):

    """Log in as another user (Impersonate)."""

    user = User.query.get_or_404(user_id)

    

    if user.is_admin:

        flash('Yöneticilerin yerine geçilemez.', 'warning')

        return redirect(url_for('admin.users'))

        

    # Store original admin info in session

    from flask import session

    session['original_admin_id'] = current_user.id

    

    # Log in as the target user

    from flask_login import login_user

    login_user(user)

    

    flash(f'"{user.full_name}" kullanıcısı olarak girix yapıldı.', 'info')

    return redirect(url_for('main.dashboard'))



@admin_bp.route('/stop_impersonating')

@login_required

def stop_impersonating():

    """Stop impersonating and return to admin account."""

    from flask import session

    original_admin_id = session.get('original_admin_id')

    

    if not original_admin_id:

        abort(403)

        

    # Log back into admin account

    admin_user = User.query.get(original_admin_id)

    if admin_user:

        from flask_login import login_user

        login_user(admin_user)

        session.pop('original_admin_id', None)

        flash('Admin hesabına geri dönüldü.', 'success')

        return redirect(url_for('admin.users'))

    

    flash('Admin hesabı bulunamadı.', 'danger')

    return redirect(url_for('auth.logout'))

@admin_bp.route('/activity_monitor')
@admin_required
def activity_monitor():
    """Real-time user activity monitor page."""
    return render_template('admin/activity_monitor.html')

@admin_bp.route('/user_logs/<int:user_id>')
@admin_required
def user_activity_logs(user_id):
    """View paginated logs for a specific user."""
    user = User.query.get_or_404(user_id)
    page = request.args.get('page', 1, type=int)
    per_page = 50
    
    logs = UserActivityLog.query.filter_by(user_id=user_id)\
        .order_by(UserActivityLog.created_at.desc())\
        .paginate(page=page, per_page=per_page, error_out=False)
        
    return render_template('admin/user_logs.html', user=user, logs=logs)


@admin_bp.route('/users/<int:user_id>/orders')
@admin_required
def user_orders(user_id):
    """View orders for a specific user."""
    from app.models import Order
    user = User.query.get_or_404(user_id)
    
    page = request.args.get('page', 1, type=int)
    per_page = 20
    pagination = Order.query.filter_by(user_id=user_id).order_by(Order.created_at.desc()).paginate(page=page, per_page=per_page, error_out=False)
    orders = pagination.items
    
    return render_template('admin/user_orders.html', user=user, orders=orders, pagination=pagination)


@admin_bp.route('/api/live_logs')
@admin_required
def api_live_logs():
    """Return recent user activity logs as JSON."""
    try:
        limit = request.args.get('limit', 50, type=int)
        after_id = request.args.get('after_id', 0, type=int)
        
        query = UserActivityLog.query
        if after_id > 0:
            query = query.filter(UserActivityLog.id > after_id)
            
        logs = query.order_by(UserActivityLog.id.desc()).limit(limit).all()
        
        data = []
        for log in logs:
            data.append({
                'id': log.id,
                'user': log.user.email if log.user else 'Unknown',
                'action': log.action,
                'marketplace': log.marketplace or '-',
                'details': log.details,
                'ip': log.ip_address,
                'time': log.created_at.strftime('%H:%M:%S'),
                'timestamp': log.created_at.isoformat()
            })
        
        return jsonify({'success': True, 'logs': data})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500

@admin_bp.route('/debug/xml-log')
@admin_required
def debug_xml_log():
    """Temporary route to view XML debug log."""
    try:
        import os
        log_path = os.path.join(os.getcwd(), 'xml_debug.log')
        if not os.path.exists(log_path):
            return "Log dosyası bulunamadı. XML'i tekrar yükleyip deneyin. (Sunucu ana dizininde xml_debug.log dosyası yok)", 404
        
        with open(log_path, 'r', encoding='utf-8') as f:
            content = f.read()
            # Escape HTML characters for safety if needed, but simple XML dump is usually fine
            import html
            content = html.escape(content)
        return f"<html><body style='font-family: monospace; background: #f4f4f4; padding: 20px;'><h3>XML Debug Log</h3><pre style='background: white; padding: 15px; border-radius: 5px; overflow: auto;'>{content}</pre></body></html>"
    except Exception as e:
        return f"Hata: {e}", 500


@admin_bp.route('/team-permissions')
@super_admin_required
def team_permissions():
    """List all support team members (admins except super admin)."""
    # Get all admin users except the super admin
    team_members = User.query.filter(
        User.is_admin == True,
        User.email != 'bugraerkaradeniz34@gmail.com'
    ).order_by(User.created_at.desc()).all()
    
    return render_template('admin/team_permissions.html', 
                         team_members=team_members)


@admin_bp.route('/bug_z_settings')
@admin_required
def bug_z_settings():
    """Manage BUG-Z Bayilik Plan users and settings."""
    from app.models import Subscription, SupplierXML
    # Filter users who have 'bug-z-bayilik' plan in active subscription
    users = User.query.join(Subscription).filter(Subscription.plan == 'bug-z-bayilik').order_by(User.id.desc()).all()
    return render_template('admin/bug_z_settings.html', users=users)

@admin_bp.route('/bug_z/add_xml/<int:user_id>', methods=['POST'])
@admin_required
def add_bug_z_xml(user_id):
    """Add XML source for a BUG-Z user (Admin Bypass)."""
    from app.models import SupplierXML
    
    user = User.query.get_or_404(user_id)
    name = request.form.get('name')
    url = request.form.get('url')
    
    if not name or not url:
        flash('İsim ve URL gereklidir.', 'danger')
        return redirect(url_for('admin.bug_z_settings'))
        
    try:
        logger.info(f"Admin {current_user.email} is adding BUG-Z XML for user {user.email}: {name} ({url})")
        new_xml = SupplierXML(user_id=user.id, name=name, url=url, active=True)
        db.session.add(new_xml)
        db.session.commit()
        
        # Log action
        AdminLog.log_action(
            admin_id=current_user.id,
            action='add_bugz_xml',
            target_user_id=user.id,
            details=f'Added XML for user {user.email}: {name}',
            ip_address=request.remote_addr
        )
        
        logger.info(f"Successfully added BUG-Z XML for user {user.email}. ID: {new_xml.id}")
        flash(f'{user.email} için "{name}" XML kaynağı başarıyla eklendi.', 'success')
    except Exception as e:
        db.session.rollback()
        logger.error(f"Error adding BUG-Z XML for {user.email}: {str(e)}")
        flash(f'XML ekleme hatası: {str(e)}', 'danger')
        
    return redirect(url_for('admin.bug_z_settings'))

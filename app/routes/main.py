import json
import os
import time
from functools import wraps
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, flash, redirect, url_for, abort, current_app, jsonify
from flask_login import login_required, current_user
import logging
from app import db
from app.models import Product, BatchLog, Setting, SupplierXML, Order, Announcement, OrderItem, AdminLog, MarketplaceProduct
from app.services.trendyol_service import (
    get_trendyol_client, load_trendyol_snapshot, fetch_trendyol_categories_flat
)
from app.services.pazarama_service import (
    get_pazarama_client, pazarama_build_product_index, fetch_pazarama_categories_flat
)
from app.services.xml_service import fetch_xml_from_url
import threading
from app.utils.helpers import get_marketplace_multiplier, to_int, to_float

main_bp = Blueprint('main', __name__)

def background_dashboard_sync(app, user_id):
    """Background thread to sync all products and orders"""
    with app.app_context():
        try:
            from app.services.order_service import sync_all_orders, sync_all_products
            from app.models import Setting
            logging.info(f"BACKGROUND: Starting sync for user {user_id}...")
            sync_all_products(user_id=user_id)
            sync_all_orders(user_id=user_id)
            Setting.set('LAST_DASHBOARD_SYNC', datetime.now().isoformat(), user_id=user_id)
            logging.info(f"BACKGROUND: Sync completed for user {user_id}.")
        except Exception as e:
            logging.error(f"BACKGROUND: Sync error for user {user_id}: {e}")

MARKETPLACES = {
    'trendyol': 'Trendyol',
    'pazarama': 'Pazarama',
    'hepsiburada': 'Hepsiburada',
    'n11': 'N11',
    'idefix': 'İdefix',
    'amazon': 'Amazon',
    'ikas': 'İkas',
}

def get_mp_count(mp_name, u_id):
    """Get product count for a marketplace (Hybrid: DB + API fallback)"""
    try:
        from app.models import MarketplaceProduct
        # 1. Try Local DB (Detailed Data)
        stats = db.session.query(
            db.func.count(MarketplaceProduct.id).label('total'),
            db.func.sum(db.case((MarketplaceProduct.status == 'Aktif', 1), else_=0)).label('active'),
            db.func.sum(db.case((MarketplaceProduct.status == 'Pasif', 1), else_=0)).label('passive')
        ).filter_by(user_id=u_id, marketplace=mp_name).first()
        
        if stats and stats.total > 0:
            return {
                'count': stats.total,
                'active': int(stats.active or 0),
                'passive': int(stats.passive or 0)
            }
            
        # 2. API Fallback if DB is empty (Lightweight total count)
        total_api = 0
        try:
            if mp_name == 'trendyol':
                from app.services.trendyol_service import get_trendyol_client
                client = get_trendyol_client(user_id=u_id)
                total_api = client.get_product_count()
            elif mp_name == 'pazarama':
                from app.services.pazarama_service import get_pazarama_client
                client = get_pazarama_client(user_id=u_id)
                total_api = client.get_product_count()
            elif mp_name == 'hepsiburada':
                from app.services.hepsiburada_service import get_hepsiburada_client
                client = get_hepsiburada_client(user_id=u_id)
                total_api = client.get_product_count()
            elif mp_name == 'idefix':
                from app.services.idefix_service import get_idefix_client
                client = get_idefix_client(user_id=u_id)
                total_api = client.get_product_count()
            elif mp_name == 'n11':
                from app.services.n11_client import get_n11_client
                client = get_n11_client()
                if client:
                    total_api = client.get_product_count()
        except Exception as api_err:
            logging.warning(f"Fallback API count failed for {mp_name}: {api_err}")
            
        return {'count': total_api, 'active': total_api, 'passive': 0}
    except Exception as e:
        logging.error(f"Error count for {mp_name}: {e}")
        return {'count': 0, 'active': 0, 'passive': 0}


def permission_required(permission_name):
    """Decorator to check if user has permission to access a page."""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('auth.login'))
            
            if not current_user.has_permission(permission_name):
                flash(f'Bu sayfaya erişim izniniz yok.', 'danger')
                return redirect(url_for('main.dashboard'))
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def admin_required(f):
    """Decorator to require admin access."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login'))
        
        if not current_user.is_admin:
            flash(f'Bu işlem için yönetici yetkisi gereklidir.', 'danger')
            return redirect(url_for('main.dashboard'))
        
        return f(*args, **kwargs)
    return decorated_function

@main_bp.route('/api/chatbot/message', methods=['POST'])
def chatbot_message():
    """Advanced chatbot API."""
    if current_user.is_authenticated and not current_user.has_plan_feature('vidos_assistant'):
        return jsonify({'reply': 'Bu özellik paketinizde kısıtlıdır.'}), 403
    from app.services.chatbot_service import get_chatbot_response
    
    data = request.get_json()
    user_message = data.get('message', '')
    
    reply = get_chatbot_response(user_message)
        
    return jsonify({'reply': reply})

@main_bp.route("/user-manual")
@login_required
def user_manual():
    return render_template('user_manual.html')

@main_bp.route("/gizlilik-politikasi")
def privacy_policy():
    return render_template('legal/privacy.html')

@main_bp.route("/kullanim-kosullari")
def terms_of_use():
    return render_template('legal/terms.html')

@main_bp.route("/kvkk")
def kvkk():
    return render_template('legal/kvkk.html')

@main_bp.route("/mesafeli-satis")
def distance_sales():
    return render_template('legal/distance_sales.html')

@main_bp.route("/iptal-iade")
def refund_policy():
    return render_template('legal/refund.html')

@main_bp.route("/")
def index():
    return render_template('landing.html')

@main_bp.route('/hakkimizda')
def about_us():
    """About us page."""
    return render_template('about_us.html')


@main_bp.route('/iletisim', methods=['GET', 'POST'])
def contact():
    """Contact page with form handling."""
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        subject = request.form.get('subject')
        message = request.form.get('message')
        
        from app.services.email_service import send_contact_form_email
        if send_contact_form_email(name, email, subject, message):
            flash('Mesajınız başarıyla iletildi. En kısa sürede dönüş yapacağız.', 'success')
        else:
            flash('Mesaj gönderilirken bir hata oluştu. Lütfen daha sonra tekrar deneyin.', 'danger')
        return redirect(url_for('main.contact'))
        
    return render_template('contact.html')


@main_bp.route("/dashboard")
@login_required
def dashboard():
    try:
        user_id = current_user.id
        
        # Check email verification
        if not current_user.is_email_verified and not current_user.is_admin:
            return redirect(url_for('auth.verify_email'))
        
        user_id = current_user.id
        
        # Calculate stats for current month
        now = datetime.now()
        start_of_month = datetime(now.year, now.month, 1)

        # Marketplace Stats
        stats = {}
        for mp in ['trendyol', 'pazarama', 'hepsiburada', 'idefix', 'n11']:
            stats[mp] = get_mp_count(mp, user_id)

        marketplaces_stats = [
            {"name": "Trendyol", "key": "trendyol", "icon": "bag-check-fill", "color": "success", 
             "count": stats['trendyol']['count'], "active": stats['trendyol']['active'], "passive": stats['trendyol']['passive']},
            {"name": "Pazarama", "key": "pazarama", "icon": "shop", "color": "primary", 
             "count": stats['pazarama']['count'], "active": stats['pazarama']['active'], "passive": stats['pazarama']['passive']},
            {"name": "Hepsiburada", "key": "hepsiburada", "icon": "cart", "color": "warning", 
             "count": stats['hepsiburada']['count'], "active": stats['hepsiburada']['active'], "passive": stats['hepsiburada']['passive']},
            {"name": "İdefix", "key": "idefix", "icon": "box-fill", "color": "info", 
             "count": stats['idefix']['count'], "active": stats['idefix']['active'], "passive": stats['idefix']['passive']},
            {"name": "N11", "key": "n11", "icon": "tag-fill", "color": "danger", 
             "count": stats['n11']['count'], "active": stats['n11']['active'], "passive": stats['n11']['passive']},
        ]

        total_sent = sum(s['count'] for s in stats.values())

        # 1. Total Revenue (This Month)
        revenue_query = db.session.query(db.func.sum(Order.total_price)).filter(
            Order.created_at >= start_of_month,
            ~Order.status.ilike('%iptal%'), # Cancelled
            ~Order.status.ilike('%iade%'),  # Returned
            ~Order.status.ilike('%cancel%'), 
            ~Order.status.ilike('%return%')
        )
        if user_id:
            revenue_query = revenue_query.filter(Order.user_id == user_id)
        
        monthly_revenue = revenue_query.scalar() or 0.0

        # 2. Total Orders (This Month)
        orders_query = Order.query.filter(Order.created_at >= start_of_month)
        if user_id:
            orders_query = orders_query.filter_by(user_id=user_id)
        monthly_orders = orders_query.count()

        # 3. Returns (This Month)
        returns_query = Order.query.filter(
            Order.created_at >= start_of_month,
            db.or_(Order.status.ilike('%iade%'), Order.status.ilike('%return%'))
        )
        if user_id:
            returns_query = returns_query.filter_by(user_id=user_id)
        monthly_returns = returns_query.count()

        # 4. Cancels (This Month)
        cancels_query = Order.query.filter(
            Order.created_at >= start_of_month,
            db.or_(Order.status.ilike('%iptal%'), Order.status.ilike('%cancel%'))
        )
        if user_id:
            cancels_query = cancels_query.filter_by(user_id=user_id)
        monthly_cancels = cancels_query.count()
        
        # 5. Estimated Net Profit
        estimated_profit = monthly_revenue * 0.25 
        
        # 6. Critical Stock
        critical_limit = int(Setting.get('CRITICAL_STOCK_LIMIT', 10, user_id=user_id) or 10)
        
        critical_stock_query = Product.query.filter(Product.quantity <= critical_limit)
        if user_id:
            critical_stock_query = critical_stock_query.filter_by(user_id=user_id)
        critical_stock_products = critical_stock_query.all()
        
        # 7. Recent Orders
        recent_orders_query = Order.query.order_by(Order.created_at.desc())
        if user_id:
            recent_orders_query = recent_orders_query.filter_by(user_id=user_id)
        recent_orders = recent_orders_query.limit(5).all()

        # 4. Top 5 Bestsellers
        from sqlalchemy import func, desc
        
        bestsellers_query = db.session.query(
            OrderItem.product_name,
            OrderItem.barcode,
            func.sum(OrderItem.quantity).label('total_qty'),
            func.sum(OrderItem.price).label('total_rev')
        ).join(Order).filter(Order.created_at >= start_of_month)
        
        if user_id:
            bestsellers_query = bestsellers_query.filter(Order.user_id == user_id)
            
        bestsellers = bestsellers_query.group_by(OrderItem.product_name, OrderItem.barcode).order_by(desc('total_qty')).limit(5).all()
        
        # Chart Data
        dates = []
        counts = []
        revenues = []
        
        today = datetime.now()
        for i in range(6, -1, -1):
            date = today - timedelta(days=i)
            dates.append(date.strftime('%d.%m'))
            
            day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = date.replace(hour=23, minute=59, second=59, microsecond=999999)
            
            q = db.session.query(
                db.func.count(Order.id),
                db.func.sum(Order.total_price)
            ).filter(Order.created_at >= day_start, Order.created_at <= day_end)
            
            if user_id:
                q = q.filter(Order.user_id == user_id)
                
            day_count, day_rev = q.first()
            counts.append(day_count or 0)
            revenues.append(str(day_rev or 0)) 

        # B. Marketplace Distribution
        mp_query = db.session.query(
            Order.marketplace, db.func.count(Order.id)
        ).group_by(Order.marketplace)
        
        if user_id:
            mp_query = mp_query.filter(Order.user_id == user_id)
            
        mp_results = mp_query.all()
        mp_labels = [r[0] for r in mp_results]
        mp_data = [r[1] for r in mp_results]

        # Announcements
        announcements = Announcement.query.filter_by(is_active=True).order_by(Announcement.priority.desc(), Announcement.created_at.desc()).all()

        # User Notifications
        from app.models.notification import Notification
        user_notifications = Notification.query.filter_by(user_id=user_id, is_read=False).order_by(Notification.created_at.desc()).all()

        # Financial & Usage
        from app.services.subscription_service import get_usage_stats, check_expiring_subscriptions
        from app.services.finance_service import get_financial_summary
        
        usage_stats = get_usage_stats(user_id)
        financial_stats = get_financial_summary(user_id)

        stats = {
            'marketplaces': marketplaces_stats,
            'announcements': announcements,
            'user_notifications': user_notifications,
            'monthly_revenue': financial_stats.get('revenue', 0), 
            'revenue_growth': 0, 
            'monthly_orders': financial_stats.get('order_count', 0),
            'orders_growth': 0,
            'returns_count': monthly_returns,
            'returns_growth': 0,
            'cancel_count': monthly_cancels,
            'estimated_profit': financial_stats.get('gross_profit', 0),
            'critical_stock': critical_stock_products,
            'recent_orders': recent_orders,
            'bestsellers': bestsellers,
            'critical_stock_limit': critical_limit,
            'financial': financial_stats,
            'usage': usage_stats,
            'charts': {
                'dates': dates,
                'sales_counts': counts,
                'sales_revenues': revenues,
                'mp_labels': mp_labels,
                'mp_data': mp_data
            }
        }

        # 8. Last Sync Time (Display)
        from app.models import MarketplaceProduct
        last_sync_prod = MarketplaceProduct.query.filter_by(user_id=user_id).order_by(MarketplaceProduct.last_sync_at.desc()).first()
        last_sync_display = last_sync_prod.last_sync_at.strftime('%d.%m.%Y %H:%M') if last_sync_prod else "Nan"

        return render_template("dashboard.html", stats=stats, last_sync=last_sync_display)
        
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"Dashboard Error: {str(e)}")
        try:
            import os
            with open('dashboard_error.log', 'a') as f:
                f.write(f"\n[{datetime.utcnow()}] Error in dashboard:\n{error_details}\n")
        except: pass
        flash(f'Dashboard yüklenirken bir hata oluştu: {str(e)}', 'danger')
        
        # Provide a minimal stats structure to prevent template crashes
        fallback_stats = {
            'usage': {'products': {'used': 0, 'limit': 0}},
            'marketplaces': [],
            'announcements': [],
            'user_notifications': []
        }
        return render_template('dashboard.html', stats=fallback_stats, last_sync="Hata", error=str(e))

@main_bp.route("/questions")
@login_required
def questions_page():
    """Customer Questions Page"""
    return render_template("questions.html")

@main_bp.route("/returns")
@login_required
def returns_page():
    """Customer Returns/Claims Page"""
    return render_template("returns.html")

@main_bp.route("/excel_products")
@login_required
@permission_required('excel_products')
def excel_products_page():
    """Excel ürünleri sayfası"""
    # Check plan permission (BUG-Z Restriction)
    if not current_user.has_plan_feature('add_excel_source'):
         flash('Bu özellik paketinizde kısıtlıdır.', 'danger')
         return redirect(url_for('main.dashboard'))
         
    return render_template("excel_products.html")



@main_bp.route("/xml_products", methods=["GET"])
@login_required
@permission_required('xml_products')
def xml_urunler():
    return render_template("xml_products.html")

@main_bp.route("/operations/xml_updates")
@login_required
def xml_updates():
    if not current_user.has_plan_feature('add_xml_source'):
        flash('Bu özellik paketinizde kısıtlıdır.', 'danger')
        return redirect(url_for('main.dashboard'))
        
    xml_sources = SupplierXML.query.filter_by(user_id=current_user.id).order_by(SupplierXML.id.desc()).all()
    return render_template("operations/xml_updates.html", xml_sources=xml_sources)

@main_bp.route("/api/dashboard/stats")
@login_required
def api_dashboard_stats():
    """Dashboard istatistiklerini döndür"""
    user_id = current_user.id
    now = datetime.now()
    start_of_month = datetime(now.year, now.month, 1)

    # 1. Total Products (Local)
    total_products = Product.query.filter_by(user_id=user_id).count()

    # 2. Monthly Revenue
    revenue_query = db.session.query(func.sum(Order.total_price))\
        .filter(Order.created_at >= start_of_month)\
        .filter(~Order.status.ilike('%iptal%'))\
        .filter(~Order.status.ilike('%iade%'))\
        .filter(~Order.status.ilike('%cancel%'))\
        .filter(~Order.status.ilike('%return%'))\
        .filter(Order.user_id == user_id)
    monthly_revenue = revenue_query.scalar() or 0.0

    # 3. Monthly Orders
    orders_count = Order.query.filter(Order.created_at >= start_of_month)\
        .filter_by(user_id=user_id).count()

    # 4. Monthly Returns
    returns_count = Order.query.filter(Order.created_at >= start_of_month)\
        .filter_by(user_id=user_id)\
        .filter(or_(Order.status.ilike('%iade%'), Order.status.ilike('%return%'))).count()
        
    # 5. Monthly Cancels
    cancels_count = Order.query.filter(Order.created_at >= start_of_month)\
        .filter_by(user_id=user_id)\
        .filter(or_(Order.status.ilike('%iptal%'), Order.status.ilike('%cancel%'))).count()
        
    # 6. Marketplace Counts
    mp_counts = {
        'trendyol': get_mp_count('trendyol', user_id),
        'pazarama': get_mp_count('pazarama', user_id),
        'hepsiburada': get_mp_count('hepsiburada', user_id),
        'idefix': get_mp_count('idefix', user_id),
        'n11': get_mp_count('n11', user_id)
    }

    # 7. Last Sync Time
    last_sync = Setting.get('LAST_DASHBOARD_SYNC', user_id=user_id)
    if last_sync:
        try:
            last_sync = datetime.fromisoformat(last_sync).strftime('%H:%M:%S')
        except:
            pass

    return jsonify({
        'total_products': total_products,
        'monthly_revenue': monthly_revenue,
        'monthly_orders': orders_count,
        'monthly_returns': returns_count,
        'monthly_cancels': cancels_count,
        'mp_counts': mp_counts,
        'last_sync': last_sync
    })


@main_bp.route("/batch_logs")
@login_required
def batch_logs():
    if not (current_user.is_admin or (current_user.has_permission('batch_logs') and current_user.has_plan_feature('batch_logs'))):
        flash('Bu sayfaya erişim izniniz yok.', 'danger')
        return redirect(url_for('main.dashboard'))
    logs = BatchLog.query.filter_by(user_id=current_user.id).order_by(BatchLog.id.desc()).all()
    return render_template("batch_logs.html", logs=logs)

@main_bp.route("/batch/<batch_id>")
@login_required
@admin_required
def batch_detail(batch_id):
    entry = BatchLog.query.filter_by(batch_id=batch_id, user_id=current_user.id).first()
    if entry is not None:
        return render_template("batch_detail.html", entry=entry, d=entry.get_details())
    
    flash("Log bulunamadı.", "danger")
    return redirect(url_for('main.batch_logs'))

@main_bp.route("/batch/retry/<batch_id>", methods=["POST"])
@login_required
@admin_required
def retry_batch(batch_id):
    """Resubmit failed products from a batch."""
    from app.services.job_queue import submit_mp_job
    
    entry = BatchLog.query.filter_by(batch_id=batch_id, user_id=current_user.id).first_or_404()
    details = entry.get_details()
    
    # 1. Extract failed/skipped barcodes
    failed_barcodes = []
    skipped = details.get('skipped', [])
    if skipped:
        failed_barcodes.extend([s['barcode'] for s in skipped])
    
    # Also check logs for specific barcode errors if possible, or just use 'skipped'
    # For now, 'skipped' is the standard for failed items in our service results.
    
    if not failed_barcodes:
        flash("Yeniden gönderilecek hatalı ürün bulunamadı.", "info")
        return redirect(url_for('main.batch_detail', batch_id=batch_id))
    
    # 2. Get original params
    orig_params = details.get('params', {})
    xml_source_id = orig_params.get('xml_source_id')
    user_id = current_user.id
    
    if not xml_source_id:
        flash("Kaynak XML bilgisi (xml_source_id) eksik, yeniden gönderilemiyor.", "danger")
        return redirect(url_for('main.batch_detail', batch_id=batch_id))

    # 3. Trigger based on marketplace
    marketplace = entry.marketplace
    try:
        if marketplace == 'n11':
            from app.services.n11_service import perform_n11_send_products
            new_job_id = submit_mp_job(
                'n11_retry', 'n11',
                lambda jid: perform_n11_send_products(jid, failed_barcodes, xml_source_id, auto_match=True, user_id=user_id),
                params={'barcodes': failed_barcodes, 'xml_source_id': xml_source_id, 'retry_of': batch_id}
            )
            flash(f"{len(failed_barcodes)} ürün N11 için tekrar sıraya alındı.", "success")
        elif marketplace == 'trendyol':
            from app.services.trendyol_service import perform_trendyol_send_products
            new_job_id = submit_mp_job(
                'trendyol_retry', 'trendyol',
                lambda jid: perform_trendyol_send_products(jid, failed_barcodes, xml_source_id, auto_match=True, user_id=user_id),
                params={'barcodes': failed_barcodes, 'xml_source_id': xml_source_id, 'retry_of': batch_id}
            )
            flash(f"{len(failed_barcodes)} ürün Trendyol için tekrar sıraya alındı.", "success")
        elif marketplace == 'pazarama':
             from app.services.pazarama_service import perform_pazarama_send_products
             new_job_id = submit_mp_job(
                'pazarama_retry', 'pazarama',
                lambda jid: perform_pazarama_send_products(jid, failed_barcodes, xml_source_id, user_id=user_id),
                params={'barcodes': failed_barcodes, 'xml_source_id': xml_source_id, 'retry_of': batch_id}
            )
             flash(f"{len(failed_barcodes)} ürün Pazarama için tekrar sıraya alındı.", "success")
        else:
            flash(f"{marketplace.upper()} için otomatik tekrar gönderim henüz desteklenmiyor.", "warning")
            return redirect(url_for('main.batch_detail', batch_id=batch_id))
            
    except Exception as e:
        flash(f"Hata oluştu: {str(e)}", "danger")
        return redirect(url_for('main.batch_detail', batch_id=batch_id))

    return redirect(url_for('main.batch_logs'))


@main_bp.route("/n11/category-mapping")
@login_required
@permission_required('n11')
def n11_mapping_page():
    """N11 kategori eşleştirme sayfası"""
    # Kayıtlı eşleşmeleri al
    mapping_json = Setting.get('N11_CATEGORY_MAPPING', user_id=current_user.id)
    mapping = json.loads(mapping_json) if mapping_json else {}
    
    # Kullanıcının XML ürünlerindeki benzersiz kategorileri bul (basitçe Product tablosundan)
    # Ya da SupplierXML üzerinden. Product tablosu daha günceldir.
    xml_categories = db.session.query(Product.top_category).filter_by(user_id=current_user.id).distinct().all()
    xml_categories = [c[0] for c in xml_categories if c[0]]
    
    return render_template("n11_mapping.html", mapping=mapping, xml_categories=xml_categories)


@main_bp.route("/settings", methods=["GET", "POST"])
@login_required
@permission_required('settings')
def settings_page():
    user_id = current_user.id
    if request.method == "GET":
        settings = {
            "SELLER_ID": Setting.get("SELLER_ID", "", user_id=user_id),
            "API_KEY": Setting.get("API_KEY", "", user_id=user_id),
            "API_SECRET": Setting.get("API_SECRET", "", user_id=user_id),
            "FORBIDDEN_KEYWORDS": Setting.get("FORBIDDEN_KEYWORDS", "", user_id=user_id),
            "PRICE_MULTIPLIER": Setting.get("PRICE_MULTIPLIER", "1.0", user_id=user_id) or "1.0",
            "TRENDYOL_BRAND_NAME": Setting.get("TRENDYOL_BRAND_NAME", "", user_id=user_id),
            "TRENDYOL_BRAND_ID": Setting.get("TRENDYOL_BRAND_ID", "", user_id=user_id),
            "HB_MERCHANT_ID": Setting.get("HB_MERCHANT_ID", "", user_id=user_id),
            "HB_SERVICE_KEY": Setting.get("HB_SERVICE_KEY", "", user_id=user_id),
            "HB_PRICE_MULTIPLIER": Setting.get("HB_PRICE_MULTIPLIER", "1.0", user_id=user_id) or "1.0",
            "AMAZON_ACCESS_KEY": Setting.get("AMAZON_ACCESS_KEY", "", user_id=user_id),
            "AMAZON_SECRET_KEY": Setting.get("AMAZON_SECRET_KEY", "", user_id=user_id),
            "AMAZON_PRICE_MULTIPLIER": Setting.get("AMAZON_PRICE_MULTIPLIER", "1.0", user_id=user_id) or "1.0",
            "N11_API_KEY": Setting.get("N11_API_KEY", "", user_id=user_id),
            "N11_API_SECRET": Setting.get("N11_API_SECRET", "", user_id=user_id),
            "N11_PRICE_MULTIPLIER": Setting.get("N11_PRICE_MULTIPLIER", "1.0", user_id=user_id) or "1.0",
            "N11_DEFAULT_BRAND": Setting.get("N11_DEFAULT_BRAND", "", user_id=user_id),
            "N11_DEFAULT_SHIPMENT_TEMPLATE": Setting.get("N11_DEFAULT_SHIPMENT_TEMPLATE", "", user_id=user_id),
            "PAZARAMA_API_KEY": Setting.get("PAZARAMA_API_KEY", "", user_id=user_id),
            "PAZARAMA_API_SECRET": Setting.get("PAZARAMA_API_SECRET", "", user_id=user_id),
            "PAZARAMA_PRICE_MULTIPLIER": Setting.get("PAZARAMA_PRICE_MULTIPLIER", "1.0", user_id=user_id) or "1.0",
            "PAZARAMA_BRAND_NAME": Setting.get("PAZARAMA_BRAND_NAME", "", user_id=user_id),
            "PAZARAMA_BRAND_ID": Setting.get("PAZARAMA_BRAND_ID", "", user_id=user_id),
            "IDEFIX_API_KEY": Setting.get("IDEFIX_API_KEY", "", user_id=user_id),
            "IDEFIX_VENDOR_ID": Setting.get("IDEFIX_VENDOR_ID", "", user_id=user_id),
            "IDEFIX_API_SECRET": Setting.get("IDEFIX_API_SECRET", "", user_id=user_id),
            "IDEFIX_PRICE_MULTIPLIER": Setting.get("IDEFIX_PRICE_MULTIPLIER", "1.0", user_id=user_id) or "1.0",
            "IDEFIX_BRAND_NAME": Setting.get("IDEFIX_BRAND_NAME", "", user_id=user_id),
            "IDEFIX_BRAND_ID": Setting.get("IDEFIX_BRAND_ID", "", user_id=user_id),
            "IDEFIX_DEFAULT_CATEGORY_ID": Setting.get("IDEFIX_DEFAULT_CATEGORY_ID", "", user_id=user_id),
            "IDEFIX_BARCODE_PREFIX": Setting.get("IDEFIX_BARCODE_PREFIX", "", user_id=user_id),
            "IDEFIX_USE_RANDOM_BARCODE": Setting.get("IDEFIX_USE_RANDOM_BARCODE", "off", user_id=user_id),
            "INSTAGRAM_ACCESS_TOKEN": Setting.get("INSTAGRAM_ACCESS_TOKEN", "", user_id=user_id),
            "INSTAGRAM_ACCOUNT_ID": Setting.get("INSTAGRAM_ACCOUNT_ID", "", user_id=user_id),
            "CRITICAL_STOCK_LIMIT": Setting.get("CRITICAL_STOCK_LIMIT", "3", user_id=user_id),
            
            # New Price Settings
            "PRICE_PERCENTAGE": Setting.get("PRICE_PERCENTAGE", "0", user_id=user_id),
            "PRICE_FIXED": Setting.get("PRICE_FIXED", "0", user_id=user_id),
            "HB_PRICE_PERCENTAGE": Setting.get("HB_PRICE_PERCENTAGE", "0", user_id=user_id),
            "HB_PRICE_FIXED": Setting.get("HB_PRICE_FIXED", "0", user_id=user_id),
            "N11_PRICE_PERCENTAGE": Setting.get("N11_PRICE_PERCENTAGE", "0", user_id=user_id),
            "N11_PRICE_FIXED": Setting.get("N11_PRICE_FIXED", "0", user_id=user_id),
            "PAZARAMA_PRICE_PERCENTAGE": Setting.get("PAZARAMA_PRICE_PERCENTAGE", "0", user_id=user_id),
            "PAZARAMA_PRICE_FIXED": Setting.get("PAZARAMA_PRICE_FIXED", "0", user_id=user_id),
            "IDEFIX_PRICE_PERCENTAGE": Setting.get("IDEFIX_PRICE_PERCENTAGE", "0", user_id=user_id),
            "IDEFIX_PRICE_FIXED": Setting.get("IDEFIX_PRICE_FIXED", "0", user_id=user_id),
        }
        xml_sources = SupplierXML.query.filter_by(user_id=user_id).order_by(SupplierXML.id.desc()).all()
        return render_template("settings.html", settings=settings, xml_sources=xml_sources)

    elif request.method == "POST":
        from app.services.subscription_service import get_active_marketplaces, get_subscription
        
        subscription = get_subscription(user_id)
        active_mps = get_active_marketplaces(user_id)
        limit = subscription.max_marketplaces if subscription else 1
        
        # Save all settings
        all_keys = [
            "SELLER_ID", "API_KEY", "API_SECRET", "FORBIDDEN_KEYWORDS", "PRICE_MULTIPLIER",
            "TRENDYOL_BRAND_NAME", "TRENDYOL_BRAND_ID",
            "HB_MERCHANT_ID", "HB_SERVICE_KEY", "HB_PRICE_MULTIPLIER",
            "AMAZON_ACCESS_KEY", "AMAZON_SECRET_KEY", "AMAZON_PRICE_MULTIPLIER",
            "N11_API_KEY", "N11_API_SECRET", "N11_PRICE_MULTIPLIER",
            "N11_DEFAULT_BRAND", "N11_DEFAULT_SHIPMENT_TEMPLATE",
            "PAZARAMA_API_KEY", "PAZARAMA_API_SECRET", "PAZARAMA_PRICE_MULTIPLIER",
            "PAZARAMA_BRAND_NAME", "PAZARAMA_BRAND_ID",
            "IDEFIX_API_KEY", "IDEFIX_VENDOR_ID", "IDEFIX_API_SECRET", "IDEFIX_PRICE_MULTIPLIER",
            "IDEFIX_BRAND_NAME", "IDEFIX_BRAND_ID", "IDEFIX_DEFAULT_CATEGORY_ID",
            "IDEFIX_BARCODE_PREFIX", "IDEFIX_USE_RANDOM_BARCODE",
            "INSTAGRAM_ACCESS_TOKEN", "INSTAGRAM_ACCOUNT_ID",
            "CRITICAL_STOCK_LIMIT",
            
            # New Price Keys
            "PRICE_PERCENTAGE", "PRICE_FIXED",
            "HB_PRICE_PERCENTAGE", "HB_PRICE_FIXED",
            "N11_PRICE_PERCENTAGE", "N11_PRICE_FIXED",
            "PAZARAMA_PRICE_PERCENTAGE", "PAZARAMA_PRICE_FIXED",
            "IDEFIX_PRICE_PERCENTAGE", "IDEFIX_PRICE_FIXED",
        ]
        
        # Identify which MP being updated/added
        mp_check = {
            "SELLER_ID": "trendyol",
            "HB_MERCHANT_ID": "hepsiburada",
            "N11_API_KEY": "n11",
            "PAZARAMA_API_KEY": "pazarama",
            "IDEFIX_API_KEY": "idefix"
        }
        
        for k in all_keys:
            if k in request.form:
                val = request.form.get(k, "").strip()
                
                # Limit enforcement for NEW marketplaces
                if k in mp_check and val:
                    mp_name = mp_check[k]
                    if mp_name not in active_mps and len(active_mps) >= limit and limit != -1 and not current_user.is_admin:
                        flash(f"Pazaryeri limitinize ulaştınız ({limit}). Daha fazlası için paketinizi yükseltin.", "danger")
                        continue # Skip this setting
                
                Setting.set(k, val, user_id=user_id)
                
        flash("Ayarlar kaydedildi.", "success")
        
        # Log action
        AdminLog.log_action(
            admin_id=user_id,
            action='update_settings',
            details=f'Updated settings: {", ".join([k for k in all_keys if k in request.form])}',
            ip_address=request.remote_addr
        )
        
        return redirect(url_for("main.settings_page"))


@main_bp.route("/profile/update", methods=["POST"])
@login_required
def update_profile():
    """Update user profile information."""
    try:
        current_user.full_name = request.form.get('full_name', '').strip()
        current_user.company_name = request.form.get('company_name', '').strip()
        current_user.phone = request.form.get('phone', '').strip()
        
        db.session.commit()
        
        # Log action
        AdminLog.log_action(
            admin_id=current_user.id,
            action='update_profile',
            details=f'Updated profile: {current_user.full_name}',
            ip_address=request.remote_addr
        )
        
        flash("Profil bilgileriniz güncellendi.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Profil güncellenirken hata oluştu: {str(e)}", "danger")
    
    return redirect(url_for("main.settings_page"))


@main_bp.route("/instagram", methods=["GET", "POST"])
@login_required
@permission_required('settings')
def instagram_tools():
    """Instagram story and post tools page."""
    from app.services.scheduler_service import add_instagram_job, get_scheduled_instagram_jobs, execute_instagram_task
    from app.services.image_template_service import ImageTemplateService
    
    # Check plan permission
    if not current_user.has_plan_feature('instagram_panel'):
        flash('Bu özellik paketinizde mevcut değil. Lütfen yükseltin.', 'danger')
        return redirect(url_for('main.dashboard'))
    
    if request.method == "POST":
        action = request.form.get('action')
        
        # --- PREVIEW GENERATION ---
        if action == 'generate_preview':
            image_url = request.form.get('image_url')
            title = request.form.get('title')
            price = to_float(request.form.get('price'))
            discount_price = to_float(request.form.get('discount_price'))
            template_style = request.form.get('template_style', 'modern')
            
            if not image_url or not title or not price:
                return jsonify({'success': False, 'message': 'Eksik bilgi.'})
                
            generated_path = ImageTemplateService.create_story_image(
                image_url=image_url,
                title=title,
                price=price,
                discount_price=discount_price,
                template_style=template_style
            )
            
            if generated_path:
                return jsonify({'success': True, 'image_path': generated_path})
            else:
                return jsonify({'success': False, 'message': 'Görsel oluşturulamadı.'})
        
        # --- SCHEDULE or SHARE NOW ---
        elif action in ['schedule', 'share_now']:
            image_url = request.form.get('image_url')
            if not image_url:
                 flash("Görsel URL eksik.", "danger")
                 return redirect(url_for('main.instagram_tools'))

            media_type = request.form.get('media_type', 'story') # story or post
            run_date_str = request.form.get('run_date') # YYYY-MM-DDTHH:MM
            
            job_data = {
                'image_url': image_url,
                'title': request.form.get('title'),
                'price': to_float(request.form.get('price')),
                'discount_price': to_float(request.form.get('discount_price')),
                'template_style': request.form.get('template_style', 'modern'),
                'media_type': media_type,
                'caption': request.form.get('caption')
            }
            
            # If SHARE NOW
            if action == 'share_now':
                # Execute immediately in background or foreground? 
                # For UI responsiveness, let's do it here call the function directly (it's sync for now or we spawn thread)
                # But execute_instagram_task is synchronous in the service.
                execute_instagram_task(job_data)
                flash(f"{media_type.capitalize()} paylaşım sırasına alındı (Simülasyon).", "success")
                
            # If SCHEDULE
            else:
                if not run_date_str:
                     flash("Tarih seçmelisiniz.", "warning")
                else:
                    try:
                        run_date = datetime.strptime(run_date_str, '%Y-%m-%dT%H:%M')
                        if add_instagram_job(job_data, run_date):
                            flash(f"{media_type.capitalize()} {run_date_str} için zamanlandı.", "success")
                        else:
                            flash("Zamanlama hatası.", "danger")
                    except ValueError:
                         flash("Tarih formatı hatalı.", "danger")
            
            return redirect(url_for('main.instagram_tools'))
            
    # GET Request: Fetch extra data
    scheduled_jobs = get_scheduled_instagram_jobs()
    
    # Fetch Products (Limit 50 for performance, maybe add search later)
    products = Product.query.filter_by(user_id=current_user.id).order_by(Product.id.desc()).limit(50).all()
    xml_sources = SupplierXML.query.filter_by(user_id=current_user.id).all()
    
    return render_template("instagram.html", scheduled_jobs=scheduled_jobs, products=products, xml_sources=xml_sources)


@main_bp.route("/settings/fetch_trendyol_categories", methods=["POST"])
@login_required
def fetch_trendyol_categories():
    """Fetch all Trendyol categories and cache them."""
    try:
        from app.services.trendyol_service import fetch_and_cache_categories
        result = fetch_and_cache_categories()
        return jsonify(result)
    except Exception as e:
        return jsonify({
            'success': False, 
            'message': f'Kategori çekme hatası: {str(e)}'
        }), 500


@main_bp.route("/settings/fetch_trendyol_brands", methods=["POST"])
@login_required
def fetch_trendyol_brands():
    """Fetch all Trendyol brands and cache them."""
    try:
        from app.services.trendyol_service import fetch_and_cache_brands
        result = fetch_and_cache_brands()
        return jsonify(result)
    except Exception as e:
        return jsonify({
            'success': False, 
            'message': f'Marka çekme hatası: {str(e)}'
        }), 500


@main_bp.route("/settings/fetch_idefix_categories", methods=["POST"])
@login_required
def fetch_idefix_categories():
    """Fetch Idefix categories in background."""
    try:
        from app.services.job_queue import submit_mp_job
        from app.services.idefix_service import fetch_and_cache_categories
        
        from flask_login import current_user
        user_id = current_user.id
        
        job_id = submit_mp_job(
            'fetch_categories', 'idefix',
            lambda jid: fetch_and_cache_categories(user_id=user_id, job_id=jid),
            params={'description': 'İdefix kategori listesini güncelle'}
        )
        return jsonify({
            'success': True, 
            'job_id': job_id,
            'message': 'Kategori çekme işlemi başlatıldı.'
        }), 202
    except Exception as e:
        return jsonify({
            'success': False, 
            'message': f'İşlem başlatılamadı: {str(e)}'
        }), 500

@main_bp.route("/settings/fetch_pazarama_categories", methods=["POST"])
@login_required
def fetch_pazarama_categories():
    """Fetch Pazarama categories and save to settings for TF-IDF matching."""
    try:
        client = get_pazarama_client()
        leafs = fetch_pazarama_categories_flat(client)
        Setting.set("PAZARAMA_CATEGORY_TREE", json.dumps(leafs, ensure_ascii=False))
        return jsonify({
            'success': True, 
            'message': f'Pazarama kategori ağacı çekildi. Toplam {len(leafs)} kategori kaydedildi.'
        })
    except Exception as e:
        return jsonify({
            'success': False, 
            'message': f'Pazarama kategori çekme hatası: {str(e)}'
        }), 500

@main_bp.route("/settings/fetch_n11_categories", methods=["POST"])
@login_required
def fetch_n11_categories():
    """Fetch all N11 categories and cache them."""
    try:
        from app.services.n11_service import fetch_and_cache_n11_categories
        # force=True implies explicit request from UI
        success = fetch_and_cache_n11_categories(force=True)
        if success:
            return jsonify({
                'success': True,
                'message': 'N11 kategorileri başarıyla çekildi.'
            })
        else:
             return jsonify({
                'success': False,
                'message': 'Kategoriler çekilemedi. API ayarlarını kontrol edin.'
            })
    except Exception as e:
        return jsonify({
            'success': False, 
            'message': f'N11 kategori çekme hatası: {str(e)}'
        }), 500


@main_bp.route("/settings/auto_map_xml_categories", methods=["POST"])
@login_required
def auto_map_xml_categories():
    # Logic for auto mapping
    # ... (Simplified or copied from app.py)
    # For now, placeholder
    flash("⚠️ Otomatik eşleme henüz taşınmadı.", "warning")
    return redirect(url_for("main.settings_page"))

@main_bp.route("/products/<marketplace>", methods=["GET"])
@login_required
def marketplace_products_page(marketplace: str):
    if marketplace not in MARKETPLACES:
        abort(404)
    
    # Check permission for specific marketplace
    if not current_user.has_permission(marketplace):
        flash(f'Bu pazar yerine erişim izniniz yok.', 'danger')
        return redirect(url_for('main.dashboard'))
    
    mp_multiplier = get_marketplace_multiplier(marketplace)
    return render_template(
        "marketplace_products.html",
        marketplace=marketplace,
        marketplace_name=MARKETPLACES.get(marketplace),
        mp_multiplier=mp_multiplier
    )

@main_bp.route("/orders")
@login_required
@permission_required('orders')
def orders_page():
    from app.services.order_service import get_orders
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    marketplace = request.args.get('marketplace')
    status = request.args.get('status')
    search = request.args.get('search')
    sort_by = request.args.get('sort_by')
    order = request.args.get('order', 'desc')
    
    # Limit per_page to reasonable values
    per_page = min(max(per_page, 5), 100)
    
    orders = get_orders(user_id=current_user.id, page=page, per_page=per_page, marketplace=marketplace, status=status, search=search, sort_by=sort_by, order=order)
    return render_template("orders.html", orders=orders)

@main_bp.route("/orders/sync-all")
@login_required
def sync_all_orders_route():
    from app.services.order_service import sync_all_orders
    sync_all_orders(user_id=current_user.id)
    flash("Sipariş senkronizasyonu başlatıldı.", "success")
    return redirect(url_for('main.orders_page'))

@main_bp.route('/order/<int:order_id>')
@login_required
def order_detail(order_id):
    order = Order.query.filter_by(id=order_id, user_id=current_user.id).first_or_404()
    return render_template('order_detail.html', order=order)

@main_bp.route('/order/<int:order_id>/print_label')
@login_required
def print_order_label(order_id):
    order = Order.query.filter_by(id=order_id, user_id=current_user.id).first_or_404()
    return render_template('print_label.html', order=order, now=datetime.now())

@main_bp.route('/products')
@login_required
@permission_required('auto_sync')
def auto_sync_page():
    """Otomatik senkronizasyon yönetim sayfası"""
    if not current_user.has_plan_feature('auto_sync'):
        flash('Bu özellik paketinizde kısıtlıdır.', 'danger')
        return redirect(url_for('main.dashboard'))
    xml_sources = SupplierXML.query.filter_by(user_id=current_user.id).all()
    return render_template("auto_sync.html", marketplaces=MARKETPLACES, xml_sources=xml_sources)


# ============================================================
# Trendyol Ek Sayfalar (Sorular, İadeler)
# ============================================================

@main_bp.route("/trendyol/questions")
@login_required
@permission_required('trendyol')
def trendyol_questions_page():
    """Trendyol müşteri soruları sayfası"""
    return render_template("trendyol_questions.html")


@main_bp.route("/trendyol/claims")
@login_required
@permission_required('trendyol')
def trendyol_claims_page():
    """Trendyol iade talepleri sayfası"""
    return render_template("trendyol_claims.html")

# ============================================================
# Instagram Entegrasyonu
# ============================================================

@main_bp.route("/instagram")
@login_required
def instagram_panel():
    """Instagram paylaşım paneli"""
    # Kaynakları filtreleme için gönderelim
    xml_sources = SupplierXML.query.filter_by(user_id=current_user.id).all()
    return render_template("instagram_panel.html", xml_sources=xml_sources)

@main_bp.route("/api/instagram/share", methods=["POST"])
@login_required
def instagram_share():
    """Instagram'da fotoğraf paylaş"""
    data = request.json
    image_url = data.get('image_url')
    caption = data.get('caption')
    
    if not image_url:
        return jsonify({'success': False, 'message': 'Görsel URL gereklidir'}), 400
        
    from app.services.instagram_service import publish_photo
    result = publish_photo(image_url, caption, user_id=current_user.id)
    
    return jsonify(result)

@main_bp.route("/list-files")
@login_required
@admin_required
def list_files():
    """List files in the current working directory."""
    try:
        # Get current project path
        path = os.getcwd()
        items = []
        
        for item in os.listdir(path):
            item_path = os.path.join(path, item)
            is_dir = os.path.isdir(item_path)
            size = os.path.getsize(item_path) if not is_dir else 0
            mtime = datetime.fromtimestamp(os.path.getmtime(item_path)).strftime('%Y-%m-%d %H:%M:%S')
            
            items.append({
                'name': item,
                'is_dir': is_dir,
                'size': size,
                'mtime': mtime
            })
            
        # Sort: directories first, then files by name
        items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        
        return render_template("file_list.html", items=items, path=path)
    except Exception as e:
        flash(f"Dosyalar listelenirken hata oluştu: {str(e)}", "danger")
        return redirect(url_for('main.dashboard'))


@main_bp.route('/bug-z-bayilik')
def bug_z_detail():
    """BUG-Z Bayilik Paketi detail page."""
    return render_template('bug_z_detail.html')

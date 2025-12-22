from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from app import db
from app.models import BatchLog, Order, Product, OrderItem
import json

report_bp = Blueprint('report', __name__)

def permission_required(permission_name):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('auth.login'))
            if not current_user.has_permission(permission_name):
                flash('Bu sayfaya erişim izniniz yok.', 'danger')
                return redirect(url_for('main.dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

@report_bp.route('/my-errors')
@login_required
def error_history():
    """
    Displays a user-friendly list of recent operational errors.
    Filters BatchLogs for entries that are marked as failed or have failure counts.
    """
    page = request.args.get('page', 1, type=int)
    
    pagination = BatchLog.query.filter_by(user_id=current_user.id).order_by(BatchLog.id.desc()).paginate(page=page, per_page=20)
    
    error_reports = []
    
    for log in pagination.items:
        details = log.get_details()
        fail_count = details.get('fail_count', 0)
        
        # If success=False OR partial failures exist
        if not log.success or fail_count > 0:
            
            # Summarize reason
            summary = "İşlem sırasında hatalar oluştu."
            if not log.success:
                summary = "İşlem tamamen başarısız oldu."
            elif fail_count > 0:
                summary = f"{fail_count} adet ürün işlenemedi."
                
            # Extract common error messages if available
            failures = details.get('failures', [])
            top_errors = []
            if failures:
                # failure can be string or dict
                for f in failures[:3]:
                    if isinstance(f, dict):
                        top_errors.append(f.get('reason') or f.get('error') or str(f))
                    else:
                        top_errors.append(str(f))
            
            error_reports.append({
                'id': log.id,
                'batch_id': log.batch_id,
                'timestamp': log.timestamp,
                'marketplace': log.marketplace,
                'summary': summary,
                'top_errors': top_errors,
                'is_critical': not log.success
            })
            
    return render_template('reports/error_history.html', reports=error_reports, pagination=pagination)

@report_bp.route('/my-errors/<batch_id>')
@login_required
def error_detail(batch_id):
    """Detailed view for a specific error report"""
    log = BatchLog.query.filter_by(batch_id=batch_id, user_id=current_user.id).first_or_404()
    details = log.get_details()
    
    # Process failures for display
    failures = details.get('failures', [])
    formatted_failures = []
    for f in failures:
        if isinstance(f, dict):
            formatted_failures.append({
                'barcode': f.get('barcode', '-'),
                'reason': f.get('reason') or f.get('error', 'Bilinmeyen Hata')
            })
        else:
            formatted_failures.append({
                'barcode': '-',
                'reason': str(f)
            })
            
    return render_template('reports/error_detail.html', log=log, failures=formatted_failures)

@report_bp.route('/profit-loss')
@login_required
def profit_loss_report():
    """Detailed Profit/Loss Report."""
    from datetime import datetime, timedelta
    
    # Permission check (optional if not using the decorator, but good practice)
    if not current_user.has_permission('reports'):
         flash('Bu rapora erişim yetkiniz yok.', 'danger')
         return redirect(url_for('main.dashboard'))

    # Date Filter
    start_date_str = request.args.get('start_date')
    end_date_str = request.args.get('end_date')
    
    today = datetime.now().date()
    start_of_month = today.replace(day=1)
    
    if start_date_str:
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
        except ValueError:
            start_date = datetime.combine(start_of_month, datetime.min.time())
    else:
        start_date = datetime.combine(start_of_month, datetime.min.time())
        
    if end_date_str:
        try:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
        except ValueError:
             end_date = datetime.combine(today, datetime.max.time())
    else:
        end_date = datetime.combine(today, datetime.max.time())
    
    # Ensure end_date includes the full day if it's just a date
    if isinstance(end_date, datetime) and end_date.hour == 0 and end_date.minute == 0:
         end_date = end_date.replace(hour=23, minute=59, second=59)

    # Queries
    # 1. Total Sales (Revenue)
    revenue_query = db.session.query(db.func.sum(Order.total_price)).filter(
        Order.created_at >= start_date, Order.created_at <= end_date,
        ~Order.status.ilike('%iptal%'), ~Order.status.ilike('%cancel%'), ~Order.status.ilike('%iade%')
    )
    if current_user.id:  # Assuming user filter
         revenue_query = revenue_query.filter(Order.user_id == current_user.id)
         
    total_revenue = revenue_query.scalar() or 0.0
    
    # 2. Total Cost (Approximate)
    total_cost = 0.0
    
    # Check if OrderItem table has data
    has_items = OrderItem.query.first()
    
    if has_items:
        cost_query = db.session.query(
            db.func.sum(OrderItem.quantity * Product.cost_price)
        ).join(Order, OrderItem.order_id == Order.id).join(Product, OrderItem.product_id == Product.id).filter(
            Order.created_at >= start_date, Order.created_at <= end_date,
            ~Order.status.ilike('%iptal%'), ~Order.status.ilike('%cancel%'), ~Order.status.ilike('%iade%')
        )
        if current_user.id:
            cost_query = cost_query.filter(Order.user_id == current_user.id)
            
        total_cost = cost_query.scalar() or 0.0
        
        if total_cost == 0 and total_revenue > 0:
             total_cost = total_revenue * 0.75 # Fallback
    else:
        total_cost = total_revenue * 0.75
        
    profit = total_revenue - total_cost
    margin = (profit / total_revenue * 100) if total_revenue > 0 else 0.0
    
    # Breakdown by Marketplace
    mp_query = db.session.query(
        Order.marketplace,
        db.func.sum(Order.total_price),
        db.func.count(Order.id)
    ).filter(
        Order.created_at >= start_date, Order.created_at <= end_date,
        ~Order.status.ilike('%iptal%'), ~Order.status.ilike('%cancel%'), ~Order.status.ilike('%iade%')
    ).group_by(Order.marketplace)
    
    if current_user.id:
        mp_query = mp_query.filter(Order.user_id == current_user.id)
        
    mp_breakdown = mp_query.all()
    
    return render_template(
        'reports/profit_loss.html',
        start_date=start_date,
        end_date=end_date,
        total_revenue=total_revenue,
        total_cost=total_cost,
        profit=profit,
        margin=margin,
        mp_breakdown=mp_breakdown
    )
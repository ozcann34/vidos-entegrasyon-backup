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


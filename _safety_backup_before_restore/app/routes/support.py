from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from app.services.support_service import create_ticket, add_message, get_user_tickets, get_ticket_detail
from app.services.email_service import send_support_ticket_created_email, send_support_ticket_reply_email
from app.models.support import SupportTicket

support_bp = Blueprint('support', __name__, url_prefix='/support')

@support_bp.route('/')
@login_required
def index():
    """List user's support tickets."""
    tickets = get_user_tickets(current_user.id)
    return render_template('support/index.html', tickets=tickets)

@support_bp.route('/faq')
@login_required
def faq():
    """Frequently Asked Questions."""
    return render_template('support/faq.html')

@support_bp.route('/create', methods=['GET', 'POST'])
@login_required
def create():
    """Create a new support ticket."""
    if request.method == 'POST':
        subject = request.form.get('subject')
        message = request.form.get('message')
        file = request.files.get('file')
        
        if not subject or not message:
            flash('Lütfen konu ve mesaj alanlarını doldurun.', 'warning')
            return redirect(url_for('support.create'))
            
        ticket = create_ticket(current_user.id, subject, message, file)
        if ticket:
            send_support_ticket_created_email(current_user, ticket)
            flash('Destek talebiniz başarıyla oluşturuldu.', 'success')
            return redirect(url_for('support.index'))
        else:
            flash('Destek talebi oluşturulurken bir hata oluştu.', 'danger')
            
    return render_template('support/create.html')

@support_bp.route('/<int:ticket_id>', methods=['GET', 'POST'])
@login_required
def detail(ticket_id):
    """View ticket details and add reply."""
    ticket = get_ticket_detail(ticket_id, current_user.id)
    if not ticket:
        flash('Talep bulunamadı veya erişim yetkiniz yok.', 'danger')
        return redirect(url_for('support.index'))
        
    if request.method == 'POST':
        message = request.form.get('message')
        file = request.files.get('file')
        
        if message:
            add_message(ticket_id, current_user.id, message, is_admin=False, file=file)
            flash('Mesajınız gönderildi.', 'success')
            return redirect(url_for('support.detail', ticket_id=ticket_id))
            
    return render_template('support/detail.html', ticket=ticket)

@support_bp.route('/uploads/<filename>')
@login_required
def download_file(filename):
    """Download support attachment."""
    from flask import send_from_directory
    import os
    from flask import current_app
    
    upload_folder = os.path.join(current_app.root_path, '..', 'uploads', 'support_files')
    return send_from_directory(upload_folder, filename)

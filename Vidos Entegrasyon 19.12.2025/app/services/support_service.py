import os
from werkzeug.utils import secure_filename
from datetime import datetime
from app import db, create_app # create_app needed for config? Better usage below.
from flask import current_app
from app.models.support import SupportTicket, SupportMessage
from app.models.user import User

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'docx', 'doc', 'txt'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def save_attachment(file):
    """Save attachment and return relative path."""
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        # Create unique filename
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        unique_filename = f"{timestamp}_{filename}"
        
        # Ensure directory exists
        upload_folder = os.path.join(current_app.root_path, '..', 'uploads', 'support_files')
        os.makedirs(upload_folder, exist_ok=True)
        
        file.save(os.path.join(upload_folder, unique_filename))
        return unique_filename
    return None

def create_ticket(user_id, subject, message, file=None):
    """Create a new support ticket with optional file."""
    try:
        # Create ticket
        ticket = SupportTicket(
            user_id=user_id,
            subject=subject,
            status='open'
        )
        db.session.add(ticket)
        db.session.flush() # Get ID

        attachment_path = save_attachment(file) if file else None

        # Add initial message
        msg = SupportMessage(
            ticket_id=ticket.id,
            sender_id=user_id,
            message=message,
            attachment_path=attachment_path,
            is_admin_reply=False
        )
        db.session.add(msg)
        
        db.session.commit()
        return ticket
    except Exception as e:
        db.session.rollback()
        print(f"Error creating ticket: {e}")
        return None

def add_message(ticket_id, user_id, message, is_admin=False, file=None):
    """Add a message to a ticket with optional file."""
    try:
        ticket = SupportTicket.query.get(ticket_id)
        if not ticket:
            return None

        attachment_path = save_attachment(file) if file else None

        msg = SupportMessage(
            ticket_id=ticket_id,
            sender_id=user_id,
            message=message,
            attachment_path=attachment_path,
            is_admin_reply=is_admin
        )
        db.session.add(msg)
        
        # Update ticket status check logic
        ticket.updated_at = datetime.utcnow()
        if is_admin:
            ticket.status = 'answered'
        elif ticket.status == 'answered' or ticket.status == 'resolved':
             # If user replies, reopen if it was resolved/answered
            ticket.status = 'open'
            
        db.session.commit()
        return msg
    except Exception as e:
        db.session.rollback()
        print(f"Error adding message: {e}")
        return None

def update_ticket_status(ticket_id, status):
    """Update ticket status."""
    try:
        ticket = SupportTicket.query.get(ticket_id)
        if not ticket:
            return False
            
        ticket.status = status
        ticket.updated_at = datetime.utcnow()
        db.session.commit()
        return True
    except Exception as e:
        db.session.rollback()
        print(f"Error updating ticket status: {e}")
        return False

def get_user_tickets(user_id):
    """Get all tickets for a user."""
    return SupportTicket.query.filter_by(user_id=user_id).order_by(SupportTicket.updated_at.desc()).all()

def get_ticket_detail(ticket_id, user_id=None):
    """Get ticket detail. If user_id provided, ensures ownership."""
    query = SupportTicket.query.filter_by(id=ticket_id)
    if user_id:
        query = query.filter_by(user_id=user_id)
    return query.first()

def get_all_tickets_admin():
    """Get all tickets for admin."""
    return SupportTicket.query.order_by(SupportTicket.status == 'closed', SupportTicket.updated_at.desc()).all()

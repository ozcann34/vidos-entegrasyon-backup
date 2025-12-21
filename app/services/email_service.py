"""Email service for sending password reset and notification emails."""
import secrets
from datetime import datetime, timedelta
from typing import Optional, List
from flask import current_app, url_for, render_template_string
from flask_mail import Message
from app import mail, db
from app.models import User


def generate_token():
    """Generate a secure random token."""
    return secrets.token_urlsafe(32)


def send_verification_email(user: User) -> bool:
    """Send email verification link to new user."""
    try:
        verify_url = url_for('auth.verify_email', token=user.verification_token, _external=True)
        
        subject = "Vidos - Hesabınızı Doğrulayın"
        
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: 'Segoe UI', Arial, sans-serif; line-height: 1.6; color: #1e293b; background-color: #f8fafc; }}
                .container {{ max-width: 600px; margin: 40px auto; padding: 40px; background: #ffffff; border-radius: 24px; box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.05); }}
                .header {{ text-align: center; margin-bottom: 30px; }}
                .button {{ display: inline-block; background: #4f46e5; color: white !important; padding: 16px 32px; text-decoration: none; border-radius: 12px; font-weight: 600; margin: 30px 0; }}
                .footer {{ text-align: center; color: #64748b; font-size: 13px; margin-top: 40px; border-top: 1px solid #e2e8f0; padding-top: 20px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h2 style="color: #0f172a; margin-bottom: 8px;">Vidos'a Hoş Geldiniz!</h2>
                    <p style="color: #64748b;">E-ticaretinizi büyütmeye başlamak için hesabınızı doğrulayın.</p>
                </div>
                <div style="text-align: center;">
                    <p>Merhaba {user.first_name or user.email},</p>
                    <p>Kaydınızı tamamlamak için lütfen aşağıdaki butona tıklayarak e-posta adresinizi doğrulayın:</p>
                    <a href="{verify_url}" class="button">E-postamı Doğrula</a>
                    <p style="font-size: 14px; color: #94a3b8;">Buton çalışmıyorsa aşağıdaki bağlantıyı tarayıcınıza yapıştırabilirsiniz:</p>
                    <p style="font-size: 12px; color: #94a3b8; word-break: break-all;">{verify_url}</p>
                </div>
                <div class="footer">
                    <p>© {datetime.now().year} Vidos Entegrasyon. Tüm hakları saklıdır.</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        msg = Message(
            subject=subject,
            recipients=[user.email],
            html=html_body
        )
        
        if current_app.config.get('MAIL_USERNAME'):
            mail.send(msg)
            return True
        else:
            # Log to file if mail not configured (for dev)
            import os
            log_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'verification_links.log')
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(f"[{datetime.now()}] Verify {user.email}: {verify_url}\n")
            return True

    except Exception as e:
        import logging
        logging.exception("Failed to send verification email")
        return False


def verify_email_token(token: str) -> Optional[User]:
    """Verify email token and mark user as verified."""
    if not token:
        return None
    
    user = User.query.filter_by(verification_token=token).first()
    if user:
        user.is_verified = True
        user.verification_token = None
        db.session.commit()
        return user
    
    return None


def send_password_reset_email(user: User) -> bool:
    """
    Send password reset email to user.
    
    Args:
        user: User object to send reset email to
        
    Returns:
        True if email sent successfully, False otherwise
    """
    try:
        # Generate and save reset token
        token = generate_token()
        user.reset_token = token
        user.reset_token_expiry = datetime.utcnow() + timedelta(
            hours=current_app.config.get('PASSWORD_RESET_EXPIRY_HOURS', 24)
        )
        db.session.commit()
        
        # Build reset URL
        reset_url = url_for('auth.reset_password', token=token, _external=True)
        
        # Email content
        subject = "Vidos - Şifre Sıfırlama"
        
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; text-align: center; border-radius: 10px 10px 0 0; }}
                .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
                .button {{ display: inline-block; background: #667eea; color: white !important; padding: 15px 30px; text-decoration: none; border-radius: 5px; margin: 20px 0; }}
                .footer {{ text-align: center; color: #666; font-size: 12px; margin-top: 20px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>🔐 Şifre Sıfırlama</h1>
                </div>
                <div class="content">
                    <p>Merhaba {user.full_name or user.email},</p>
                    <p>Vidos hesabınız için şifre sıfırlama talebinde bulundunuz.</p>
                    <p>Şifrenizi sıfırlamak için aşağıdaki butona tıklayın:</p>
                    <p style="text-align: center;">
                        <a href="{reset_url}" class="button">Şifremi Sıfırla</a>
                    </p>
                    <p>Veya bu linki tarayıcınıza kopyalayın:</p>
                    <p style="word-break: break-all; background: #eee; padding: 10px; border-radius: 5px;">
                        {reset_url}
                    </p>
                    <p><strong>Bu link 24 saat geçerlidir.</strong></p>
                    <p>Eğer bu talebi siz yapmadıysanız, bu emaili görmezden gelebilirsiniz.</p>
                </div>
                <div class="footer">
                    <p>Bu email Vidos Entegrasyon sistemi tarafından otomatik olarak gönderilmiştir.</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        text_body = f"""
Merhaba {user.full_name or user.email},

Vidos hesabınız için şifre sıfırlama talebinde bulundunuz.

Şifrenizi sıfırlamak için bu linke tıklayın:
{reset_url}

Bu link 24 saat geçerlidir.

Eğer bu talebi siz yapmadıysanız, bu emaili görmezden gelebilirsiniz.

---
Vidos Entegrasyon
        """
        
        # Check if mail is configured
        if not current_app.config.get('MAIL_USERNAME'):
            log_message = f"[{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}] ⚠️ Email yapılandırılmamış.\n   Kullanıcı: {user.email}\n   Reset URL: {reset_url}\n\n"
            print(log_message)
            
            # Also write to a file for easy access
            try:
                import os
                log_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'reset_links.log')
                with open(log_file, 'a', encoding='utf-8') as f:
                    f.write(log_message)
                print(f"   📁 Reset linki dosyaya yazıldı: reset_links.log")
            except Exception as log_err:
                print(f"   ⚠️ Log dosyasına yazılamadı: {log_err}")
            
            return True  # Return true so user gets success message
        
        msg = Message(
            subject=subject,
            recipients=[user.email],
            html=html_body,
            body=text_body
        )
        
        mail.send(msg)
        print(f"✅ Şifre sıfırlama emaili gönderildi: {user.email}")
        return True
        
    except Exception as e:
        print(f"❌ Email gönderme hatası: {str(e)}")
        db.session.rollback()
        return False


def verify_reset_token(token: str) -> User:
    """
    Verify password reset token and return user if valid.
    
    Args:
        token: Reset token from URL
        
    Returns:
        User object if token is valid, None otherwise
    """
    if not token:
        return None
    
    user = User.query.filter_by(reset_token=token).first()
    
    if not user:
        return None
    
    # Check expiry
    if user.reset_token_expiry and user.reset_token_expiry < datetime.utcnow():
        # Token expired, clear it
        user.reset_token = None
        user.reset_token_expiry = None
        db.session.commit()
        return None
    
    return user


def clear_reset_token(user: User):
    """Clear password reset token for a user."""
    user.reset_token = None
    user.reset_token_expiry = None
    db.session.commit()


def send_support_ticket_created_email(user: User, ticket) -> bool:
    """Send email to user when they create a support ticket."""
    try:
        subject = f"Destek Talebiniz Alındı: #{ticket.id}"
        
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background: #2c3e50; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }}
                .content {{ background: #f9f9f9; padding: 20px; border: 1px solid #ddd; border-top: none; border-radius: 0 0 5px 5px; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h2>Destek Talebiniz Alındı</h2>
                </div>
                <div class="content">
                    <p>Merhaba {user.full_name or user.email},</p>
                    <p>Destek talebiniz başarıyla oluşturulmuştur. Ekibimiz en kısa sürede inceleyip size dönüş yapacaktır.</p>
                    <p><strong>Talep No:</strong> #{ticket.id}<br>
                    <strong>Konu:</strong> {ticket.subject}</p>
                    <p>Talebinizin durumunu panelinizden takip edebilirsiniz.</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        msg = Message(
            subject=subject,
            recipients=[user.email],
            html=html_body
        )
        
        if current_app.config.get('MAIL_USERNAME'):
            mail.send(msg)
            return True
        else:
            print(f"User email notification simulated for ticket #{ticket.id}")
            return True
            
    except Exception as e:
        print(f"Email error: {e}")
        return False

def send_support_ticket_reply_email(user: User, ticket, message_content) -> bool:
    """Send email to user when admin replies."""
    try:
        subject = f"Destek Talebiniz Hakkında: #{ticket.id}"
        
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background: #27ae60; color: white; padding: 20px; text-align: center; border-radius: 5px 5px 0 0; }}
                .content {{ background: #f9f9f9; padding: 20px; border: 1px solid #ddd; border-top: none; border-radius: 0 0 5px 5px; }}
                .message-box {{ background: white; padding: 15px; border-left: 4px solid #27ae60; margin: 15px 0; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h2>Yeni Cevap</h2>
                </div>
                <div class="content">
                    <p>Merhaba {user.full_name or user.email},</p>
                    <p>Destek talebinize yeni bir cevap verildi:</p>
                    <div class="message-box">
                        {message_content}
                    </div>
                </div>
            </div>
        </body>
        </html>
        """
        
        msg = Message(
            subject=subject,
            recipients=[user.email],
            html=html_body
        )
        
        if current_app.config.get('MAIL_USERNAME'):
            mail.send(msg)
            return True
        else:
            print(f"User reply notification simulated for ticket #{ticket.id}")
            return True
            
    except Exception as e:
        print(f"Email error: {e}")
        return False

def send_support_ticket_resolved_email(user: User, ticket) -> bool:
    """Send email when ticket is resolved."""
    try:
        subject = f"Destek Talebiniz Çözüldü: #{ticket.id}"
        
        html_body = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
        </head>
        <body>
            <p>Merhaba {user.full_name or user.email},</p>
            <p>#{ticket.id} numaralı destek talebiniz "Çözüldü" olarak işaretlenmiştir.</p>
            <p>Eğer sorununuz devam ediyorsa lütfen tekrar iletişime geçin.</p>
        </body>
        </html>
        """
        
        msg = Message(
            subject=subject,
            recipients=[user.email],
            html=html_body
        )
        
        if current_app.config.get('MAIL_USERNAME'):
            mail.send(msg)
            return True
        else:
             print(f"User resolved notification simulated for ticket #{ticket.id}")
             return True
            
    except Exception as e:
        print(f"Email error: {e}")
        return False

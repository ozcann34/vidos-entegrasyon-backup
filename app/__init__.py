from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_mail import Mail
from flask_migrate import Migrate
from config import config
from sqlalchemy import MetaData
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

naming_convention = {
    "ix": 'ix_%(column_0_label)s',
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s"
}

db = SQLAlchemy(metadata=MetaData(naming_convention=naming_convention))
migrate = Migrate()
login_manager = LoginManager()
mail = Mail()

# Configure login manager
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Bu sayfaya erişmek için giriş yapmalısınız.'
login_manager.login_message_category = 'warning'


def create_app(config_name='default'):
    app = Flask(__name__, template_folder='../templates', static_folder='../static')
    app.config.from_object(config[config_name])

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    mail.init_app(app)
    
    # Set custom Anonymous User class
    from app.models.user import AnonymousUser
    login_manager.anonymous_user = AnonymousUser
    
    # User loader for Flask-Login
    @login_manager.user_loader
    def load_user(user_id):
        from app.models import User
        return User.query.get(int(user_id))

    from app.routes.api import api_bp
    from app.routes.main import main_bp
    from app.routes.auth import auth_bp
    from app.routes.admin import admin_bp
    from app.routes.payment import payment_bp

    app.register_blueprint(api_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(payment_bp)

    from app.routes.support import support_bp
    app.register_blueprint(support_bp)
    
    from app.routes.report import report_bp
    app.register_blueprint(report_bp)

    from app.routes.error_handlers import errors_bp
    app.register_blueprint(errors_bp)

    from app.routes.products import products_bp
    app.register_blueprint(products_bp)
    
    # Ban check middleware
    @app.before_request
    def check_banned_user():
        """Check if logged-in user is banned before each request."""
        from flask_login import current_user
        from flask import redirect, url_for, session, request
        from flask_login import logout_user
        
        if current_user.is_authenticated:
            # 1. Ban check
            if current_user.is_banned:
                session['banned_email'] = current_user.email
                session['ban_reason'] = current_user.ban_reason
                logout_user()
                return redirect(url_for('auth.banned'))
            
            # 2. Email verification check (Force OTP)
            # Allow access to auth blueprints and static files
            if not current_user.is_email_verified and not current_user.is_admin:
                allowed_endpoints = ['auth.verify_email', 'auth.resend_otp', 'auth.logout', 'static']
                if request.endpoint and request.endpoint not in allowed_endpoints and not request.endpoint.startswith('auth.'):
                    return redirect(url_for('auth.verify_email'))

    
    # Initialize scheduler for auto sync
    with app.app_context():
        # Create database tables
        try:
            from sqlalchemy.exc import IntegrityError, ProgrammingError
            db.create_all()
        except (IntegrityError, ProgrammingError):
            pass # Race condition handling for multiple workers
        except Exception as e:
            print(f"DB Error: {e}")

        # Create admin user if not exists
        try:
            from app.services.user_service import create_admin_user_if_not_exists
            create_admin_user_if_not_exists()
        except Exception:
            pass
        
        # Start scheduler
        try:
            from app.services.scheduler_service import init_scheduler
            init_scheduler(app)
        except Exception as e:
            print(f"Scheduler Error: {e}")

    return app

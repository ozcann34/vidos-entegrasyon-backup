import os

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    # Use environment variable for DB if available (important for production)
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or f"sqlite:///{os.path.join(BASE_DIR, 'panel.db')}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Marketplace Settings
    TRENDYOL_SNAPSHOT_TTL = int(os.environ.get("TRENDYOL_SNAPSHOT_TTL", "300"))
    MP_MAX_JOBS = int(os.environ.get("MP_MAX_JOBS", "120"))
    
    # Email Settings (Flask-Mail)
    MAIL_SERVER = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
    MAIL_PORT = int(os.environ.get('MAIL_PORT', 587))
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'true').lower() in ('true', '1', 'yes')
    MAIL_USE_SSL = os.environ.get('MAIL_USE_SSL', 'false').lower() in ('true', '1', 'yes')
    MAIL_USERNAME = os.environ.get('MAIL_USERNAME', '')
    MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD', '')
    MAIL_DEFAULT_SENDER = os.environ.get('MAIL_DEFAULT_SENDER', 'noreply@vidos.com')
    
    # Password Reset Settings
    PASSWORD_RESET_EXPIRY_HOURS = 24

class DevelopmentConfig(Config):
    DEBUG = True

class ProductionConfig(Config):
    DEBUG = False

config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}

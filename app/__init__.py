from flask import Flask
from datetime import datetime
from app.config import Config


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    from app.auth import auth_bp
    from app.routes_trades import trades_bp
    from app.routes_views import views_bp
    from app.routes_api import api_bp

    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(trades_bp, url_prefix='/trades')
    app.register_blueprint(views_bp, url_prefix='/')
    app.register_blueprint(api_bp, url_prefix='/api')

    # Jinja2 globals and filters
    app.jinja_env.globals['now'] = datetime.utcnow
    app.jinja_env.globals['enumerate'] = enumerate
    app.jinja_env.globals['zip'] = zip

    @app.template_filter('currency')
    def currency_filter(value):
        if value is None:
            return 'N/A'
        try:
            v = float(value)
            sign = '+' if v >= 0 else ''
            return f'{sign}${v:,.2f}'
        except (TypeError, ValueError):
            return 'N/A'

    @app.template_filter('pct')
    def pct_filter(value, decimals=2):
        if value is None:
            return 'N/A'
        try:
            v = float(value)
            sign = '+' if v >= 0 else ''
            return f'{sign}{v:.{decimals}f}%'
        except (TypeError, ValueError):
            return 'N/A'

    @app.template_filter('abs_val')
    def abs_val_filter(value):
        if value is None:
            return None
        try:
            return abs(float(value))
        except (TypeError, ValueError):
            return None

    @app.template_filter('fmt_date')
    def fmt_date_filter(value):
        if not value:
            return 'N/A'
        try:
            if isinstance(value, str):
                value = datetime.strptime(value[:10], '%Y-%m-%d')
            return value.strftime('%m/%d/%y')
        except (ValueError, AttributeError):
            return str(value)[:10]

    return app

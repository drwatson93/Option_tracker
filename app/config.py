import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY: str = os.environ.get('SECRET_KEY', 'dev-secret-change-me')
    SECRET_PASSWORD: str = os.environ.get('SECRET_PASSWORD', '')
    SUPABASE_URL: str = os.environ.get('SUPABASE_URL', '')
    SUPABASE_SERVICE_ROLE_KEY: str = os.environ.get('SUPABASE_SERVICE_ROLE_KEY', '')
    ANTHROPIC_API_KEY: str = os.environ.get('ANTHROPIC_API_KEY', '')
    MAX_CONTENT_LENGTH: int = 16 * 1024 * 1024  # 16 MB upload limit

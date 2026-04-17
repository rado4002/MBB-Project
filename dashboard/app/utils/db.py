"""Dashboard DB connection utility — sync SQLAlchemy for Streamlit."""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

_DB_URL = (
    f"postgresql://{os.getenv('POSTGRES_USER', 'mbb')}:{os.getenv('POSTGRES_PASSWORD', '')}"
    f"@{os.getenv('POSTGRES_HOST', 'postgres')}:{os.getenv('POSTGRES_PORT', '5432')}"
    f"/{os.getenv('POSTGRES_DB', 'mbb')}"
)

engine = create_engine(_DB_URL, pool_pre_ping=True, pool_size=5)
SessionLocal = sessionmaker(bind=engine)


def get_session():
    return SessionLocal()

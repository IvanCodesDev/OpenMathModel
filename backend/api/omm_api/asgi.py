"""uvicorn 入口：uvicorn app.asgi:app --reload --port 8000"""

from .main import create_app

app = create_app()

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SNU_DB=/tmp/snu.db \
    SNU_FRONTEND_DIR=/code/frontend-dist

WORKDIR /build/frontend
COPY frontend/ ./
RUN python build_frontend.py && mkdir -p /code/frontend-dist \
    && cp /build/frontend/dist/index.html /code/frontend-dist/index.html

WORKDIR /code
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app

EXPOSE 8000
CMD ["sh", "-c", "python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]

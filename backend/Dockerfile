FROM python:3.11-slim

WORKDIR /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV SNU_DB=/data/snu.db
VOLUME ["/data"]

EXPOSE 8000

# `python -m uvicorn` (not the bare `uvicorn` entrypoint) so cwd (/code) is on
# sys.path and `app.main` resolves the same way scripts/run-e2e.sh runs it locally.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN useradd --create-home appuser \
    && mkdir -p /app/logs \
    && chown -R appuser /app
USER appuser

ENV PORT=5000 \
    LOG_LEVEL=INFO \
    LOG_FILE=/app/logs/app.log

EXPOSE 5000

CMD ["sh", "-c", "exec gunicorn --workers 1 --threads 4 --bind 0.0.0.0:${PORT} app:app"]

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=10000

WORKDIR /app

RUN useradd --system --uid 10001 --create-home aurix-ai

COPY aurix_ai /app/aurix_ai
COPY migrations.py persistence.py telegram_web_app.py /app/
COPY scripts/aurix_ai_keys.py scripts/aurix_ai_reconcile.py /app/
COPY web/ai-app /app/web/ai-app

# Optional shared AuriX PostgreSQL storage. SQLite remains the default when
# AURIX_AI_DATABASE_URL is unset.
RUN pip install --no-cache-dir "psycopg[binary]>=3.1,<4" "psycopg-pool>=3.2,<4"

RUN chown -R aurix-ai:aurix-ai /app
USER aurix-ai

EXPOSE 10000
CMD ["python", "-u", "-m", "aurix_ai"]

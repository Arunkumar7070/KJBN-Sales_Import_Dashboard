FROM python:3.12-slim

# Logs go straight to the container output; no .pyc files in the image.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Migrate and seed (safe on every start), then serve the API.
CMD ["sh", "-c", "sh scripts/migrate_and_seed.sh && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]

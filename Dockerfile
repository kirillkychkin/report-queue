# Единый образ для api, воркеров Celery/RQ, beat, flower и бенчмарков.
# Команда запуска задаётся в docker-compose.yml.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Зависимости отдельно от кода — слой кэшируется
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Непривилегированный пользователь; каталог артефактов монтируется как volume
RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data/artifacts \
    && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

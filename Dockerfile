FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Зависимости отдельным слоем: пересобираются только при смене requirements.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Процесс не должен работать от root: при компрометации контейнера это
# ограничивает возможности атакующего.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "app.main"]

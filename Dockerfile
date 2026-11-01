FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CAMOUFOX_VIRTUAL_DISPLAY_SIZE=1920x1080x24

# xvfb + setpriv нужны для headless="virtual" (headed-браузер на виртуальном дисплее)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xvfb util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# системные библиотеки для Firefox (ставит сам Playwright)
RUN playwright install-deps firefox

# скачиваем бинарник Camoufox в образ, чтобы не тянуть его при старте
RUN camoufox fetch

COPY app.py .

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.11-slim

# Зависимости системы, необходимые Chromium (Playwright)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg curl xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Устанавливаем Chromium и его системные зависимости
RUN playwright install --with-deps chromium

COPY app ./app
COPY test_page.html ./test_page.html

RUN mkdir -p /srv/data /srv/pw_profile

EXPOSE 8000

# Для первичного ручного входа (HEADLESS=false) требуется X11-дисплей —
# запускаем через xvfb-run, чтобы можно было подключиться по VNC/X11-forwarding
# при необходимости; при HEADLESS=true xvfb не мешает работе.
CMD ["xvfb-run", "-a", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

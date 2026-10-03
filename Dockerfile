# syntax=docker/dockerfile:1
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    PYTHONDONTWRITEBYTECODE=1

# Системный Chromium + ffmpeg (из apt) + curl (для диагностики)
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium ffmpeg curl ca-certificates tini \
    && apt-get autoremove -y && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* \
    && rm -rf /usr/share/doc /usr/share/man /usr/share/locale /usr/share/info /usr/share/fonts/truetype/noto

WORKDIR /app

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt
# Снимаем pip/setuptools/wheel после установки. В рантайме они не нужны:
# код ничего не запускает через subprocess pip. Экономия ~15 МБ.
RUN pip uninstall -y pip setuptools wheel && rm -rf /usr/local/lib/python3.11/ensurepip

# Исходный код
COPY routers/*.py ./routers/
COPY services/*.py ./services/
COPY core/*.py ./core/
COPY web ./web
COPY restart_flaresolverr.sh .
RUN mkdir -p /app/db /app/logs

EXPOSE 8000

# tini-init-v1
# tini как PID 1: собирает осиротевших детей (Chromium, Playwright,
# ffmpeg) и не даёт копиться зомби. Без него uvicorn/PID 1 не вызывает
# waitpid, и дети зомбируются.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "core.main:app", "--host", "0.0.0.0", "--port", "8000"]

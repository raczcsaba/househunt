FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    TZ=Europe/Budapest \
    DISPLAY=:99 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Virtual screen (Xvfb), VNC server and the noVNC web client for the "solve the check" browser view.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tzdata ca-certificates xvfb x11vnc novnc websockify fluxbox fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt playwright==1.60.0 \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY config ./config
COPY plugins ./plugins
COPY start.sh /start.sh
RUN chmod +x /start.sh

VOLUME ["/data"]
EXPOSE 8080 6080
CMD ["/start.sh"]

#!/bin/sh
# Starts the virtual screen + browser view, then the dashboard and scheduler.
set -e

if [ -z "$VNC_PASSWORD" ]; then
  echo "VNC_PASSWORD is not set; the browser view on port 6080 is disabled." >&2
  echo "Set it in .env if a job plugin needs you to complete a check in the browser view." >&2
fi

Xvfb :99 -screen 0 1280x900x24 -nolisten tcp >/dev/null 2>&1 &
sleep 1
fluxbox >/dev/null 2>&1 &

if [ -n "$VNC_PASSWORD" ]; then
  mkdir -p /root/.vnc
  x11vnc -storepasswd "$VNC_PASSWORD" /root/.vnc/passwd >/dev/null 2>&1
  x11vnc -display :99 -rfbauth /root/.vnc/passwd -forever -shared -localhost -rfbport 5900 -quiet >/dev/null 2>&1 &
  websockify --web /usr/share/novnc 6080 localhost:5900 >/dev/null 2>&1 &
fi

exec python -m app.main serve

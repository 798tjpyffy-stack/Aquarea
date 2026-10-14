FROM ghcr.io/home-assistant/base:latest

ARG BUILD_VERSION=0.1.0
ARG BUILD_ARCH=amd64

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apk add --no-cache python3

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY app/ /app/
COPY run.sh /run.sh
RUN chmod a+x /run.sh

LABEL \
    io.hass.name="Aquarea Dashboard" \
    io.hass.description="Panasonic Aquarea heat pump dashboard" \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="aarch64|amd64"

CMD [ "/run.sh" ]

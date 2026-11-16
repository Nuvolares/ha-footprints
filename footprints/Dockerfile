ARG BUILD_FROM=ghcr.io/home-assistant/base:3.24
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-aiohttp curl

ARG BUILD_VERSION
ENV BUILD_VERSION=${BUILD_VERSION}

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

# Leaflet (BSD-2-Clause), bundled so the page doesn't depend on a CDN.
ARG LEAFLET_VERSION=1.9.4
RUN mkdir -p /app/static/vendor/images \
    && base="https://cdn.jsdelivr.net/npm/leaflet@${LEAFLET_VERSION}/dist" \
    && curl -fsSL -o /app/static/vendor/leaflet.js "${base}/leaflet.js" \
    && curl -fsSL -o /app/static/vendor/leaflet.css "${base}/leaflet.css" \
    && for f in layers.png layers-2x.png marker-icon.png marker-icon-2x.png marker-shadow.png; do \
        curl -fsSL -o "/app/static/vendor/images/${f}" "${base}/images/${f}" || exit 1; \
    done

CMD ["/run.sh"]

FROM python:3.13-slim@sha256:e544a7fcbdf8555eceda66bf86cafb006c736339f76141918bcb812f3174c00a

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/mock-package \
    && openssl req -x509 -newkey rsa:2048 -nodes -days 2 \
       -keyout /opt/mock-package/key.pem \
       -out /opt/mock-package/cert.pem \
       -subj /CN=packages.test \
       -addext subjectAltName=DNS:packages.test \
    && chown -R 65532:65532 /opt/mock-package \
    && chmod 0400 /opt/mock-package/key.pem

COPY --chown=65532:65532 docker/mock-package/server.py /opt/mock-package/server.py

USER 65532:65532
WORKDIR /opt/mock-package
CMD ["python", "server.py"]

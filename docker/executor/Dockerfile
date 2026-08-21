FROM python:3.13-slim@sha256:e544a7fcbdf8555eceda66bf86cafb006c736339f76141918bcb812f3174c00a

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates git ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 mewcode \
    && useradd --uid 65532 --gid 65532 --create-home --home-dir /home/mewcode mewcode \
    && mkdir -p /opt/mewcode /workspace \
    && chown -R 65532:65532 /opt/mewcode /workspace /home/mewcode

COPY --chown=65532:65532 mewcode/platform/execution/workspace_helper.py /opt/mewcode/workspace_helper.py

USER 65532:65532
WORKDIR /workspace
CMD ["sleep", "infinity"]


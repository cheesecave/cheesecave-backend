FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install uv
RUN pip install --no-cache-dir uv

WORKDIR /app

COPY ./pyproject.toml .
RUN mkdir -p /app/src/kohakuhub
RUN echo "" > /app/src/kohakuhub/__init__.py
RUN uv pip install --system -e .

COPY ./src/kohakuhub ./src/kohakuhub
COPY ./scripts ./scripts
COPY ./docker/startup.py /app/startup.py
RUN chmod +x /app/startup.py

# Which code this image runs, reported by /api/version:
#   docker compose build --build-arg KOHAKU_HUB_GIT_SHA=$(git rev-parse HEAD) \
#     --build-arg KOHAKU_HUB_BUILD_TIME=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ARG KOHAKU_HUB_GIT_SHA=
ARG KOHAKU_HUB_BUILD_TIME=
ENV KOHAKU_HUB_GIT_SHA=${KOHAKU_HUB_GIT_SHA} KOHAKU_HUB_BUILD_TIME=${KOHAKU_HUB_BUILD_TIME}

EXPOSE 48888
CMD ["/app/startup.py"]

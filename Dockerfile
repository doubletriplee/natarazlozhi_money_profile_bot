FROM python:3.14-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir --upgrade "pip>=26.2,<27" \
    && python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      fonts-dejavu-core fonts-liberation2 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --home-dir /app app
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --upgrade "pip>=26.2,<27" \
    && python -m pip install --no-cache-dir --no-index --find-links=/wheels \
      natarazlozhi-money-profile-bot \
    && python -m pip uninstall -y pip setuptools wheel \
    && rm -rf /wheels
WORKDIR /app
COPY --chown=app:app . .
RUN mkdir -p /data/cards && chown -R app:app /data
USER 10001:10001
EXPOSE 8080
CMD ["money-profile-bot"]

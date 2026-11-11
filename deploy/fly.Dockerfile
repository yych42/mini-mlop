# mini-mlop server image for Fly.io (used by fly.toml at the repository root).
# Same as deploy/Dockerfile, except that Fly volumes are mounted owned by root,
# so the container starts as root, hands /data to the mini-mlop user, then
# drops to that user before starting the server.
FROM python:3.13-alpine

RUN apk add --no-cache su-exec && adduser -D -u 1000 mini-mlop
COPY mini_mlop_server /app/mini_mlop_server

WORKDIR /app
ENV MINI_MLOP_DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 2718

CMD ["sh", "-c", "chown -R mini-mlop /data && exec su-exec mini-mlop python -m mini_mlop_server"]

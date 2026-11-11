# mlop server: the image is Python plus this repository's mini_mlop_server package.
# Build from the repository root:  docker build -f deploy/Dockerfile -t mini-mlop-server .
FROM python:3.13-alpine

COPY mini_mlop_server /app/mini_mlop_server

RUN adduser -D -u 1000 mlop && mkdir /data && chown mlop /data
USER mlop
WORKDIR /app
ENV MLOP_DATA_DIR=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 2718

HEALTHCHECK --interval=30s --timeout=3s \
  CMD wget -qO- http://127.0.0.1:2718/api/health || exit 1

ENTRYPOINT ["python", "-m", "mini_mlop_server"]

# Sandbox runner for the mini-mlop assistant's analysis code.
# Deploy it somewhere that holds nothing you care about, NOT next to your data.
# Build from the repository root:
#   docker build -f deploy/sandbox.Dockerfile -t mini-mlop-sandbox .
FROM python:3.13-slim
RUN pip install --no-cache-dir numpy scipy pandas
WORKDIR /app
COPY mini_mlop_server/__init__.py mini_mlop_server/sandbox.py mini_mlop_server/sandbox_runner.py /app/mini_mlop_server/
RUN useradd --create-home --uid 1000 runner
USER runner
ENV PYTHONUNBUFFERED=1 PORT=8700
EXPOSE 8700
# MLOP_SANDBOX_TOKEN must be set at run time.
ENTRYPOINT ["python", "-m", "mini_mlop_server.sandbox_runner"]

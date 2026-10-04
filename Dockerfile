FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && useradd --uid 10001 --create-home gateway
COPY config ./config
RUN mkdir -p operations/evaluations && chown -R gateway:gateway operations
USER gateway
ENV GATEWAY_HOST=0.0.0.0 GATEWAY_PORT=35000 GATEWAY_CONFIG_PATH=config/gateway.docker.yaml
EXPOSE 35000
CMD ["python", "-m", "llm_gateway.main"]

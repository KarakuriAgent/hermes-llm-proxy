FROM nousresearch/hermes-agent:latest

ENTRYPOINT []

WORKDIR /app

COPY hermes_llm_proxy /app/hermes_llm_proxy

ENV HERMES_SRC=/opt/hermes \
    HERMES_HOME=/opt/data \
    HOME=/opt/data/home \
    PYTHONPATH=/opt/hermes:/app \
    PYTHONDONTWRITEBYTECODE=1

CMD ["/opt/hermes/.venv/bin/python", "-m", "hermes_llm_proxy.server"]

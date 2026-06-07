FROM nousresearch/hermes-agent:latest

ENTRYPOINT []

WORKDIR /app

COPY hermes_llm_proxy /app/hermes_llm_proxy

ENV HERMES_SRC=/opt/hermes \
    HERMES_HOME=/opt/data \
    HOME=/opt/data/home \
    PYTHONDONTWRITEBYTECODE=1

CMD ["sh", "-c", "export PATH=\"$HERMES_SRC/.venv/bin:$PATH\" PYTHONPATH=\"$HERMES_SRC:/app${PYTHONPATH:+:$PYTHONPATH}\"; exec \"$HERMES_SRC/.venv/bin/python\" -m hermes_llm_proxy.server"]

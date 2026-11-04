# Build context is docs/samples/ASE, not the repository root.
# Supply approved, scanned digest-pinned images from the existing registry.
ARG UV_IMAGE
ARG PYTHON_IMAGE
FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /solution
COPY ase_document_tools.py /solution/ase_document_tools.py
COPY synapse_worker /solution/synapse_worker
RUN test -f synapse_worker/uv.lock \
    && cd synapse_worker \
    && uv sync --locked --extra worker --no-dev --no-editable \
    && uv build --wheel --out-dir /wheels

FROM ${PYTHON_IMAGE}
USER root
# Microsoft ODBC repository/keyring must be pre-approved in the base image.
# Build-time repositories must be available to the approved builder, never at startup.
RUN apt-get update && ACCEPT_EULA=Y apt-get install -y --no-install-recommends \
    msodbcsql18 unixodbc libreoffice-writer fonts-dejavu-core ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home processor
COPY --from=build /solution/synapse_worker/.venv /solution/synapse_worker/.venv
COPY synapse_worker/config.example.json /solution/config.json
ENV PATH="/solution/synapse_worker/.venv/bin:${PATH}" \
    DOCUMENT_WORKER_CONFIG="/solution/config.json" \
    PORT="8080" \
    PYTHONUNBUFFERED="1" \
    PYTHONDONTWRITEBYTECODE="1"
USER 10001
EXPOSE 8080
ENTRYPOINT ["presidio-document-worker"]

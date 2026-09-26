# syntax=docker/dockerfile:1
# Image for the Windows setup: the official Open Notebook 1.14.0 image with this
# fork's changes swapped in. Build context is the repository root; build with
# build.ps1 or `docker compose build open_notebook` from this folder.
#
# Stage 1 rebuilds the Next.js front end from this fork's source (the steps
# mirror the repository's own Dockerfile). Stage 2 starts from the official
# image, pinned by digest, and replaces the front end and the changed backend
# files. The image's Python environment is otherwise untouched.

FROM node:22-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm config set fetch-retries 5 \
 && npm config set fetch-retry-mintimeout 20000 \
 && npm config set fetch-retry-maxtimeout 120000 \
 && npm ci
COPY frontend/ ./
ENV NEXT_TELEMETRY_DISABLED=1
RUN npm run build

# lfnovo/open_notebook:v1-latest as pulled on 2026-09-14 (app version 1.14.0),
# pinned by digest so the base cannot change underneath the copied files.
FROM lfnovo/open_notebook@sha256:e53f90d6153fcf4a64604d9a0c12cb0428a32cfb8dbcbb72e81ab12c013ee330

RUN rm -rf /app/frontend/.next /app/frontend/node_modules /app/frontend/server.js
COPY --from=frontend-builder /app/frontend/.next/standalone /app/frontend/
COPY --from=frontend-builder /app/frontend/.next/static /app/frontend/.next/static
COPY --from=frontend-builder /app/frontend/public /app/frontend/public
COPY --from=frontend-builder /app/frontend/start-server.js /app/frontend/start-server.js

# Backend files this fork changes or adds.
COPY api/main.py api/models.py /app/api/
COPY api/routers/batch_transformations.py api/routers/llm_control.py api/routers/sources.py api/routers/vector_index.py /app/api/routers/
COPY commands/embedding_commands.py /app/commands/
COPY scripts/inbox_watcher.py /app/scripts/
COPY open_notebook/domain/notebook.py /app/open_notebook/domain/
COPY open_notebook/graphs/source.py /app/open_notebook/graphs/
COPY open_notebook/utils/text_sanitize.py /app/open_notebook/utils/
COPY open_notebook/vector_index/ /app/open_notebook/vector_index/
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY supervisord.conf /app/supervisord.conf

# TurboVec, the in-process vector index, installed into the app's environment.
# The build fails if the wheel does not install or import.
RUN uv pip install --python /app/.venv/bin/python --no-cache "turbovec==1.0.0" \
 && /app/.venv/bin/python -c "import turbovec, numpy; turbovec.IdMapIndex(dim=8, bit_width=4); print('turbovec', turbovec.__version__, 'numpy', numpy.__version__)"

# Fail the build, not the container, if a copied Python file does not parse or
# the index package does not import.
RUN /app/.venv/bin/python -c "import ast; [ast.parse(open(p).read(), p) for p in ('/app/api/main.py', '/app/api/models.py', '/app/api/routers/llm_control.py', '/app/api/routers/batch_transformations.py', '/app/api/routers/vector_index.py', '/app/api/routers/sources.py', '/app/commands/embedding_commands.py', '/app/scripts/inbox_watcher.py', '/app/open_notebook/graphs/source.py', '/app/open_notebook/utils/text_sanitize.py', '/app/open_notebook/domain/notebook.py', '/app/open_notebook/vector_index/ids.py', '/app/open_notebook/vector_index/store.py', '/app/open_notebook/vector_index/search.py', '/app/open_notebook/vector_index/client.py')]"
RUN cd /app && /app/.venv/bin/python -c "import open_notebook.vector_index.ids, open_notebook.vector_index.store, open_notebook.vector_index.search, open_notebook.vector_index.client; print('vector_index imports ok')"

LABEL org.opencontainers.image.title="open-notebook-custom" \
      org.opencontainers.image.description="Open Notebook 1.14.0 with local changes: model-memory toggle, inbox drop folder, CSV/TSV upload, insights not auto-embedded, null characters stripped, batch transformations, TurboVec vector index, Missing rebuild mode, embedded mark on insights" \
      org.opencontainers.image.base.name="docker.io/lfnovo/open_notebook:v1-latest"

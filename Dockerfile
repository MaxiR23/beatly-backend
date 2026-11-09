# Build stage: install the runtime dependencies into a venv. The final stage
# copies only that venv, so pip's cache and any build leftovers stay behind.
FROM python:3.14-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
RUN /opt/venv/bin/pip install -r /tmp/requirements.txt

FROM python:3.14-slim
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin beatly
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
# An explicit list, never `COPY . .`: a .env can not end up in the image even
# if .dockerignore is wrong.
COPY app.py ./
COPY core ./core
COPY models ./models
COPY routes ./routes
COPY services ./services
USER 10001
EXPOSE 8000
# `sh -c` expands ${PORT:-8000}; `exec` replaces the shell with uvicorn, so
# uvicorn is PID 1 and receives SIGTERM. WEB_CONCURRENCY and
# FORWARDED_ALLOW_IPS are read by uvicorn from the environment.
# --timeout-graceful-shutdown 8: docker stop and Cloud Run send SIGKILL after
# 10 s, so uvicorn must give up on slow requests before that.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port \"${PORT:-8000}\" --timeout-graceful-shutdown 8"]

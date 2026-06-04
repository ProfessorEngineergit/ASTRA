FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

RUN pip install --no-cache-dir uv

# Copy project + source, then install. Base deps must succeed; the optional
# memory extra (mem0ai) is best-effort so a heavy/flaky build never blocks boot.
COPY pyproject.toml ./
COPY app ./app
RUN uv pip install --system . \
 && (uv pip install --system mem0ai || echo "mem0ai optional — skipped, memory layer will no-op")

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

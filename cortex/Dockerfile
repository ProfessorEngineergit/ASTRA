FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

# Copy project + source, then install. Base deps must succeed; the optional
# extras (mem0ai memory, edupage-api timetable) are best-effort so a heavy/flaky
# build never blocks boot — the matching feature simply no-ops when absent.
COPY pyproject.toml ./
COPY app ./app
RUN uv pip install --system . \
 && (uv pip install --system mem0ai || echo "mem0ai optional — skipped, memory layer will no-op") \
 && (uv pip install --system edupage-api || echo "edupage-api optional — skipped, timetable will no-op")

# Durable, editable knowledge lives here (mounted as a volume in compose).
RUN mkdir -p /srv/data

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

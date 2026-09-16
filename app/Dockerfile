# One image, two roles: `app.py` (default CMD) and `loadgen.py` (overridden
# command in docker-compose.yml). Keeps the demo to a single build.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

WORKDIR /app

# Copy requirements first so the dependency layer is cached across code edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py loadgen.py ./

# Drop root. Nothing here needs to write to the image filesystem.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

CMD ["python", "app.py"]

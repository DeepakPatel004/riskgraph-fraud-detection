FROM python:3.14-slim

ARG INSTALL_ML=false
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY backend/requirements.txt backend/requirements-ml.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && case "$INSTALL_ML" in \
       true) apt-get update && apt-get install -y --no-install-recommends libgomp1 \
             && rm -rf /var/lib/apt/lists/* \
             && pip install --no-cache-dir --only-binary=:all: -r requirements-ml.txt ;; \
       false) ;; \
       *) echo 'INSTALL_ML must be true or false' >&2; exit 1 ;; \
       esac
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir /data && chown app:app /data
COPY backend/app ./app
COPY backend/scripts ./scripts
USER app
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

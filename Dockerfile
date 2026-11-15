# FrontRow API image. Built by .github/workflows/deploy.yml and tagged with
# scripts/image-tag.sh (a hash of everything copied in here), so an unchanged
# API is never rebuilt or redeployed.
FROM python:3.12-slim-bookworm AS deps
WORKDIR /build
COPY app/requirements.txt app/constraints.txt ./
RUN pip install --no-cache-dir --no-compile --disable-pip-version-check \
      --target /build/site -r requirements.txt -c constraints.txt \
 && rm -rf /build/site/bin

FROM python:3.12-slim-bookworm
ENV PYTHONPATH=/srv/site:/srv/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CATALOG_DIR=/srv/app
WORKDIR /srv/app
COPY --from=deps /build/site /srv/site
COPY app/*.py ./
COPY core/*.py ./core/
COPY catalog/events.json ./catalog/
COPY venues/*.json ./venues/
COPY frontend/ ./frontend/
# Unprivileged, and the root filesystem can be mounted read-only.
USER 10001:10001
EXPOSE 8000
# One worker per pod: Kubernetes scales pods, not processes.
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*", "--timeout-keep-alive", "65"]

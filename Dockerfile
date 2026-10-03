# Container runtime verification: NOT RUN on the preparation machine (no Docker).
FROM node:24.19.0-bookworm-slim AS web-build
WORKDIR /build/apps/web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY apps/web/ ./
RUN npm run build

FROM python:3.12.14-slim-bookworm
COPY --from=web-build /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir pip==25.0.1 setuptools==75.8.0 wheel==0.45.1 && pip install --no-cache-dir -r requirements.lock
COPY src/ src/
COPY eval/ eval/
COPY scripts/ scripts/
COPY apps/web/package.json apps/web/package.json
COPY --from=web-build /build/apps/web/dist/ apps/web/dist/
RUN pip install --no-deps --no-build-isolation . && useradd --uid 10001 --create-home demo && mkdir /state && chown demo:demo /state
USER demo
ENV PYTHONUNBUFFERED=1
CMD ["python", "scripts/demo_runtime.py", "api"]

# Imagen LIGERA para Render (plan free: 512 MB de RAM, 0,1 CPU): nginx + API + replayer de datos
# grabados (docker/render-start.sh). Sin torch, ultralytics, opencv ni yt-dlp: la API no los
# importa (tests/unit/test_api_aislamiento.py). La imagen completa con detector es compose.yaml.
# Se construye con la raiz del repo como contexto.

# --- Frontend: se compila con Vite (igual que docker/web.Dockerfile) ---
FROM node:22-alpine AS web
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
ENV VITE_API_BASE_URL=""
RUN npm run build

# --- API ligera + nginx ---
FROM python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN uv venv "$VIRTUAL_ENV"

WORKDIR /app

# Solo lo que importa `cvp.api`, con las versiones del lock (-c) pero sin arrastrar el resto.
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --no-hashes -o /tmp/lock.txt \
    && uv pip install --no-cache -c /tmp/lock.txt \
        fastapi uvicorn sqlmodel pydantic pyyaml numpy "starlette<2" \
    && rm /tmp/lock.txt

COPY README.md ./
COPY backend ./backend
RUN uv pip install --no-cache --no-deps .

COPY deploy ./deploy
COPY scripts/replay_seed.py ./scripts/replay_seed.py
COPY --from=web /frontend/dist ./web
COPY docker/render-nginx.conf ./nginx.conf
COPY docker/render-start.sh ./start.sh

# Disco efimero: la base de trabajo y el preview viven en /app/data y se rehacen al arrancar.
RUN useradd -m -u 1000 user \
    && mkdir -p data/preview \
    && cp deploy/seed-preview.jpg data/preview/01-parking-trafico.jpg \
    && chown -R user:user /app/data
USER user

ENV CVP_API_HOST=127.0.0.1 \
    CVP_API_PORT=8000 \
    DATABASE_URL=sqlite:////app/data/cvp.sqlite3

EXPOSE 10000
CMD ["sh", "/app/start.sh"]

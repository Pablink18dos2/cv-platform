# Imagen unica para Hugging Face Spaces (SDK docker): nginx + API + worker en un contenedor
# (docker/space-start.sh). Corre como uid 1000 y solo escribe en /app/data y /tmp.
# Se construye con la raiz del repo como contexto; el workflow de deploy la sube como `Dockerfile`.

# --- Frontend: se compila con Vite (igual que docker/web.Dockerfile) ---
FROM node:22-alpine AS web
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
ENV VITE_API_BASE_URL=""
RUN npm run build

# --- Backend, igual que docker/backend.Dockerfile (torch CPU) mas nginx ---
FROM python:3.13-slim

# Sin ffmpeg: solo lo pide `youtube_live` y aqui se usa el proyecto demo (fichero en bucle).
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN uv venv "$VIRTUAL_ENV"

WORKDIR /app

# Dependencias en su propia capa; torch desde el indice CPU (ver el comentario largo de
# docker/backend.Dockerfile: el lock arrastraria ~2,5 GB de CUDA).
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --no-hashes \
        -o /tmp/lock.txt \
    && grep -E '^(torch|torchvision)==' /tmp/lock.txt | cut -d' ' -f1 > /tmp/torch.txt \
    && grep -Ev '^(torch|torchvision|nvidia-|cuda-|triton)' /tmp/lock.txt > /tmp/rest.txt \
    && uv pip install --no-cache --no-deps -r /tmp/rest.txt \
    && uv pip install --no-cache --no-deps -r /tmp/torch.txt \
        --index-url https://download.pytorch.org/whl/cpu \
    && rm /tmp/*.txt

COPY README.md ./
COPY backend ./backend
RUN uv pip install --no-cache --no-deps .

# Pesos horneados en la imagen: el Space no descarga nada al arrancar.
RUN mkdir -p models \
    && cd models \
    && python -c "from ultralytics import YOLO; YOLO('yolo11n.pt')"

COPY projects ./projects
COPY tests/fixtures/video/calle-1080p-6s.mp4 ./tests/fixtures/video/calle-1080p-6s.mp4
COPY --from=web /frontend/dist ./web
COPY docker/space-nginx.conf ./nginx.conf
COPY docker/space-start.sh ./start.sh

# Hugging Face ejecuta el contenedor como uid 1000. Disco efimero: SQLite y el preview viven en
# /app/data y se pierden al reiniciar.
RUN useradd -m -u 1000 user \
    && mkdir -p data \
    && chown -R user:user /app/data
USER user

# Hilos de torch (ver el comentario de `worker` en compose.yaml): CPU basic = 2 vCPU.
# PYTHON_CPU_COUNT=3 porque ultralytics usa cpu_count - 1 hilos.
ENV OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    PYTHON_CPU_COUNT=3 \
    YOLO_CONFIG_DIR=/tmp/Ultralytics \
    CVP_API_HOST=127.0.0.1 \
    CVP_API_PORT=8000 \
    DATABASE_URL=sqlite:////app/data/cvp.sqlite3

EXPOSE 7860
CMD ["sh", "/app/start.sh"]

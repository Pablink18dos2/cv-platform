# Imagen unica del backend: la usan `api` y `worker`, que solo cambian de comando (#15).
# Lleva torch porque el worker lo necesita; la API no lo importa nunca, pero una segunda
# imagen sin torch es otra cosa que mantener y aqui pesa mas la simplicidad.
FROM python:3.13-slim

# ffmpeg lo pide `youtube_live`; libgl1 y libglib2.0-0, el wheel de opencv-python.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN uv venv "$VIRTUAL_ENV"

WORKDIR /app

# UV_COMPILE_BYTECODE: sin el, uv instala los paquetes sin .pyc y cada arranque del contenedor
# recompila ~1000 modulos de ultralytics/torch (medido: 5,3 s de `compile` en la primera
# inferencia), porque la capa del contenedor se descarta al reiniciarlo.
#
# Dependencias, en su propia capa: cambiar codigo no vuelve a descargar torch.
#
# El uv.lock resuelve torch desde PyPI (el `[tool.uv.sources]` del pyproject solo vale para
# dependencias directas y torch entra por ultralytics), y en Linux eso arrastra ~2,5 GB de
# CUDA. Se exporta el lock sin torch/torchvision ni los paquetes nvidia/cuda/triton, y se
# instalan esos dos por separado desde el indice CPU con la misma version que fija el lock.
# `--no-deps` en las dos: el lock ya trae todo, y dejar que uv resuelva otra vez volveria a
# pedir torch a PyPI (medido: 8 minutos de descarga de CUDA).
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project --no-hashes \
        -o /tmp/lock.txt \
    && grep -E '^(torch|torchvision)==' /tmp/lock.txt | cut -d' ' -f1 > /tmp/torch.txt \
    && grep -Ev '^(torch|torchvision|nvidia-|cuda-|triton)' /tmp/lock.txt > /tmp/rest.txt \
    && uv pip install --no-cache --no-deps -r /tmp/rest.txt \
    && uv pip install --no-cache --no-deps -r /tmp/torch.txt \
        --index-url https://download.pytorch.org/whl/cpu \
    && rm /tmp/*.txt

# El paquete. README.md lo exige el `readme` del pyproject al construir el wheel.
COPY README.md ./
COPY backend ./backend
RUN uv pip install --no-cache --no-deps .

# `projects/` y `tests/fixtures/` los monta compose desde el repo; `data/` y `models/`, volumenes.
# Las rutas relativas del codigo (`data/preview`, `models`) cuelgan de este directorio.
CMD ["cvp", "--version"]

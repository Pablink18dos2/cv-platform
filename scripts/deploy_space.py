"""Sube un directorio a un Hugging Face Space (SDK docker) y espera a que arranque.

Uso: `HF_TOKEN=... python scripts/deploy_space.py owner/nombre directorio`. Lo llama
`.github/workflows/deploy.yml`; el token solo se lee del entorno y no se imprime.
"""

import os
import sys
import time

from huggingface_hub import HfApi

ESPERA_MAX_S = 30 * 60


def main(space: str, carpeta: str) -> None:
    api = HfApi(token=os.environ["HF_TOKEN"])
    api.create_repo(space, repo_type="space", space_sdk="docker", exist_ok=True)
    commit = api.upload_folder(
        repo_id=space,
        repo_type="space",
        folder_path=carpeta,
        commit_message="Deploy from GitHub Actions",
    )
    print(f"subido: {commit.commit_url}", flush=True)

    limite = time.monotonic() + ESPERA_MAX_S
    while time.monotonic() < limite:
        runtime = api.get_space_runtime(space)
        stage = str(runtime.stage)
        print(f"estado del Space: {stage}", flush=True)
        # `sha` es el commit que sirve el Space: sin comprobarlo, el RUNNING de la version
        # anterior (antes de que empiece el build nuevo) pasaria por bueno.
        sirviendo = runtime.raw.get("sha")
        if stage == "RUNNING" and sirviendo in (None, commit.oid):
            return
        if stage.endswith("_ERROR"):
            sys.exit(f"el Space ha fallado: {stage}")
        time.sleep(15)
    sys.exit(f"el Space no ha llegado a RUNNING en {ESPERA_MAX_S // 60} min")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])

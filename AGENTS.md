# CV Platform

Plataforma local de visión por computador. Un **proyecto** conecta una fuente de vídeo
(connector) con un modelo de detección y uno o varios analizadores, y todos comparten el mismo
frontal web. Toda la inferencia corre en local, sin GPU y sin servicios de pago.

## Commands

- `uv sync` - instala dependencias
- `uv run pytest` - suite completa (excluye los tests marcados `network`)
- `uv run pytest tests/unit/test_smoke.py` - un solo fichero
- `uv run pytest -m "not slow and not network"` - solo los rápidos, es lo que corre CI
- `uv run ruff check .` y `uv run ruff format .` - lint y formato
- `uv run cvp --version` - el CLI

## Rules

- Dependencies are added in `pyproject.toml`. Do not add one without asking.
- Commit regularly.
- Implement against the acceptance criteria in the issue. Do not change them.
- Prefer small, reviewable diffs.
- Do not modify CI/CD, secrets, or deployment configuration without asking.
- Do not close GitHub issues. Only the orchestrator closes issues.

### Reglas propias de este proyecto

- **Los bloques se comunican solo por los contratos de `cvp.core`.** Un analizador no sabe de
  qué fuente vino el fotograma ni qué modelo lo detectó. Si para añadir algo hay que tocar el
  pipeline, el diseño está roto: arregla el diseño.
- **Nada del formato de una librería externa sale de su adaptador.** Los objetos de ultralytics
  se convierten a `Detection` dentro de `detectors/`, y ahí se quedan.
- **Ningún test de la suite por defecto toca la red ni descarga pesos.** Lo que necesite
  internet va marcado `@pytest.mark.network`; lo que cargue un modelo real, `@pytest.mark.slow`.
- **Nada de `datetime.now()` en lógica testeable.** El tiempo se inyecta, para poder probar la
  histéresis y las agregaciones con un reloj falso.
- **No se guardan matrículas, caras ni recortes de personas.** Es un no-goal explícito del plan.
- Los pesos de los modelos van a `models/` y no se commitean.

## Documents

- `_docs/plan.md` - qué construimos y para quién, y qué queda fuera
- `_docs/architecture.md` - los bloques, sus contratos y cómo se añade uno nuevo.
  **Léelo antes de tocar el pipeline, un connector, un detector o un analizador.**
- `_docs/process.md` - cómo se organiza el trabajo, roles y ciclo de vida
- Before writing tests, read `_docs/testing-guidelines.md`
- For anything touching the UI, read `_docs/design-system.md`
- For anything touching the API, read `_docs/api.md`

## Backlog

Tasks are GitHub issues. The issue is the source of truth for what to build.

<!-- Keep this file SHORT. It is loaded in every single session.
     Everything else belongs in _docs/ and gets linked from here, so it is
     loaded only when it is relevant to the task at hand. -->

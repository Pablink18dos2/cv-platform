# Design system

Dashboard sobrio, de herramienta interna: fondo claro, tipografía con contraste, sin
gradientes ni sombras. Estilo "minimalist" (editorial tipo Notion/Linear).

## Stack

- CSS: Tailwind v4 (`@theme` en `frontend/src/index.css`; los tokens de abajo son sus variables)
- Componentes: ninguno de terceros; React + TypeScript a mano
- Gráficas: `recharts`, sin animación
- Rutas: `location.hash` (`#/` y `#/p/<id>`), sin librería

## Colours

Use only these. Do not introduce new colours without asking.

| Token | Value | Used for |
|---|---|---|
| `surface` | `#f7f6f3` | page background |
| `card` | `#ffffff` | cards, charts |
| `line` | `#eaeaea` | 1 px borders, grid lines |
| `ink` | `#111111` | headings, KPI values, selected control |
| `body` | `#2f3437` | body text |
| `muted` | `#787774` | secondary text, axes |
| `danger-bg` / `danger-ink` | `#fdebec` / `#9f2f2d` | errors |
| `warn-bg` / `warn-ink` | `#fbf3db` / `#956400` | "sin datos recientes", encuadre perdido, fallo de refresco |
| `ok-bg` / `ok-ink` | `#edf3ec` / `#346538` | reservado para estados correctos |

Series de las gráficas, por orden de sentido: `#2f3437`, `#8fa3b8`, `#c9a66b`, `#9fb89a`.

## Typography

- Títulos y valores de KPI: serif (`Newsreader`, `Lyon Text`, Georgia), tracking ajustado
- Cuerpo y UI: `SF Pro Display`, `Helvetica Neue`, `system-ui`. Sin Inter ni Roboto
- Escala: título de página `text-4xl`, tarjeta `text-2xl`, KPI `text-2xl`, cuerpo base, ayudas `text-sm`

## Spacing

- Base unit: 4px (escala por defecto de Tailwind) — all spacing is a multiple of it
- Contenido en `max-w-6xl`, `px-6 py-12`; separación entre bloques `gap-4` / `gap-6`
- Bordes `rounded-lg` (8px) en tarjetas, `rounded-md` en botones y avisos; sin `shadow-*`

## Components

- **Project card** — enlace a `#/p/<id>`: nombre, "última muestra hace X" y el KPI principal
- **KPI** — etiqueta `muted` arriba, valor serif debajo, dentro de una tarjeta
- **Chart frame** — tarjeta con título serif y 256 px de alto; una por gráfica
- **Window selector** — botones `1 h` / `24 h` con `aria-pressed`; el activo en `ink` sobre blanco
- **Notice** — `role="alert"`, `danger` para errores que bloquean, `warn` para avisos
- **Async** — envuelve toda vista con datos y cubre los tres estados: "Cargando…", error (Notice) y vacío (texto `muted`). Si falla un refresco conserva el último dato y avisa

## Reglas de datos (no son estilo, pero rompen la UI si se olvidan; ver `api.md`)

- `null` en una serie: 0 para conteos (`crossings`), hueco para gauges
- Conteos con `sum_value`, gauges con `mean_value`; nunca "cobertura = seconds / 60"
- El KPI de aparcamiento es siempre "N libres de M monitorizadas"
- No se afirma "worker vivo/parado": solo "última muestra hace X", o "sin datos recientes" pasados 2 min
- Sin desglose por tipo de vehículo

## Rules

- Every interactive element has a visible focus state
- Every form field has a label
- Every destructive action asks for confirmation
- Every list has an empty state
- Reuse an existing component before creating a new one

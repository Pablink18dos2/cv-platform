# Design system

<!-- Loaded only for UI tasks. Linked from AGENTS.md.
     Purpose: stop the UI from drifting a little bit every session. -->

## Stack

- CSS: `<Tailwind / plain CSS / ...>`
- Components: `<shadcn/ui / none / ...>`
- Templates: `<Django templates / React / ...>`

## Colours

Use only these. Do not introduce new colours without asking.

| Token | Value | Used for |
|---|---|---|
| `<surface>` | `<#...>` | page background |
| `<text>` | `<#...>` | body text |
| `<accent>` | `<#...>` | primary actions |
| `<danger>` | `<#...>` | destructive actions, errors |

## Typography

- Font: `<...>`
- Scale: `<...>`

## Spacing

- Base unit: `<4px / 8px>` — all spacing is a multiple of it

## Components

<Describe each reusable component once, so the agent reuses it instead of
inventing a new variant every session.>

- **Button** — `<variants: primary, secondary, danger>`
- **Form field** — `<label above, error message below in danger colour>`
- **Empty state** — `<icon, one line of text, one action>`

## Rules

- Every interactive element has a visible focus state
- Every form field has a label
- Every destructive action asks for confirmation
- Every list has an empty state
- Reuse an existing component before creating a new one

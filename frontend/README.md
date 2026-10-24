# Frontend

Dashboard de CV Platform: Vite + React + TypeScript + Tailwind. Solo lee la API
(`_docs/api.md`); los tipos se generan desde `../openapi.yaml`.

## Arrancar

```
# 1. Datos y API (desde la raiz del repo)
uv run cvp run projects/00-demo --max-frames 150
uv run cvp api

# 2. Frontend (desde frontend/)
npm ci
npm run dev        # http://localhost:5173
```

La URL de la API sale de `VITE_API_BASE_URL` (por defecto `http://localhost:8000`) y el
origen del dashboard tiene que estar en `CVP_CORS_ORIGINS`.

## Scripts

- `npm run build` - comprueba tipos y construye
- `npm test` - vitest (transformacion de datos y estados de la vista)
- `npm run gen:api` - regenera `src/api/schema.d.ts`; hay que ejecutarlo y commitearlo cuando cambia `openapi.yaml`

## Estructura

- `src/api/client.ts` - el unico modulo que hace `fetch`
- `src/lib/metrics.ts` - reglas de `metric_minute` (null, sum/mean) en funciones puras
- `src/views/` - Dashboard (`#/`) y ProjectView (`#/p/<id>`); se refrescan cada 30 s

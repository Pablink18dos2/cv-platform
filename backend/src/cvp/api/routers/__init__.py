"""Los routers, uno por recurso: `projects`, `metrics`, `events` y `stream`.

Todos cuelgan de `/api/v1` (lo pone `crear_app`) y todos comparten `cvp.api.deps`: la
sesion de lectura, el reloj inyectado y "el proyecto o 404".
"""

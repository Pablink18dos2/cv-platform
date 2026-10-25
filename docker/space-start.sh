#!/bin/sh
# Arranque del Space: API, worker y nginx en un solo contenedor. Si nginx o la API caen,
# el contenedor termina y Hugging Face lo reinicia; el worker se relanza solo.
set -eu

cvp api &
API=$!

# La API crea el esquema de SQLite al arrancar; el worker espera a que responda para no crearlo
# a la vez (igual que `depends_on: api` en compose).
until python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/projects')" 2>/dev/null; do
    kill -0 "$API" 2>/dev/null || exit 1
    sleep 1
done

( while true; do cvp run "${PROJECT:-projects/00-demo}" || true; sleep 5; done ) &

nginx -c /app/nginx.conf -g 'daemon off;' &
NGINX=$!

# Termina en cuanto cae la API o nginx.
while kill -0 "$API" 2>/dev/null && kill -0 "$NGINX" 2>/dev/null; do sleep 2; done
exit 1

#!/bin/sh
# Arranque en Render: replayer de datos grabados, API y nginx en un solo contenedor. Si
# cualquiera cae, el contenedor termina y Render lo reinicia.
set -eu

# nginx no lee variables de entorno: el puerto que inyecta Render (10000 por defecto) se
# sustituye en una copia de la configuracion.
sed "s/__PORT__/${PORT:-10000}/" /app/nginx.conf > /tmp/nginx.conf

# El replayer deja la base lista (renombrando un fichero completo) antes de que arranque la API.
python scripts/replay_seed.py deploy/seed.sqlite3 &
REPLAY=$!
until [ -f data/cvp.sqlite3 ]; do
    kill -0 "$REPLAY" 2>/dev/null || exit 1
    sleep 0.5
done

cvp api &
API=$!

nginx -c /tmp/nginx.conf -g 'daemon off;' &
NGINX=$!

while kill -0 "$REPLAY" 2>/dev/null && kill -0 "$API" 2>/dev/null && kill -0 "$NGINX" 2>/dev/null; do
    sleep 2
done
exit 1

# Frontend: se compila con Vite y lo sirve nginx, que tambien hace de proxy a la API (#15).
FROM node:22-alpine AS build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend ./
# Vacia a proposito: el cliente pide `/api/v1/...` al mismo origen y nginx lo reenvia.
ENV VITE_API_BASE_URL=""
RUN npm run build

FROM nginx:1.27-alpine
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /frontend/dist /usr/share/nginx/html

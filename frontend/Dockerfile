FROM node:24-bookworm-slim AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/index.html frontend/tsconfig*.json frontend/vite.config.ts ./
COPY frontend/src ./src
COPY frontend/public ./public
RUN npm run build

FROM nginx:stable-alpine AS runtime
RUN mkdir -p /var/cache/nginx /var/run /var/log/nginx /tmp/nginx \
  && chown -R 101:101 /var/cache/nginx /var/run /var/log/nginx /tmp/nginx /usr/share/nginx/html
COPY infra/proxy/nginx.conf /etc/nginx/nginx.conf
COPY --from=build --chown=101:101 /app/dist /usr/share/nginx/html
EXPOSE 8080
USER 101:101
CMD ["nginx", "-g", "daemon off;"]

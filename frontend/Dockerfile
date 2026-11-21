FROM node:24-alpine AS build
WORKDIR /workspace
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY index.html tsconfig.json vite.config.ts ./
COPY src ./src
RUN npm run typecheck && npm run build

FROM nginx:1.28-alpine
COPY --chmod=755 15-resolver.envsh /docker-entrypoint.d/15-resolver.envsh
COPY nginx.conf /etc/nginx/templates/default.conf.template
COPY --from=build /workspace/dist /usr/share/nginx/html

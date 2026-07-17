FROM node:24-alpine AS build

ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"

RUN corepack enable

WORKDIR /workspace

COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/web/package.json ./apps/web/package.json
RUN pnpm install --frozen-lockfile

COPY apps/web ./apps/web
RUN pnpm build:web

FROM nginx:1.29-alpine AS runtime

COPY deploy/nginx/nginx.conf /etc/nginx/templates/default.conf.template
COPY deploy/nginx/25-local-resolver.sh /docker-entrypoint.d/25-local-resolver.sh
RUN chmod 755 /docker-entrypoint.d/25-local-resolver.sh
COPY --from=build /workspace/apps/web/dist /usr/share/nginx/html

EXPOSE 80

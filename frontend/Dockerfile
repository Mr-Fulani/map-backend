FROM node:26.8.1-alpine@sha256:2d984a15c9b54fd0aeb608b8e0d0d83529eb34d2966db27a1fb4f1edc3d298a3 AS base

RUN set -eux; \
    apk add --no-cache --upgrade libcrypto3 libssl3; \
    for package in libcrypto3 libssl3; do \
        version="$(apk list --installed "$package" \
            | sed -n "s/^${package}-\\([^ ]*\\).*/\\1/p")"; \
        [ -n "$version" ]; \
        comparison="$(apk version -t "$version" 3.5.8-r0)"; \
        [ "$comparison" = '=' ] || [ "$comparison" = '>' ]; \
    done

FROM base AS deps
WORKDIR /app
RUN set -eu; \
    npm_tarball=/tmp/npm-12.0.2.tgz; \
    wget -qO "$npm_tarball" \
        https://registry.npmjs.org/npm/-/npm-12.0.2.tgz; \
    echo "b885e890b9418fa1693544d05f53e64f9a73ec194837d4258b15fecdd692347b1dd2a517b1b0cbaf9d31cd8e92c3b70956bd2ecc72833a57b4b3098f5bfa7943  $npm_tarball" \
        | sha512sum -c -; \
    npm install --global "$npm_tarball" --ignore-scripts --no-audit --no-fund; \
    rm -f "$npm_tarball"; \
    test "$(npm --version)" = "12.0.2"
COPY package.json package-lock.json ./
RUN npm ci --strict-allow-scripts

FROM deps AS dev
COPY . .
EXPOSE 3000
ENV NEXT_TELEMETRY_DISABLED=1
CMD ["node", "node_modules/next/dist/bin/next", "dev"]

FROM deps AS builder
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_TELEMETRY_DISABLED=1
COPY . .
RUN node node_modules/next/dist/bin/next build

FROM base AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
RUN rm -rf /usr/local/lib/node_modules/npm \
    && rm -f /usr/local/bin/npm /usr/local/bin/npx
COPY --chown=node:node --from=builder /app/public ./public
COPY --chown=node:node --from=builder /app/.next/standalone ./
COPY --chown=node:node --from=builder /app/.next/static ./.next/static
USER node
EXPOSE 3000
ENV PORT=3000
ENV HOSTNAME=0.0.0.0
STOPSIGNAL SIGTERM
CMD ["node", "server.js"]

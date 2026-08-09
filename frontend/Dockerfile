FROM node:26.7.0-alpine@sha256:aadf416b2cdce311a8811ba3f0608a61b77dbf997500e2eafe781b51f6a0b019 AS base

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

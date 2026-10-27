# The Oběhy web app: built here, served by nginx with the feeds and the API (deploy/nginx.conf,
# mounted by deploy/compose.yaml). Both bases come from Google's Docker Hub mirror, as in CI.
# The app lives outside /srv, where compose mounts the data volume.
FROM mirror.gcr.io/library/node:24-alpine AS build
WORKDIR /web
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# The commit shown in the app's "O aplikaci" (CI passes it).
ARG VITE_COMMIT=""
RUN npm run build

FROM mirror.gcr.io/library/nginx:1.30-alpine
COPY --from=build /web/dist /usr/share/nginx/obehy

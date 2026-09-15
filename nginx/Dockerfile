ARG NODE_IMAGE=node:22-alpine
FROM ${NODE_IMAGE} AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM nginx:1.25-alpine
COPY nginx/nginx.conf /etc/nginx/nginx.conf
COPY nginx/conf.d/mbb.conf /etc/nginx/conf.d/default.conf
COPY nginx/react.conf /etc/nginx/react.conf
COPY --from=frontend-build /frontend/dist/ /usr/share/nginx/html/

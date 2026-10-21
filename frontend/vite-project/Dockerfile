# Build stage: compile the React app into static files.
FROM node:22-slim AS build
WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
# Vite bakes this into the bundle at build time. The browser calls the API
# directly, so it must be an address the reviewer's browser can reach.
ARG VITE_API_BASE_URL=http://localhost:8000
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

# Serve stage: nginx serves the built files on port 5173.
FROM nginx:1.30-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 5173

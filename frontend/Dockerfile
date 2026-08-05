FROM python:3.11-slim AS build
WORKDIR /src
COPY . .
# build_frontend.py has its own leak detector: it fails this build if any removed
# compute engine (SIMULATE./OPTIMIZE./ROBUST./COMPETITION./ENGINE.) is referenced
# in the bundle, so a bad image can never ship.
RUN python3 build_frontend.py

FROM nginx:alpine
COPY --from=build /src/dist/index.html /usr/share/nginx/html/index.html
EXPOSE 80

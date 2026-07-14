FROM golang:1.22-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/orchestrator ./cmd/orchestrator \
    && mkdir -p /out/db_backups

FROM gcr.io/distroless/static-debian12:nonroot
WORKDIR /app
COPY --from=build /out/orchestrator /usr/local/bin/orchestrator
COPY --from=build --chown=65532:65532 /out/db_backups /app/db_backups
VOLUME ["/app/db_backups"]
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/orchestrator", "serve"]

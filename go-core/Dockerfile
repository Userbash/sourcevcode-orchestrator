FROM golang:1.22-alpine AS build
ARG VERSION=dev
ARG COMMIT=unknown
ARG BUILD_TIME=unknown
WORKDIR /src
COPY go.mod ./
COPY go.sum ./
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 go build -trimpath \
    -ldflags="-s -w -X sourcevcode-orchestrator/go-core/internal/buildinfo.Version=${VERSION} -X sourcevcode-orchestrator/go-core/internal/buildinfo.Commit=${COMMIT} -X sourcevcode-orchestrator/go-core/internal/buildinfo.BuildTime=${BUILD_TIME}" \
    -o /out/orchestrator ./cmd/orchestrator \
    && mkdir -p /out/db_backups

FROM gcr.io/distroless/static-debian12:nonroot
ARG VERSION=dev
ARG COMMIT=unknown
ARG BUILD_TIME=unknown
LABEL org.opencontainers.image.title="go-core orchestrator"
LABEL org.opencontainers.image.version="${VERSION}"
LABEL org.opencontainers.image.revision="${COMMIT}"
LABEL org.opencontainers.image.created="${BUILD_TIME}"
WORKDIR /app
COPY --from=build /out/orchestrator /usr/local/bin/orchestrator
COPY --from=build --chown=65532:65532 /out/db_backups /app/db_backups
VOLUME ["/app/db_backups"]
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/orchestrator", "serve"]

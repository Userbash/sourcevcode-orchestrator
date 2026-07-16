FROM golang:1.22-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY main.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/ai-kernel-proxy .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/ai-kernel-proxy /usr/local/bin/ai-kernel-proxy
EXPOSE 8012
ENTRYPOINT ["/usr/local/bin/ai-kernel-proxy"]

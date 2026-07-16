FROM golang:1.22-alpine AS build
WORKDIR /src
COPY go.mod ./
COPY main.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/chat-proxy .

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/chat-proxy /usr/local/bin/chat-proxy
EXPOSE 80
ENTRYPOINT ["/usr/local/bin/chat-proxy"]

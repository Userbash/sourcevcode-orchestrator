# Orchestrator core

Minimal Go orchestration kernel with HTTP and Telegram-webhook intake.

```sh
go test ./...
go test -race ./...
go vet ./...
RUN_E2E=1 go test ./test/e2e/...
```

Run the server:

```sh
go run ./cmd/orchestrator
```

Set `TELEGRAM_WEBHOOK_SECRET` to enable `POST /webhooks/telegram`.

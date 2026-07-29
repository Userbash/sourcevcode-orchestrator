package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"sourcevcode-orchestrator/rewrite/internal/orchestrator"
)

// main starts the HTTP server using the configured PORT or the local default.
func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8010"
	}
	service, err := orchestrator.NewService(orchestrator.NewMemoryStore())
	if err != nil {
		log.Fatal(err)
	}
	handler, err := orchestrator.NewHTTPHandler(service, orchestrator.HTTPConfig{TelegramSecret: strings.TrimSpace(os.Getenv("TELEGRAM_WEBHOOK_SECRET"))})
	if err != nil {
		log.Fatal(err)
	}
	server := &http.Server{Addr: ":" + port, Handler: handler, ReadHeaderTimeout: 5 * time.Second}
	shutdownContext, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		<-shutdownContext.Done()
		context, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := server.Shutdown(context); err != nil {
			log.Printf("graceful shutdown: %v", err)
		}
	}()
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		log.Fatal(err)
	}
}

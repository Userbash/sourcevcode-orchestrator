package orchestrator

import (
	"crypto/subtle"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
)

const maxTaskRequestBytes = 1 << 20

// HTTPConfig supplies optional inbound adapter configuration.
type HTTPConfig struct {
	// TelegramSecret is the secret token configured with Telegram for webhook calls.
	TelegramSecret string
}

// NewHTTPHandler exposes health and task workflow operations over HTTP.
func NewHTTPHandler(service *Service, config ...HTTPConfig) (http.Handler, error) {
	if service == nil {
		return nil, errors.New("service is required")
	}
	if len(config) > 1 {
		return nil, errors.New("only one HTTP config is allowed")
	}
	settings := HTTPConfig{}
	if len(config) == 1 {
		settings = config[0]
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	mux.HandleFunc("POST /tasks", func(w http.ResponseWriter, r *http.Request) {
		defer r.Body.Close()
		r.Body = http.MaxBytesReader(w, r.Body, maxTaskRequestBytes)
		var request SubmitRequest
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON"})
			return
		}
		result, err := service.Submit(r.Context(), request)
		if err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		status := http.StatusAccepted
		if result.Replayed {
			status = http.StatusOK
		}
		writeJSON(w, status, result)
	})
	mux.HandleFunc("GET /tasks/{id}", func(w http.ResponseWriter, r *http.Request) {
		workflow := service.store.Get(r.PathValue("id"))
		if workflow == nil {
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"workflow_id": workflow.TaskID, "status": workflow.Status(), "events": workflow.Events()})
	})
	if settings.TelegramSecret != "" {
		mux.HandleFunc("POST /webhooks/telegram", telegramWebhookHandler(service, settings.TelegramSecret))
	}
	return mux, nil
}

type telegramUpdate struct {
	Message *telegramMessage `json:"message"`
}

type telegramMessage struct {
	MessageID int64 `json:"message_id"`
	Chat      struct {
		ID int64 `json:"id"`
	} `json:"chat"`
	From struct {
		IsBot bool `json:"is_bot"`
	} `json:"from"`
	Text string `json:"text"`
}

// telegramWebhookHandler validates and maps addressed Telegram commands to tasks.
func telegramWebhookHandler(service *Service, secret string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if subtle.ConstantTimeCompare([]byte(r.Header.Get("X-Telegram-Bot-Api-Secret-Token")), []byte(secret)) != 1 {
			writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "unauthorized"})
			return
		}
		defer r.Body.Close()
		r.Body = http.MaxBytesReader(w, r.Body, maxTaskRequestBytes)
		var update telegramUpdate
		if err := json.NewDecoder(r.Body).Decode(&update); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON"})
			return
		}
		if update.Message == nil || update.Message.From.IsBot {
			writeJSON(w, http.StatusOK, map[string]bool{"ignored": true})
			return
		}
		description, addressed := telegramCommandDescription(update.Message.Text)
		if !addressed {
			writeJSON(w, http.StatusOK, map[string]bool{"ignored": true})
			return
		}
		if description == "" || update.Message.MessageID == 0 || update.Message.Chat.ID == 0 {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid Telegram command"})
			return
		}
		result, _ := service.Submit(r.Context(), SubmitRequest{
			Description:    description,
			Capability:     "code",
			IdempotencyKey: "telegram:" + strconv.FormatInt(update.Message.Chat.ID, 10) + ":" + strconv.FormatInt(update.Message.MessageID, 10),
		})
		status := http.StatusAccepted
		if result.Replayed {
			status = http.StatusOK
		}
		writeJSON(w, status, result)
	}
}

// telegramCommandDescription returns command text for /orchestrate and its mention form.
func telegramCommandDescription(text string) (string, bool) {
	fields := strings.Fields(text)
	if len(fields) == 0 {
		return "", false
	}
	command := fields[0]
	if command != "/orchestrate" && !strings.HasPrefix(command, "/orchestrate@") {
		return "", false
	}
	return strings.TrimSpace(strings.TrimPrefix(text, command)), true
}

// writeJSON writes a JSON response with the supplied HTTP status.
func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

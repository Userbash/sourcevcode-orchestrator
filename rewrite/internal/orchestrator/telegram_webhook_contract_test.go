package orchestrator_test

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"testing"

	"sourcevcode-orchestrator/rewrite/internal/orchestrator"
)

const telegramSecret = "telegram-webhook-secret"

func TestTelegramWebhookRejectsUnauthorizedAndMalformedRequests(t *testing.T) {
	service, err := orchestrator.NewService(orchestrator.NewMemoryStore())
	if err != nil {
		t.Fatal(err)
	}
	handler, err := orchestrator.NewHTTPHandler(service, orchestrator.HTTPConfig{TelegramSecret: telegramSecret})
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name, body, secret string
		status             int
	}{
		{"missing secret", validTelegramCommand("run tests"), "", http.StatusUnauthorized},
		{"wrong secret", validTelegramCommand("run tests"), "wrong", http.StatusUnauthorized},
		{"invalid JSON", "{", telegramSecret, http.StatusBadRequest},
		{"oversized", string(make([]byte, 1<<20+1)), telegramSecret, http.StatusBadRequest},
	} {
		t.Run(test.name, func(t *testing.T) {
			response := telegramRequest(handler, test.body, test.secret)
			if response.Code != test.status {
				t.Fatalf("status=%d; want=%d", response.Code, test.status)
			}
		})
	}
}

func TestTelegramWebhookIgnoresBotsAndUnaddressedMessages(t *testing.T) {
	store := orchestrator.NewMemoryStore()
	service, err := orchestrator.NewService(store)
	if err != nil {
		t.Fatal(err)
	}
	handler, err := orchestrator.NewHTTPHandler(service, orchestrator.HTTPConfig{TelegramSecret: telegramSecret})
	if err != nil {
		t.Fatal(err)
	}
	for _, body := range []string{
		`{"message":{"message_id":55,"chat":{"id":42},"from":{"is_bot":true},"text":"/orchestrate should ignore"}}`,
		`{"message":{"message_id":56,"chat":{"id":42},"from":{"is_bot":false},"text":"ordinary chat"}}`,
		`{"edited_message":{"message_id":57,"chat":{"id":42},"text":"/orchestrate ignored update type"}}`,
		`{"message":{"message_id":58,"chat":{"id":42},"from":{"is_bot":false},"text":" \t "}}`,
	} {
		if response := telegramRequest(handler, body, telegramSecret); response.Code != http.StatusOK {
			t.Fatalf("ignored update status=%d", response.Code)
		}
	}
	if store.Count() != 0 {
		t.Fatalf("ignored updates created %d workflows", store.Count())
	}
}

func TestTelegramWebhookSubmitsCommandAndReplaysDuplicates(t *testing.T) {
	store := orchestrator.NewMemoryStore()
	service, err := orchestrator.NewService(store)
	if err != nil {
		t.Fatal(err)
	}
	handler, err := orchestrator.NewHTTPHandler(service, orchestrator.HTTPConfig{TelegramSecret: telegramSecret})
	if err != nil {
		t.Fatal(err)
	}
	body := `{"message":{"message_id":55,"chat":{"id":42},"from":{"is_bot":false},"text":"/orchestrate@my_bot   add endpoint  "}}`
	if response := telegramRequest(handler, body, telegramSecret); response.Code != http.StatusAccepted {
		t.Fatalf("first status=%d", response.Code)
	}
	if response := telegramRequest(handler, body, telegramSecret); response.Code != http.StatusOK {
		t.Fatalf("replay status=%d", response.Code)
	}
	workflow := store.ByKey("telegram:42:55")
	if workflow == nil || workflow.Status() != orchestrator.WorkflowQueued || store.Count() != 1 {
		t.Fatalf("workflow=%#v count=%d", workflow, store.Count())
	}
}

func TestTelegramWebhookRejectsMalformedCommands(t *testing.T) {
	service, err := orchestrator.NewService(orchestrator.NewMemoryStore())
	if err != nil {
		t.Fatal(err)
	}
	handler, err := orchestrator.NewHTTPHandler(service, orchestrator.HTTPConfig{TelegramSecret: telegramSecret})
	if err != nil {
		t.Fatal(err)
	}
	for _, body := range []string{
		`{"message":{"message_id":55,"chat":{"id":42},"from":{"is_bot":false},"text":"/orchestrate"}}`,
		`{"message":{"message_id":0,"chat":{"id":42},"from":{"is_bot":false},"text":"/orchestrate work"}}`,
		`{"message":{"message_id":55,"chat":{"id":0},"from":{"is_bot":false},"text":"/orchestrate work"}}`,
	} {
		if response := telegramRequest(handler, body, telegramSecret); response.Code != http.StatusBadRequest {
			t.Fatalf("status=%d; want 400 for %s", response.Code, body)
		}
	}
}

func TestTelegramWebhookIsDisabledWithoutAConfiguredSecret(t *testing.T) {
	service, err := orchestrator.NewService(orchestrator.NewMemoryStore())
	if err != nil {
		t.Fatal(err)
	}
	handler, err := orchestrator.NewHTTPHandler(service)
	if err != nil {
		t.Fatal(err)
	}
	if response := telegramRequest(handler, validTelegramCommand("run tests"), telegramSecret); response.Code != http.StatusNotFound {
		t.Fatalf("status=%d; want 404", response.Code)
	}
}

func telegramRequest(handler http.Handler, body, secret string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodPost, "/webhooks/telegram", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Telegram-Bot-Api-Secret-Token", secret)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, req)
	return response
}

func validTelegramCommand(description string) string {
	return `{"message":{"message_id":55,"chat":{"id":42},"from":{"is_bot":false},"text":"/orchestrate ` + description + `"}}`
}

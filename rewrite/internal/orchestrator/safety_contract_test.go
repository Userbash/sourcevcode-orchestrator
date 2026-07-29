package orchestrator_test

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"testing"

	"sourcevcode-orchestrator/rewrite/internal/orchestrator"
)

func TestConstructorsRejectNilDependencies(t *testing.T) {
	if service, err := orchestrator.NewService(nil); err == nil || service != nil {
		t.Fatalf("NewService(nil) = %#v, %v", service, err)
	}
	if router, err := orchestrator.NewRouter(nil); err == nil || router != nil {
		t.Fatalf("NewRouter(nil) = %#v, %v", router, err)
	}
	if scheduler, err := orchestrator.NewScheduler(nil, orchestrator.SchedulerConfig{}); err == nil || scheduler != nil {
		t.Fatalf("NewScheduler(nil) = %#v, %v", scheduler, err)
	}
	if handler, err := orchestrator.NewHTTPHandler(nil); err == nil || handler != nil {
		t.Fatalf("NewHTTPHandler(nil) = %#v, %v", handler, err)
	}
}

func TestHTTPHandlerRejectsAmbiguousConfiguration(t *testing.T) {
	service, err := orchestrator.NewService(orchestrator.NewMemoryStore())
	if err != nil {
		t.Fatal(err)
	}
	if handler, err := orchestrator.NewHTTPHandler(service, orchestrator.HTTPConfig{}, orchestrator.HTTPConfig{}); err == nil || handler != nil {
		t.Fatalf("NewHTTPHandler() = %#v, %v", handler, err)
	}
}

func TestHTTPHandlerRejectsOversizedTaskBody(t *testing.T) {
	service, err := orchestrator.NewService(orchestrator.NewMemoryStore())
	if err != nil {
		t.Fatal(err)
	}
	handler, err := orchestrator.NewHTTPHandler(service)
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest(http.MethodPost, "/tasks", bytes.NewReader(make([]byte, 1<<20+1)))
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, req)
	if response.Code != http.StatusBadRequest {
		t.Fatalf("status=%d; want %d", response.Code, http.StatusBadRequest)
	}
}

func TestSchedulerRejectsConcurrentRunWithoutBlocking(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	executor := orchestrator.ExecutorFunc(func(_ context.Context, step orchestrator.Step) (orchestrator.StepResult, error) {
		close(started)
		<-release
		return orchestrator.StepResult{StepID: step.ID}, nil
	})
	scheduler, err := orchestrator.NewScheduler(executor, orchestrator.SchedulerConfig{})
	if err != nil {
		t.Fatal(err)
	}
	firstDone := make(chan error, 1)
	go func() {
		_, err := scheduler.Run(context.Background(), orchestrator.Plan{ID: "one", Steps: []orchestrator.Step{{ID: "one"}}})
		firstDone <- err
	}()
	<-started
	if _, err := scheduler.Run(context.Background(), orchestrator.Plan{ID: "two", Steps: []orchestrator.Step{{ID: "two"}}}); err == nil {
		t.Fatal("concurrent Run was accepted")
	}
	close(release)
	if err := <-firstDone; err != nil {
		t.Fatal(err)
	}
}

func TestSchedulerRejectsConcurrentResumeWithoutBlocking(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	executor := orchestrator.ExecutorFunc(func(_ context.Context, step orchestrator.Step) (orchestrator.StepResult, error) {
		close(started)
		<-release
		return orchestrator.StepResult{StepID: step.ID}, nil
	})
	scheduler, err := orchestrator.NewScheduler(executor, orchestrator.SchedulerConfig{})
	if err != nil {
		t.Fatal(err)
	}
	firstDone := make(chan error, 1)
	go func() {
		_, err := scheduler.Run(context.Background(), orchestrator.Plan{ID: "one", Steps: []orchestrator.Step{{ID: "one"}}})
		firstDone <- err
	}()
	<-started
	if _, err := scheduler.Resume(context.Background(), "one"); err == nil {
		t.Fatal("concurrent Resume was accepted")
	}
	close(release)
	if err := <-firstDone; err != nil {
		t.Fatal(err)
	}
}

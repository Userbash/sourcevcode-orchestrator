package selflearn

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/state"
)

func TestStoreTraceRecorderAndDatasetBuilder(t *testing.T) {
	store := newTestFileStore(t)
	recorder := NewStoreTraceRecorder(store, "self_learning")
	ctx := context.Background()
	base := time.Now().UTC()

	success := domain.TraceRecord{
		TraceID:        "trace-success",
		SessionID:      "session-1",
		Prompt:         "Implement parser",
		ThoughtProcess: "Need structured parser",
		GeneratedCode:  "package main\nfunc main() {}\n",
		Evaluation:     domain.CodeExecutionResult{Status: domain.TraceRecordStatusSuccess, Score: 1},
		CreatedAt:      base,
	}
	fail := domain.TraceRecord{
		TraceID:        "trace-fail",
		SessionID:      "session-1",
		Prompt:         "Implement parser",
		ThoughtProcess: "Try broken code",
		GeneratedCode:  "package main\nfunc main( {}\n",
		Evaluation:     domain.CodeExecutionResult{Status: domain.TraceRecordStatusFail, Score: 0},
		CreatedAt:      base.Add(time.Second),
	}

	if err := recorder.RecordTrace(ctx, success); err != nil {
		t.Fatalf("RecordTrace(success) error = %v", err)
	}
	if err := recorder.RecordTrace(ctx, fail); err != nil {
		t.Fatalf("RecordTrace(fail) error = %v", err)
	}

	builder := NewStorePreferenceDatasetBuilder(store, "self_learning", "", 10)
	dataset, err := builder.BuildPreferenceDataset(ctx, base.Add(-time.Minute))
	if err != nil {
		t.Fatalf("BuildPreferenceDataset() error = %v", err)
	}
	if len(dataset) != 1 {
		t.Fatalf("dataset length = %d, want 1", len(dataset))
	}
	if dataset[0].Chosen.TraceID != success.TraceID {
		t.Fatalf("chosen trace = %q, want %q", dataset[0].Chosen.TraceID, success.TraceID)
	}
	if dataset[0].Rejected.TraceID != fail.TraceID {
		t.Fatalf("rejected trace = %q, want %q", dataset[0].Rejected.TraceID, fail.TraceID)
	}
}

func TestGoCodeEvaluator(t *testing.T) {
	evaluator := NewGoCodeEvaluator(EvalConfig{Timeout: 10 * time.Second})
	ctx := context.Background()

	okResult, err := evaluator.Evaluate(ctx, domain.TraceRecord{
		GeneratedCode: "package main\nfunc main() {}\n",
	})
	if err != nil {
		t.Fatalf("Evaluate(success) error = %v", err)
	}
	if okResult.Status != domain.TraceRecordStatusSuccess {
		t.Fatalf("success status = %q, want %q", okResult.Status, domain.TraceRecordStatusSuccess)
	}

	failResult, err := evaluator.Evaluate(ctx, domain.TraceRecord{
		GeneratedCode: "package main\nfunc main( {}\n",
	})
	if err != nil {
		t.Fatalf("Evaluate(fail) error = %v", err)
	}
	if failResult.Status != domain.TraceRecordStatusFail {
		t.Fatalf("fail status = %q, want %q", failResult.Status, domain.TraceRecordStatusFail)
	}
	if failResult.ErrorLog == "" {
		t.Fatal("expected compiler error log for invalid code")
	}
}

func TestSessionReasoningEngineUsesRAGAndRetries(t *testing.T) {
	model := &fakeStreamModel{
		responses: [][]string{
			{"<thought>need docs</thought><rag_query>jwt middleware</rag_query>"},
			{"<thought>done</thought><code>package main\nfunc main() {}\n</code>"},
		},
	}
	retriever := &fakeRetriever{
		results: []domain.RAGResult{{DocumentID: "doc-1", Content: "Use http middleware adapter"}},
	}
	engine := NewSessionReasoningEngine(model, retriever, 1)

	response, err := engine.Think(context.Background(), domain.ReasoningRequest{
		Prompt:    "build middleware",
		SessionID: "session-1",
		TaskID:    "task-1",
	})
	if err != nil {
		t.Fatalf("Think() error = %v", err)
	}
	if model.calls != 2 {
		t.Fatalf("model calls = %d, want 2", model.calls)
	}
	if retriever.calls != 1 {
		t.Fatalf("retriever calls = %d, want 1", retriever.calls)
	}
	if response.Code == "" {
		t.Fatal("expected final code after second pass")
	}
	if len(model.requests) < 2 {
		t.Fatalf("captured requests = %d, want at least 2", len(model.requests))
	}
	if _, ok := model.requests[1].Context["rag_results"]; !ok {
		t.Fatal("expected second request to include rag_results")
	}
	if got := model.requests[1].Context["rag_query"]; got != "jwt middleware" {
		t.Fatalf("rag_query = %#v, want %q", got, "jwt middleware")
	}
}

func TestExecTrainerAndHotReloader(t *testing.T) {
	workDir := t.TempDir()
	trainer := NewExecTrainer(ExecConfig{
		CommandTemplate: []string{"sh", "-c", "printf trained > {adapter}"},
		WorkDir:         workDir,
	})
	job, err := trainer.StartTraining(context.Background(), domain.FineTuneJob{
		JobID:       "job-1",
		ModelName:   domain.TargetReasoningModel,
		DatasetPath: filepath.Join(workDir, "dataset.jsonl"),
		AdapterPath: filepath.Join(workDir, "adapter.bin"),
	})
	if err != nil {
		t.Fatalf("StartTraining() error = %v", err)
	}
	if job.Status != domain.FineTuneJobStatusSucceeded {
		t.Fatalf("job status = %q, want %q", job.Status, domain.FineTuneJobStatusSucceeded)
	}
	if _, err := os.Stat(filepath.Join(workDir, "adapter.bin")); err != nil {
		t.Fatalf("adapter output missing: %v", err)
	}

	discovery := &fakeDiscovery{}
	reloader := NewExecHotReloader(ExecConfig{
		CommandTemplate: []string{"sh", "-c", "test -f {path}"},
		WorkDir:         workDir,
	}, discovery)
	modelPath := filepath.Join(workDir, "model.gguf")
	if err := os.WriteFile(modelPath, []byte("gguf"), 0o644); err != nil {
		t.Fatalf("WriteFile(model) error = %v", err)
	}
	if err := reloader.ReloadModel(context.Background(), domain.HotReloadRequest{
		Provider:  domain.TargetKernelProvider,
		ModelName: domain.TargetReasoningModel,
		ModelPath: modelPath,
	}); err != nil {
		t.Fatalf("ReloadModel() error = %v", err)
	}
	if discovery.refreshes != 1 {
		t.Fatalf("discovery refreshes = %d, want 1", discovery.refreshes)
	}
}

func newTestFileStore(t *testing.T) *state.FileStore {
	t.Helper()
	store, err := state.NewFileStore(filepath.Join(t.TempDir(), "state.json"))
	if err != nil {
		t.Fatalf("NewFileStore() error = %v", err)
	}
	return store
}

type fakeStreamModel struct {
	responses [][]string
	requests  []domain.ReasoningRequest
	calls     int
}

func (m *fakeStreamModel) StreamReasoning(_ context.Context, request domain.ReasoningRequest) (<-chan string, <-chan error) {
	stream := make(chan string, 8)
	errCh := make(chan error, 1)
	m.requests = append(m.requests, request)
	index := m.calls
	m.calls++
	go func() {
		defer close(stream)
		defer close(errCh)
		if index >= len(m.responses) {
			errCh <- errors.New("unexpected extra reasoning call")
			return
		}
		for _, chunk := range m.responses[index] {
			stream <- chunk
		}
	}()
	return stream, errCh
}

type fakeRetriever struct {
	results []domain.RAGResult
	calls   int
	query   domain.RAGQuery
}

func (r *fakeRetriever) Retrieve(_ context.Context, query domain.RAGQuery) ([]domain.RAGResult, error) {
	r.calls++
	r.query = query
	return append([]domain.RAGResult(nil), r.results...), nil
}

type fakeDiscovery struct {
	refreshes int
}

func (d *fakeDiscovery) Start(context.Context) {}

func (d *fakeDiscovery) Refresh(context.Context) (domain.SelfLearningRegistrySnapshot, error) {
	d.refreshes++
	return domain.SelfLearningRegistrySnapshot{}, nil
}

func (d *fakeDiscovery) Snapshot() domain.SelfLearningRegistrySnapshot {
	return domain.SelfLearningRegistrySnapshot{}
}

func TestExpandTemplate(t *testing.T) {
	job := domain.FineTuneJob{
		ModelName:   "model-a",
		BaseModel:   "base-a",
		DatasetPath: "/tmp/data.jsonl",
		AdapterPath: "/tmp/adapter.bin",
		MergedPath:  "/tmp/model.gguf",
	}
	got := expandTemplate([]string{"tool", "{dataset}", "{model}", "{base_model}", "{adapter}", "{merged}"}, job)
	want := []string{"tool", "/tmp/data.jsonl", "model-a", "base-a", "/tmp/adapter.bin", "/tmp/model.gguf"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("expandTemplate() = %#v, want %#v", got, want)
	}
}

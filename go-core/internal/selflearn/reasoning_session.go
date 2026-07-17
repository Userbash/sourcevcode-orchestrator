package selflearn

import (
	"context"
	"fmt"
	"strings"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type StreamModel interface {
	StreamReasoning(ctx context.Context, request domain.ReasoningRequest) (<-chan string, <-chan error)
}

type SessionReasoningEngine struct {
	model        StreamModel
	retriever    domain.RAGRetriever
	maxRAGRounds int
}

func NewSessionReasoningEngine(model StreamModel, retriever domain.RAGRetriever, maxRAGRounds int) *SessionReasoningEngine {
	if maxRAGRounds <= 0 {
		maxRAGRounds = 1
	}
	return &SessionReasoningEngine{
		model:        model,
		retriever:    retriever,
		maxRAGRounds: maxRAGRounds,
	}
}

func (e *SessionReasoningEngine) Think(ctx context.Context, request domain.ReasoningRequest) (domain.ReasoningResponse, error) {
	if e == nil || e.model == nil {
		return domain.ReasoningResponse{}, fmt.Errorf("reasoning engine is not configured")
	}
	current := cloneReasoningRequest(request)
	var last domain.ReasoningResponse
	for round := 0; round <= e.maxRAGRounds; round++ {
		response, err := e.runOnce(ctx, current)
		if err != nil {
			return domain.ReasoningResponse{}, err
		}
		last = response
		query := strings.TrimSpace(response.RAGQuery)
		if query == "" || e.retriever == nil || round == e.maxRAGRounds {
			return last, nil
		}
		results, err := e.retriever.Retrieve(ctx, domain.RAGQuery{
			Query:     query,
			SessionID: current.SessionID,
			TaskID:    current.TaskID,
			Limit:     5,
		})
		if err != nil {
			return domain.ReasoningResponse{}, err
		}
		current = augmentReasoningRequest(current, query, results)
	}
	return last, nil
}

func (e *SessionReasoningEngine) runOnce(ctx context.Context, request domain.ReasoningRequest) (domain.ReasoningResponse, error) {
	parser := NewTraceParser()
	stream, errCh := e.model.StreamReasoning(ctx, request)
	for stream != nil || errCh != nil {
		select {
		case <-ctx.Done():
			return domain.ReasoningResponse{}, ctx.Err()
		case chunk, ok := <-stream:
			if !ok {
				stream = nil
				continue
			}
			parser.Consume(chunk)
		case err, ok := <-errCh:
			if !ok {
				errCh = nil
				continue
			}
			if err != nil {
				return domain.ReasoningResponse{}, err
			}
		}
	}
	return parser.Result()
}

func cloneReasoningRequest(request domain.ReasoningRequest) domain.ReasoningRequest {
	cloned := request
	if request.Context != nil {
		cloned.Context = make(map[string]any, len(request.Context))
		for key, value := range request.Context {
			cloned.Context[key] = value
		}
	}
	return cloned
}

func augmentReasoningRequest(request domain.ReasoningRequest, query string, results []domain.RAGResult) domain.ReasoningRequest {
	cloned := cloneReasoningRequest(request)
	if cloned.Context == nil {
		cloned.Context = map[string]any{}
	}
	cloned.Context["rag_query"] = query
	cloned.Context["rag_results"] = results
	lines := make([]string, 0, len(results))
	for _, result := range results {
		content := strings.TrimSpace(result.Content)
		if content == "" {
			continue
		}
		lines = append(lines, content)
	}
	if len(lines) > 0 {
		cloned.Context["rag_context"] = strings.Join(lines, "\n\n")
		cloned.Prompt = strings.TrimSpace(cloned.Prompt + "\n\nRAG context:\n" + strings.Join(lines, "\n\n"))
	}
	return cloned
}

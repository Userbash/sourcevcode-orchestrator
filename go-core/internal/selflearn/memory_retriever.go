package selflearn

import (
	"context"
	"strings"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/memory"
)

type MemoryRAGRetriever struct {
	manager *memory.Manager
}

func NewMemoryRAGRetriever(manager *memory.Manager) *MemoryRAGRetriever {
	return &MemoryRAGRetriever{manager: manager}
}

func (r *MemoryRAGRetriever) Retrieve(ctx context.Context, query domain.RAGQuery) ([]domain.RAGResult, error) {
	if r == nil || r.manager == nil {
		return nil, nil
	}
	limit := query.Limit
	if limit <= 0 {
		limit = 5
	}
	task := domain.Task{
		ID:        firstNonEmpty(query.TaskID, "selflearn-rag"),
		SessionID: firstNonEmpty(query.SessionID, query.TaskID, "selflearn-session"),
		Input: domain.TaskInput{
			Description: strings.TrimSpace(query.Query),
		},
		RoutingHints: cloneAnyMap(query.Filters),
	}
	snapshot, err := r.manager.Retrieve(ctx, task, limit)
	if err != nil {
		return nil, err
	}
	packed := snapshot.Packed
	if len(packed) == 0 {
		packed = snapshot.Results
	}
	results := make([]domain.RAGResult, 0, len(packed))
	for _, item := range packed {
		results = append(results, domain.RAGResult{
			DocumentID: item.Chunk.ChunkID,
			Score:      item.Score,
			Content:    strings.TrimSpace(item.Chunk.Text),
			Metadata:   cloneAnyMap(item.Chunk.Metadata),
		})
	}
	return results, nil
}

func cloneAnyMap(input map[string]any) map[string]any {
	if input == nil {
		return nil
	}
	out := make(map[string]any, len(input))
	for key, value := range input {
		out[key] = value
	}
	return out
}

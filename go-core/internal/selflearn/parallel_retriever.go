package selflearn

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type ParallelRAGRetriever struct {
	retrievers []domain.RAGRetriever
}

func NewParallelRAGRetriever(retrievers ...domain.RAGRetriever) *ParallelRAGRetriever {
	active := make([]domain.RAGRetriever, 0, len(retrievers))
	for _, retriever := range retrievers {
		if retriever != nil {
			active = append(active, retriever)
		}
	}
	return &ParallelRAGRetriever{retrievers: active}
}

func (r *ParallelRAGRetriever) Retrieve(ctx context.Context, query domain.RAGQuery) ([]domain.RAGResult, error) {
	if len(r.retrievers) == 0 {
		return nil, nil
	}
	if len(r.retrievers) == 1 {
		return r.retrievers[0].Retrieve(ctx, query)
	}
	type outcome struct {
		results []domain.RAGResult
		err     error
	}
	capacity := 1
	if query.Limit > 0 {
		capacity = query.Limit
	}
	outcomes := make(chan outcome, len(r.retrievers))
	var wg sync.WaitGroup
	for _, retriever := range r.retrievers {
		wg.Add(1)
		go func(retriever domain.RAGRetriever) {
			defer wg.Done()
			results, err := retriever.Retrieve(ctx, query)
			select {
			case outcomes <- outcome{results: results, err: err}:
			case <-ctx.Done():
			}
		}(retriever)
	}
	go func() {
		wg.Wait()
		close(outcomes)
	}()
	merged := make([]domain.RAGResult, 0, len(r.retrievers)*capacity)
	errs := make([]error, 0, len(r.retrievers))
	for outcome := range outcomes {
		if outcome.err != nil {
			errs = append(errs, outcome.err)
			continue
		}
		merged = append(merged, outcome.results...)
	}
	merged = dedupeRAGResults(merged)
	sort.SliceStable(merged, func(i, j int) bool {
		if merged[i].Score == merged[j].Score {
			return merged[i].DocumentID < merged[j].DocumentID
		}
		return merged[i].Score > merged[j].Score
	})
	if query.Limit > 0 && len(merged) > query.Limit {
		merged = merged[:query.Limit]
	}
	if len(merged) == 0 && len(errs) > 0 {
		return nil, errors.Join(errs...)
	}
	return merged, nil
}

func dedupeRAGResults(results []domain.RAGResult) []domain.RAGResult {
	if len(results) <= 1 {
		return results
	}
	byID := make(map[string]domain.RAGResult, len(results))
	for _, item := range results {
		key := ragResultKey(item)
		if existing, ok := byID[key]; !ok || item.Score > existing.Score {
			byID[key] = item
		}
	}
	merged := make([]domain.RAGResult, 0, len(byID))
	for _, item := range byID {
		merged = append(merged, item)
	}
	return merged
}

func ragResultKey(item domain.RAGResult) string {
	if id := strings.TrimSpace(item.DocumentID); id != "" {
		return "doc:" + id
	}
	if id := strings.TrimSpace(item.MemoryID); id != "" {
		return "mem:" + id
	}
	content := strings.TrimSpace(item.Content)
	if content == "" {
		return fmt.Sprintf("fallback:%0.6f:%v", item.Score, item.Metadata)
	}
	return "content:" + content
}

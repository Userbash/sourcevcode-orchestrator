package selflearn

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/state"
)

type StorePreferenceDatasetBuilder struct {
	store   state.Store
	scope   string
	ownerID string
	limit   int
}

func NewStorePreferenceDatasetBuilder(store state.Store, scope string, ownerID string, limit int) *StorePreferenceDatasetBuilder {
	return &StorePreferenceDatasetBuilder{
		store:   store,
		scope:   strings.TrimSpace(scope),
		ownerID: strings.TrimSpace(ownerID),
		limit:   limit,
	}
}

func (b *StorePreferenceDatasetBuilder) BuildPreferenceDataset(ctx context.Context, since time.Time) ([]domain.PreferenceExample, error) {
	if b == nil || b.store == nil {
		return nil, fmt.Errorf("dataset builder is not configured")
	}
	memories, err := b.store.ListRAGMemories(ctx, b.scope, b.ownerID, b.limit)
	if err != nil {
		return nil, err
	}
	type tracePair struct {
		success *domain.TraceRecord
		fail    *domain.TraceRecord
	}
	pairs := map[string]*tracePair{}
	for _, memory := range memories {
		trace, ok := decodeTraceMemory(memory)
		if !ok || (!since.IsZero() && trace.CreatedAt.Before(since)) {
			continue
		}
		key := normalizePromptKey(trace.Prompt)
		if key == "" {
			continue
		}
		current := pairs[key]
		if current == nil {
			current = &tracePair{}
			pairs[key] = current
		}
		switch trace.Evaluation.Status {
		case domain.TraceRecordStatusSuccess:
			if current.success == nil || trace.Evaluation.Score > current.success.Evaluation.Score || trace.CreatedAt.After(current.success.CreatedAt) {
				copyTrace := trace
				current.success = &copyTrace
			}
		case domain.TraceRecordStatusFail:
			if current.fail == nil || trace.CreatedAt.After(current.fail.CreatedAt) {
				copyTrace := trace
				current.fail = &copyTrace
			}
		}
	}
	examples := make([]domain.PreferenceExample, 0, len(pairs))
	for key, pair := range pairs {
		if pair.success == nil || pair.fail == nil {
			continue
		}
		examples = append(examples, domain.PreferenceExample{
			DatasetID: fmt.Sprintf("pref-%s-%d", key, pair.success.CreatedAt.UnixNano()),
			Prompt:    pair.success.Prompt,
			Chosen:    *pair.success,
			Rejected:  *pair.fail,
			Metadata: map[string]any{
				"prompt_key": key,
				"scope":      b.scope,
			},
		})
	}
	sort.Slice(examples, func(i, j int) bool {
		return examples[i].Chosen.CreatedAt.After(examples[j].Chosen.CreatedAt)
	})
	return examples, nil
}

func normalizePromptKey(prompt string) string {
	return strings.Join(strings.Fields(strings.ToLower(strings.TrimSpace(prompt))), " ")
}

package vfs

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
	"sourcevcode-orchestrator/go-core/internal/state"
)

const (
	defaultArtifactThreshold = 2048
)

type Manager struct {
	artifactThreshold int
	store             state.Store
}

func NewManager(store state.Store) (*Manager, error) {
	if store == nil {
		return nil, fmt.Errorf("vfs store is required")
	}
	return &Manager{
		artifactThreshold: defaultArtifactThreshold,
		store:             store,
	}, nil
}

func (m *Manager) WriteCheckpoint(ctx context.Context, record domain.VFSCheckpointRecord) (domain.VFSCheckpointRecord, error) {
	if m == nil {
		return domain.VFSCheckpointRecord{}, fmt.Errorf("vfs manager is nil")
	}
	record.Path = strings.TrimSpace(record.Path)
	if record.Path == "" {
		return domain.VFSCheckpointRecord{}, fmt.Errorf("checkpoint path is required")
	}
	if record.CreatedAt.IsZero() {
		record.CreatedAt = time.Now().UTC()
	}
	record.UpdatedAt = time.Now().UTC()
	if record.Metadata == nil {
		record.Metadata = map[string]any{}
	}
	checkpoint := cloneMap(record.Checkpoint)
	artifacts, replaced, err := m.extractArtifacts(checkpoint)
	if err != nil {
		return domain.VFSCheckpointRecord{}, err
	}
	record.Checkpoint = replaced
	record.Checksum = checksumValue(record.Checkpoint)
	record.Integrity = "ok"
	record.StorageURI = checkpointStorageURI(record.Path)
	if len(artifacts) > 0 {
		if err := m.store.UpsertVFSArtifacts(ctx, artifacts); err != nil {
			return domain.VFSCheckpointRecord{}, err
		}
	}
	if err := m.store.UpsertVFSCheckpoints(ctx, []domain.VFSCheckpointRecord{record}); err != nil {
		return domain.VFSCheckpointRecord{}, err
	}
	return record, nil
}

func (m *Manager) ReadCheckpoint(ctx context.Context, path string) (domain.VFSCheckpointRecord, bool, error) {
	if m == nil {
		return domain.VFSCheckpointRecord{}, false, nil
	}
	record, ok, err := m.store.GetVFSCheckpoint(ctx, path)
	if err != nil || !ok {
		return record, ok, err
	}
	rehydrated, err := m.rehydrateArtifacts(record.Checkpoint)
	if err != nil {
		return domain.VFSCheckpointRecord{}, false, err
	}
	record.Checkpoint = rehydrated
	if checksumValue(record.Checkpoint) != record.Checksum {
		record.Integrity = "corrupt"
	} else if strings.TrimSpace(record.Integrity) == "" {
		record.Integrity = "ok"
	}
	return record, true, nil
}

func (m *Manager) extractArtifacts(value map[string]any) ([]domain.VFSArtifact, map[string]any, error) {
	artifacts := []domain.VFSArtifact{}
	replaced, err := m.walkValue(value, &artifacts)
	if err != nil {
		return nil, nil, err
	}
	out, _ := replaced.(map[string]any)
	return artifacts, out, nil
}

func (m *Manager) walkValue(value any, artifacts *[]domain.VFSArtifact) (any, error) {
	switch typed := value.(type) {
	case map[string]any:
		out := make(map[string]any, len(typed))
		for key, child := range typed {
			if text, ok := child.(string); ok && shouldExternalizeString(key, text, m.artifactThreshold) {
				artifact, err := m.persistArtifact(key, text)
				if err != nil {
					return nil, err
				}
				*artifacts = append(*artifacts, artifact)
				out[key] = map[string]any{
					"$vfs_artifact": artifact.ArtifactID,
					"sha256":        artifact.SHA256,
					"size_bytes":    artifact.SizeBytes,
					"mime_type":     artifact.MIMEType,
				}
				continue
			}
			next, err := m.walkValue(child, artifacts)
			if err != nil {
				return nil, err
			}
			out[key] = next
		}
		return out, nil
	case []any:
		out := make([]any, 0, len(typed))
		for _, child := range typed {
			next, err := m.walkValue(child, artifacts)
			if err != nil {
				return nil, err
			}
			out = append(out, next)
		}
		return out, nil
	default:
		return value, nil
	}
}

func (m *Manager) persistArtifact(key string, content string) (domain.VFSArtifact, error) {
	sum := sha256.Sum256([]byte(content))
	artifactID := hex.EncodeToString(sum[:])
	return domain.VFSArtifact{
		ArtifactID: artifactID,
		StorageURI: artifactStorageURI(artifactID),
		SHA256:     artifactID,
		SizeBytes:  int64(len(content)),
		MIMEType:   guessArtifactMime(key),
		Content:    []byte(content),
		Metadata:   map[string]any{"field": key},
		CreatedAt:  time.Now().UTC(),
	}, nil
}

func (m *Manager) rehydrateArtifacts(value map[string]any) (map[string]any, error) {
	rewritten, err := m.walkHydrate(value)
	if err != nil {
		return nil, err
	}
	out, _ := rewritten.(map[string]any)
	return out, nil
}

func (m *Manager) walkHydrate(value any) (any, error) {
	switch typed := value.(type) {
	case map[string]any:
		if ref, ok := typed["$vfs_artifact"].(string); ok && strings.TrimSpace(ref) != "" {
			artifact, ok, err := m.store.GetVFSArtifact(context.Background(), ref)
			if err != nil {
				return nil, err
			}
			if !ok {
				return nil, fmt.Errorf("vfs artifact %s not found in store", ref)
			}
			return string(artifact.Content), nil
		}
		out := make(map[string]any, len(typed))
		for key, child := range typed {
			next, err := m.walkHydrate(child)
			if err != nil {
				return nil, err
			}
			out[key] = next
		}
		return out, nil
	case []any:
		out := make([]any, 0, len(typed))
		for _, child := range typed {
			next, err := m.walkHydrate(child)
			if err != nil {
				return nil, err
			}
			out = append(out, next)
		}
		return out, nil
	default:
		return value, nil
	}
}

func shouldExternalizeString(key string, value string, threshold int) bool {
	if len(value) < threshold {
		return false
	}
	key = strings.ToLower(strings.TrimSpace(key))
	switch key {
	case "summary", "diff", "markdown", "raw_output", "raw_response", "logs":
		return true
	default:
		return len(value) >= threshold*2
	}
}

func guessArtifactMime(key string) string {
	key = strings.ToLower(strings.TrimSpace(key))
	switch key {
	case "markdown":
		return "text/markdown"
	case "diff":
		return "text/x-diff"
	default:
		return "text/plain"
	}
}

func checksumValue(value map[string]any) string {
	data, _ := json.Marshal(value)
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func checkpointStorageURI(path string) string {
	return "postgres://go_vfs_checkpoints/" + sanitizeStorageToken(path)
}

func artifactStorageURI(artifactID string) string {
	return "postgres://go_vfs_artifacts/" + sanitizeStorageToken(artifactID)
}

func sanitizeStorageToken(value string) string {
	return strings.NewReplacer("/", "_", "\\", "_", ":", "_", "..", "_").Replace(strings.TrimSpace(value))
}

func cloneMap(input map[string]any) map[string]any {
	if input == nil {
		return map[string]any{}
	}
	out := make(map[string]any, len(input))
	for key, value := range input {
		out[key] = value
	}
	return out
}

package state

import (
	"fmt"
	"os"
	"strings"

	"sourcevcode-orchestrator/go-core/internal/app"
)

func OpenStore(_ string) (Store, error) {
	databaseURL := strings.TrimSpace(os.Getenv("AI_BRIDGE_MEMORY_DATABASE_URL"))
	if databaseURL == "" {
		databaseURL = app.ResolvePostgresConnectionInfo().URL
	}
	if databaseURL == "" {
		return nil, fmt.Errorf("database store is required: set AI_BRIDGE_MEMORY_DATABASE_URL or PostgreSQL connection env")
	}
	return NewPostgresStore(databaseURL)
}

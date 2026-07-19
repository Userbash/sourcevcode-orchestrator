package state

import (
	"fmt"
	"os"
	"strings"

	"sourcevcode-orchestrator/go-core/internal/app"
)

func OpenStore(_ string) (Store, error) {
	postgres := app.ResolvePostgresConnectionInfo()
	databaseURL := strings.TrimSpace(os.Getenv("AI_BRIDGE_MEMORY_DATABASE_URL"))
	if databaseURL == "" {
		databaseURL = postgres.URL
	}
	if databaseURL == "" {
		return nil, fmt.Errorf(
			"database store is required: set AI_BRIDGE_MEMORY_DATABASE_URL or PostgreSQL env (resolved host=%q port=%q db=%q user=%q)",
			postgres.Host,
			postgres.Port,
			postgres.Database,
			postgres.User,
		)
	}
	return NewPostgresStore(databaseURL)
}

package ops

import (
	"strings"
	"testing"
)

func TestManagedTablesQueryUsesSingleCharacterEscape(t *testing.T) {
	query := managedTablesQuery()

	if strings.Contains(query, `ESCAPE '\\'`) {
		t.Fatalf("managed tables query uses invalid escape clause: %q", query)
	}
	if !strings.Contains(query, `ESCAPE '\'`) {
		t.Fatalf("managed tables query must use a single-character escape clause: %q", query)
	}
}

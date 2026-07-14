package kernel

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"time"
)

func newTaskID() string {
	buf := make([]byte, 8)
	if _, err := rand.Read(buf); err == nil {
		return "task_" + hex.EncodeToString(buf)
	}
	return fmt.Sprintf("task_%d", time.Now().UnixNano())
}

package kernel

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"time"
)

var taskIDRandRead = rand.Read
var taskIDNowUnixNano = func() int64 {
	return time.Now().UnixNano()
}

func newTaskID() string {
	buf := make([]byte, 8)
	if _, err := taskIDRandRead(buf); err == nil {
		return "task_" + hex.EncodeToString(buf)
	}
	return fmt.Sprintf("task_%d", taskIDNowUnixNano())
}

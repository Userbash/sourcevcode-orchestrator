package realtime

import (
	"strconv"
	"sync"
	"testing"
)

func TestHubPublishCloseDoesNotPanic(t *testing.T) {
	hub := NewHub("runtime", 16)
	sub := hub.Subscribe("tasks")
	defer sub.Close()

	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		for i := 0; i < 256; i++ {
			hub.Publish("tasks", "task.running", strconv.Itoa(i), map[string]any{"i": i})
		}
	}()
	go func() {
		defer wg.Done()
		sub.Close()
	}()
	wg.Wait()
}

package realtime

import (
	"fmt"
	"sort"
	"sync"
	"time"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type Subscription struct {
	Events <-chan domain.StreamEvent
	close  func()
}

func (s *Subscription) Close() {
	if s != nil && s.close != nil {
		s.close()
	}
}

type subscriber struct {
	topic string
	ch    chan domain.StreamEvent
}

type Hub struct {
	mu          sync.RWMutex
	name        string
	historySize int
	nextID      uint64
	subscribers map[uint64]subscriber
	history     map[string][]domain.StreamEvent
	dropped     uint64
}

func NewHub(name string, historySize int) *Hub {
	if historySize <= 0 {
		historySize = 64
	}
	return &Hub{
		name:        name,
		historySize: historySize,
		subscribers: map[uint64]subscriber{},
		history:     map[string][]domain.StreamEvent{},
	}
}

func (h *Hub) Publish(topic string, kind string, entityID string, payload map[string]any) domain.StreamEvent {
	if topic == "" {
		topic = "all"
	}
	h.mu.Lock()
	h.nextID++
	event := domain.StreamEvent{
		ID:        fmt.Sprintf("%s-%d", h.name, h.nextID),
		Stream:    h.name,
		Topic:     topic,
		Kind:      kind,
		EntityID:  entityID,
		Timestamp: time.Now().UTC(),
		Payload:   cloneMap(payload),
	}
	h.history[topic] = appendBounded(h.history[topic], event, h.historySize)
	subscribers := make([]subscriber, 0, len(h.subscribers))
	for _, sub := range h.subscribers {
		subscribers = append(subscribers, sub)
	}
	h.mu.Unlock()

	for _, sub := range subscribers {
		if sub.topic != "all" && sub.topic != topic {
			continue
		}
		select {
		case sub.ch <- event:
		default:
			h.mu.Lock()
			h.dropped++
			h.mu.Unlock()
		}
	}
	return event
}

func (h *Hub) Subscribe(topic string) *Subscription {
	if topic == "" {
		topic = "all"
	}
	ch := make(chan domain.StreamEvent, 32)
	h.mu.Lock()
	h.nextID++
	id := h.nextID
	h.subscribers[id] = subscriber{topic: topic, ch: ch}
	h.mu.Unlock()
	return &Subscription{
		Events: ch,
		close: func() {
			h.mu.Lock()
			defer h.mu.Unlock()
			sub, ok := h.subscribers[id]
			if !ok {
				return
			}
			delete(h.subscribers, id)
			close(sub.ch)
		},
	}
}

func (h *Hub) Snapshot(topic string) []domain.StreamEvent {
	h.mu.RLock()
	defer h.mu.RUnlock()
	if topic == "" || topic == "all" {
		combined := make([]domain.StreamEvent, 0)
		for _, events := range h.history {
			combined = append(combined, cloneEvents(events)...)
		}
		sort.Slice(combined, func(i, j int) bool {
			return combined[i].Timestamp.Before(combined[j].Timestamp)
		})
		return combined
	}
	return cloneEvents(h.history[topic])
}

func (h *Hub) Stats() map[string]any {
	h.mu.RLock()
	defer h.mu.RUnlock()
	topics := make([]string, 0, len(h.history))
	for topic := range h.history {
		topics = append(topics, topic)
	}
	sort.Strings(topics)
	historyCounts := make(map[string]int, len(h.history))
	for topic, events := range h.history {
		historyCounts[topic] = len(events)
	}
	return map[string]any{
		"stream":           h.name,
		"subscriber_count": len(h.subscribers),
		"history_size":     h.historySize,
		"topics":           topics,
		"history_counts":   historyCounts,
		"dropped_events":   h.dropped,
	}
}

func appendBounded(events []domain.StreamEvent, event domain.StreamEvent, limit int) []domain.StreamEvent {
	events = append(events, event)
	if len(events) <= limit {
		return events
	}
	return append([]domain.StreamEvent(nil), events[len(events)-limit:]...)
}

func cloneEvents(events []domain.StreamEvent) []domain.StreamEvent {
	out := make([]domain.StreamEvent, 0, len(events))
	for _, event := range events {
		out = append(out, domain.StreamEvent{
			ID:        event.ID,
			Stream:    event.Stream,
			Topic:     event.Topic,
			Kind:      event.Kind,
			EntityID:  event.EntityID,
			Timestamp: event.Timestamp,
			Payload:   cloneMap(event.Payload),
		})
	}
	return out
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

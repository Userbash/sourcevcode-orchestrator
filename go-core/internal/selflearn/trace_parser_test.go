package selflearn

import "testing"

func TestTraceParserStreamsThoughtQueryAndCode(t *testing.T) {
	parser := NewTraceParser()
	events := []ParseEvent{}
	for _, chunk := range []string{
		"<thought>plan ",
		"steps</thought><rag_query>pgv",
		"ector cosine search</rag_query><code>package main\n",
		"func main() {}</code>",
	} {
		events = append(events, parser.Consume(chunk)...)
	}
	result, err := parser.Result()
	if err != nil {
		t.Fatalf("Result() error = %v", err)
	}
	if result.Thought != "plan steps" {
		t.Fatalf("Thought = %q, want %q", result.Thought, "plan steps")
	}
	if result.RAGQuery != "pgvector cosine search" {
		t.Fatalf("RAGQuery = %q, want %q", result.RAGQuery, "pgvector cosine search")
	}
	if result.Code != "package main\nfunc main() {}" {
		t.Fatalf("Code = %q", result.Code)
	}
	if len(events) != 5 {
		t.Fatalf("events len = %d, want 5", len(events))
	}
	if events[2].Kind != ParseEventRAGQuery {
		t.Fatalf("events[2].Kind = %q, want %q", events[2].Kind, ParseEventRAGQuery)
	}
}

func TestTraceParserDetectsUnterminatedTag(t *testing.T) {
	parser := NewTraceParser()
	parser.Consume("<thought>unfinished")
	if _, err := parser.Result(); err == nil {
		t.Fatal("Result() error = nil, want unterminated tag error")
	}
}

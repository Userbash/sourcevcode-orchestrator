package selflearn

import (
	"fmt"
	"strings"

	"sourcevcode-orchestrator/go-core/internal/domain"
)

type ParseEventKind string

const (
	ParseEventThoughtDelta ParseEventKind = "thought_delta"
	ParseEventCodeDelta    ParseEventKind = "code_delta"
	ParseEventRAGQuery     ParseEventKind = "rag_query"
)

type ParseEvent struct {
	Kind  ParseEventKind `json:"kind"`
	Value string         `json:"value"`
}

type TraceParser struct {
	buffer    string
	tag       string
	thought   strings.Builder
	ragQuery  strings.Builder
	code      strings.Builder
	finalText strings.Builder
}

func NewTraceParser() *TraceParser {
	return &TraceParser{}
}

func (p *TraceParser) Consume(chunk string) []ParseEvent {
	if p == nil || chunk == "" {
		return nil
	}
	p.buffer += chunk
	events := make([]ParseEvent, 0, 4)
	for len(p.buffer) > 0 {
		if p.tag == "" {
			index := strings.IndexByte(p.buffer, '<')
			if index < 0 {
				p.finalText.WriteString(p.buffer)
				p.buffer = ""
				break
			}
			if index > 0 {
				p.finalText.WriteString(p.buffer[:index])
				p.buffer = p.buffer[index:]
			}
			tag, consumed, ok := matchOpenTag(p.buffer)
			if !ok {
				break
			}
			p.tag = tag
			p.buffer = p.buffer[consumed:]
			continue
		}

		closeTag := "</" + p.tag + ">"
		index := strings.Index(p.buffer, closeTag)
		if index < 0 {
			safe := safeEmitPrefix(p.buffer)
			if safe == "" {
				break
			}
			events = append(events, p.appendContent(p.tag, safe)...)
			p.buffer = p.buffer[len(safe):]
			continue
		}
		if index > 0 {
			events = append(events, p.appendContent(p.tag, p.buffer[:index])...)
		}
		if p.tag == "rag_query" {
			query := strings.TrimSpace(p.ragQuery.String())
			if query != "" {
				events = append(events, ParseEvent{Kind: ParseEventRAGQuery, Value: query})
			}
		}
		p.buffer = p.buffer[index+len(closeTag):]
		p.tag = ""
	}
	return events
}

func (p *TraceParser) Result() (domain.ReasoningResponse, error) {
	if p == nil {
		return domain.ReasoningResponse{}, nil
	}
	if strings.TrimSpace(p.tag) != "" {
		return domain.ReasoningResponse{}, fmt.Errorf("unterminated tag <%s>", p.tag)
	}
	return domain.ReasoningResponse{
		Thought:   strings.TrimSpace(p.thought.String()),
		RAGQuery:  strings.TrimSpace(p.ragQuery.String()),
		Code:      strings.TrimSpace(p.code.String()),
		FinalText: strings.TrimSpace(p.finalText.String()),
	}, nil
}

func (p *TraceParser) appendContent(tag string, text string) []ParseEvent {
	if text == "" {
		return nil
	}
	switch tag {
	case "thought":
		p.thought.WriteString(text)
		return []ParseEvent{{Kind: ParseEventThoughtDelta, Value: text}}
	case "rag_query":
		p.ragQuery.WriteString(text)
	case "code":
		p.code.WriteString(text)
		return []ParseEvent{{Kind: ParseEventCodeDelta, Value: text}}
	default:
		p.finalText.WriteString(text)
	}
	return nil
}

func matchOpenTag(buffer string) (string, int, bool) {
	for _, tag := range []string{"<thought>", "<rag_query>", "<code>"} {
		if strings.HasPrefix(buffer, tag) {
			return strings.Trim(tag, "<>"), len(tag), true
		}
	}
	return "", 0, false
}

func safeEmitPrefix(buffer string) string {
	index := strings.LastIndexByte(buffer, '<')
	if index < 0 {
		return buffer
	}
	if index == 0 {
		return ""
	}
	return buffer[:index]
}

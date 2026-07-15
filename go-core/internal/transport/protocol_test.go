package transport

import (
	"bufio"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"net"
	"testing"
	"time"
)

func TestParseEnvelopeCompactChat(t *testing.T) {
	envelope, err := ParseEnvelope([]byte(`{"u":"implement feature","m":"session-1","r":"req-1"}`), true)
	if err != nil {
		t.Fatalf("ParseEnvelope: %v", err)
	}
	if envelope.Type != "command" || envelope.Action != "chat.submit" || envelope.RequestID != "req-1" {
		t.Fatalf("unexpected envelope: %#v", envelope)
	}
	if envelope.Data["message"] != "implement feature" || envelope.Data["session_id"] != "session-1" {
		t.Fatalf("unexpected compact data: %#v", envelope.Data)
	}
}

func TestParseEnvelopeErrorTaxonomy(t *testing.T) {
	tests := []struct {
		name string
		raw  string
		code string
	}{
		{name: "invalid json", raw: "{", code: "INVALID_JSON"},
		{name: "not object", raw: "[]", code: "INVALID_FRAME"},
		{name: "missing action", raw: `{"type":"command","request_id":"r1"}`, code: "INVALID_ACTION"},
		{name: "zero timeout", raw: `{"type":"command","request_id":"r1","action":"x","timeout_ms":0}`, code: "INVALID_TIMEOUT"},
		{name: "empty idempotency", raw: `{"type":"command","request_id":"r1","action":"x","idempotency_key":" "}`, code: "INVALID_IDEMPOTENCY_KEY"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := ParseEnvelope([]byte(test.raw), false)
			if err == nil || ErrorCode(err) != test.code {
				t.Fatalf("expected %s, got %v (%s)", test.code, err, ErrorCode(err))
			}
		})
	}
}

func TestDispatcherAckResponseAndTimeout(t *testing.T) {
	dispatcher := NewDispatcher()
	dispatcher.RegisterSingle("state.get", true, func(_ context.Context, _ Envelope) (map[string]any, error) {
		return map[string]any{"status": "ok"}, nil
	})
	var frames []Envelope
	request := Envelope{Type: "command", RequestID: "r1", Action: "state.get", Ack: true}
	if err := dispatcher.Dispatch(context.Background(), request, func(frame Envelope) error {
		frames = append(frames, frame)
		return nil
	}); err != nil {
		t.Fatalf("Dispatch: %v", err)
	}
	if len(frames) != 2 || frames[0].Type != "ack" || frames[1].Type != "response" || !frames[1].Final {
		t.Fatalf("unexpected frames: %#v", frames)
	}

	dispatcher.Register("slow", "single", false, func(ctx context.Context, _ Envelope, _ Emitter) error {
		<-ctx.Done()
		return ctx.Err()
	})
	frames = nil
	request = Envelope{Type: "command", RequestID: "r2", Action: "slow", TimeoutMS: 5}
	if err := dispatcher.Dispatch(context.Background(), request, func(frame Envelope) error {
		frames = append(frames, frame)
		return nil
	}); err != nil {
		t.Fatalf("timeout Dispatch: %v", err)
	}
	if len(frames) != 1 || frames[0].Type != "error" {
		t.Fatalf("unexpected timeout frames: %#v", frames)
	}
	protocolErr, ok := frames[0].Error.(*ProtocolError)
	if !ok || protocolErr.Code != "TIMEOUT" {
		t.Fatalf("expected TIMEOUT, got %#v", frames[0].Error)
	}
}

func TestWSConnReadsMaskedTextAndWritesJSON(t *testing.T) {
	serverSide, clientSide := net.Pipe()
	defer clientSide.Close()
	conn := &WSConn{conn: serverSide, reader: bufio.NewReader(serverSide), maxMessage: 1024}
	defer conn.Close()

	readResult := make(chan []byte, 1)
	readErr := make(chan error, 1)
	go func() {
		payload, err := conn.ReadMessage(context.Background())
		if err != nil {
			readErr <- err
			return
		}
		readResult <- payload
	}()

	payload := []byte(`{"type":"command"}`)
	writeMaskedClientFrame(t, clientSide, payload)
	select {
	case err := <-readErr:
		t.Fatalf("ReadMessage: %v", err)
	case result := <-readResult:
		if string(result) != string(payload) {
			t.Fatalf("payload mismatch: %q", result)
		}
	case <-time.After(time.Second):
		t.Fatal("ReadMessage timed out")
	}

	writeDone := make(chan error, 1)
	go func() {
		writeDone <- conn.WriteJSON(context.Background(), map[string]any{"status": "ok"})
	}()
	framePayload, err := readServerFrame(clientSide)
	if err != nil {
		t.Fatalf("readServerFrame: %v", err)
	}
	if err := <-writeDone; err != nil {
		t.Fatalf("WriteJSON: %v", err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(framePayload, &decoded); err != nil || decoded["status"] != "ok" {
		t.Fatalf("unexpected JSON frame: %s (%v)", framePayload, err)
	}
}

func TestWebSocketAcceptVector(t *testing.T) {
	const key = "dGhlIHNhbXBsZSBub25jZQ=="
	const expected = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
	if actual := websocketAccept(key); actual != expected {
		t.Fatalf("accept mismatch: %s", actual)
	}
}

func writeMaskedClientFrame(t *testing.T, conn net.Conn, payload []byte) {
	t.Helper()
	mask := [4]byte{1, 2, 3, 4}
	header := []byte{0x81, 0x80 | byte(len(payload))}
	masked := make([]byte, len(payload))
	for i := range payload {
		masked[i] = payload[i] ^ mask[i%4]
	}
	if _, err := conn.Write(append(append(header, mask[:]...), masked...)); err != nil {
		t.Fatalf("write client frame: %v", err)
	}
}

func readServerFrame(conn net.Conn) ([]byte, error) {
	header := make([]byte, 2)
	if _, err := conn.Read(header); err != nil {
		return nil, err
	}
	if header[0]&0x0F != wsOpcodeText {
		return nil, errors.New("expected text frame")
	}
	length := uint64(header[1] & 0x7F)
	if length == 126 {
		var value uint16
		if err := binary.Read(conn, binary.BigEndian, &value); err != nil {
			return nil, err
		}
		length = uint64(value)
	} else if length == 127 {
		if err := binary.Read(conn, binary.BigEndian, &length); err != nil {
			return nil, err
		}
	}
	payload := make([]byte, length)
	_, err := conn.Read(payload)
	return payload, err
}

package api

import (
	"bufio"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

func TestControlWebSocketEndToEnd(t *testing.T) {
	server := newTestServer(t)
	httpServer := httptest.NewServer(server.Handler())
	defer httpServer.Close()

	parsed, err := url.Parse(httpServer.URL)
	if err != nil {
		t.Fatalf("parse server URL: %v", err)
	}
	conn, err := net.DialTimeout("tcp", parsed.Host, time.Second)
	if err != nil {
		t.Fatalf("dial server: %v", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(3 * time.Second))

	const key = "dGhlIHNhbXBsZSBub25jZQ=="
	if _, err := fmt.Fprintf(conn,
		"GET /control/ws HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Protocol: chat.v1\r\n\r\n",
		parsed.Host, key,
	); err != nil {
		t.Fatalf("write handshake: %v", err)
	}

	reader := bufio.NewReader(conn)
	status, err := reader.ReadString('\n')
	if err != nil {
		t.Fatalf("read handshake: %v", err)
	}
	if !strings.Contains(status, "101 Switching Protocols") {
		t.Fatalf("unexpected handshake status: %s", status)
	}
	headers := make(map[string]string)
	for {
		line, err := reader.ReadString('\n')
		if err != nil {
			t.Fatalf("read handshake header: %v", err)
		}
		if line == "\r\n" {
			break
		}
		parts := strings.SplitN(strings.TrimSpace(line), ":", 2)
		if len(parts) == 2 {
			headers[strings.ToLower(parts[0])] = strings.TrimSpace(parts[1])
		}
	}
	if headers["sec-websocket-protocol"] != "chat.v1" {
		t.Fatalf("subprotocol not negotiated: %#v", headers)
	}

	welcome, err := readWebSocketJSON(reader)
	if err != nil {
		t.Fatalf("read kernel version frame: %v", err)
	}
	if welcome["type"] != "system" || welcome["action"] != "kernel.version" {
		t.Fatalf("expected kernel.version frame, got %#v", welcome)
	}
	data, ok := welcome["data"].(map[string]any)
	if !ok {
		t.Fatalf("expected kernel.version data, got %#v", welcome)
	}
	kernelVersion, ok := data["kernel_version"].(map[string]any)
	if !ok || kernelVersion["version"] == "" {
		t.Fatalf("expected kernel version payload, got %#v", welcome)
	}

	request := []byte(`{"type":"command","request_id":"req-1","action":"chat.submit","ack":true,"data":{"description":"test websocket Go runtime","type":"code","project":"tests"}}`)
	if err := writeClientTextFrame(conn, request); err != nil {
		t.Fatalf("write command: %v", err)
	}
	first, err := readWebSocketJSON(reader)
	if err != nil {
		t.Fatalf("read ack: %v", err)
	}
	second, err := readWebSocketJSON(reader)
	if err != nil {
		t.Fatalf("read response: %v", err)
	}
	if first["type"] != "ack" {
		t.Fatalf("expected ack, got %#v", first)
	}
	if second["type"] != "response" || second["final"] != true {
		t.Fatalf("expected final response, got %#v", second)
	}
}

func writeClientTextFrame(conn net.Conn, payload []byte) error {
	mask := [4]byte{7, 11, 13, 17}
	header := []byte{0x81}
	switch {
	case len(payload) < 126:
		header = append(header, 0x80|byte(len(payload)))
	case len(payload) <= 65535:
		header = append(header, 0x80|126, byte(len(payload)>>8), byte(len(payload)))
	default:
		return fmt.Errorf("test payload is too large")
	}
	header = append(header, mask[:]...)
	for i, value := range payload {
		payload[i] = value ^ mask[i%4]
	}
	_, err := conn.Write(append(header, payload...))
	return err
}

func readWebSocketJSON(reader *bufio.Reader) (map[string]any, error) {
	header := make([]byte, 2)
	if _, err := io.ReadFull(reader, header); err != nil {
		return nil, err
	}
	length := uint64(header[1] & 0x7F)
	switch length {
	case 126:
		var value uint16
		if err := binary.Read(reader, binary.BigEndian, &value); err != nil {
			return nil, err
		}
		length = uint64(value)
	case 127:
		if err := binary.Read(reader, binary.BigEndian, &length); err != nil {
			return nil, err
		}
	}
	payload := make([]byte, length)
	if _, err := io.ReadFull(reader, payload); err != nil {
		return nil, err
	}
	var frame map[string]any
	if err := json.Unmarshal(payload, &frame); err != nil {
		return nil, err
	}
	return frame, nil
}

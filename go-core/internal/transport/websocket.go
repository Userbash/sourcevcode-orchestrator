package transport

import (
	"bufio"
	"context"
	"crypto/sha1"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

const (
	wsOpcodeContinuation = byte(0x0)
	wsOpcodeText         = byte(0x1)
	wsOpcodeBinary       = byte(0x2)
	wsOpcodeClose        = byte(0x8)
	wsOpcodePing         = byte(0x9)
	wsOpcodePong         = byte(0xA)
	defaultMaxMessage    = int64(4 << 20)
)

type WSConn struct {
	conn       net.Conn
	reader     *bufio.Reader
	writeMu    sync.Mutex
	closeOnce  sync.Once
	maxMessage int64
}

func UpgradeWebSocket(w http.ResponseWriter, r *http.Request, subprotocol string) (*WSConn, error) {
	if r.Method != http.MethodGet {
		return nil, NewProtocolError("websocket upgrade requires GET")
	}
	if !headerContainsToken(r.Header, "Connection", "upgrade") || !headerContainsToken(r.Header, "Upgrade", "websocket") {
		return nil, NewProtocolError("missing websocket upgrade headers")
	}
	if strings.TrimSpace(r.Header.Get("Sec-WebSocket-Version")) != "13" {
		w.Header().Set("Sec-WebSocket-Version", "13")
		return nil, NewProtocolError("unsupported websocket version")
	}
	key := strings.TrimSpace(r.Header.Get("Sec-WebSocket-Key"))
	decodedKey, err := base64.StdEncoding.DecodeString(key)
	if err != nil || len(decodedKey) != 16 {
		return nil, NewProtocolError("invalid Sec-WebSocket-Key")
	}

	hijacker, ok := w.(http.Hijacker)
	if !ok {
		return nil, errors.New("http server does not support connection hijacking")
	}
	conn, rw, err := hijacker.Hijack()
	if err != nil {
		return nil, fmt.Errorf("hijack websocket connection: %w", err)
	}

	accept := websocketAccept(key)
	if _, err = fmt.Fprintf(rw, "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n", accept); err == nil {
		if subprotocol = strings.TrimSpace(subprotocol); subprotocol != "" {
			_, err = fmt.Fprintf(rw, "Sec-WebSocket-Protocol: %s\r\n", subprotocol)
		}
	}
	if err == nil {
		_, err = fmt.Fprint(rw, "\r\n")
	}
	if err == nil {
		err = rw.Flush()
	}
	if err != nil {
		_ = conn.Close()
		return nil, fmt.Errorf("write websocket handshake: %w", err)
	}
	return &WSConn{conn: conn, reader: rw.Reader, maxMessage: defaultMaxMessage}, nil
}

func (c *WSConn) SetMaxMessageSize(size int64) {
	if size > 0 {
		c.maxMessage = size
	}
}

func (c *WSConn) ReadMessage(ctx context.Context) ([]byte, error) {
	var message []byte
	var messageOpcode byte
	for {
		if deadline, ok := ctx.Deadline(); ok {
			_ = c.conn.SetReadDeadline(deadline)
		} else {
			_ = c.conn.SetReadDeadline(time.Time{})
		}
		fin, opcode, payload, err := c.readFrame()
		if err != nil {
			return nil, err
		}
		switch opcode {
		case wsOpcodePing:
			if err := c.writeFrame(wsOpcodePong, payload); err != nil {
				return nil, err
			}
			continue
		case wsOpcodePong:
			continue
		case wsOpcodeClose:
			_ = c.writeFrame(wsOpcodeClose, payload)
			return nil, io.EOF
		case wsOpcodeText, wsOpcodeBinary:
			if messageOpcode != 0 {
				return nil, NewProtocolError("new data frame before fragmented message completed")
			}
			messageOpcode = opcode
			message = append(message, payload...)
		case wsOpcodeContinuation:
			if messageOpcode == 0 {
				return nil, NewProtocolError("unexpected continuation frame")
			}
			message = append(message, payload...)
		default:
			return nil, NewProtocolError("unsupported websocket opcode")
		}
		if int64(len(message)) > c.maxMessage {
			return nil, NewProtocolError("websocket message exceeds size limit")
		}
		if fin && messageOpcode != 0 {
			if messageOpcode != wsOpcodeText {
				return nil, NewProtocolError("binary websocket messages are not supported")
			}
			return message, nil
		}
	}
}

func (c *WSConn) WriteJSON(ctx context.Context, value any) error {
	payload, err := json.Marshal(value)
	if err != nil {
		return err
	}
	if deadline, ok := ctx.Deadline(); ok {
		_ = c.conn.SetWriteDeadline(deadline)
	} else {
		_ = c.conn.SetWriteDeadline(time.Time{})
	}
	return c.writeFrame(wsOpcodeText, payload)
}

func (c *WSConn) WritePing(ctx context.Context, payload []byte) error {
	if len(payload) > 125 {
		payload = payload[:125]
	}
	if deadline, ok := ctx.Deadline(); ok {
		_ = c.conn.SetWriteDeadline(deadline)
	}
	return c.writeFrame(wsOpcodePing, payload)
}

func (c *WSConn) Close() error {
	var err error
	c.closeOnce.Do(func() {
		_ = c.conn.SetWriteDeadline(time.Now().Add(50 * time.Millisecond))
		_ = c.writeFrame(wsOpcodeClose, []byte{})
		err = c.conn.Close()
	})
	return err
}

func (c *WSConn) readFrame() (bool, byte, []byte, error) {
	header := make([]byte, 2)
	if _, err := io.ReadFull(c.reader, header); err != nil {
		return false, 0, nil, err
	}
	if header[0]&0x70 != 0 {
		return false, 0, nil, NewProtocolError("websocket extensions are not supported")
	}
	fin := header[0]&0x80 != 0
	opcode := header[0] & 0x0F
	masked := header[1]&0x80 != 0
	if !masked {
		return false, 0, nil, NewProtocolError("client websocket frames must be masked")
	}
	length := uint64(header[1] & 0x7F)
	switch length {
	case 126:
		var extended uint16
		if err := binary.Read(c.reader, binary.BigEndian, &extended); err != nil {
			return false, 0, nil, err
		}
		length = uint64(extended)
	case 127:
		if err := binary.Read(c.reader, binary.BigEndian, &length); err != nil {
			return false, 0, nil, err
		}
		if length>>63 != 0 {
			return false, 0, nil, NewProtocolError("invalid websocket payload length")
		}
	}
	if opcode >= wsOpcodeClose && (!fin || length > 125) {
		return false, 0, nil, NewProtocolError("invalid websocket control frame")
	}
	if length > uint64(c.maxMessage) {
		return false, 0, nil, NewProtocolError("websocket frame exceeds size limit")
	}
	mask := make([]byte, 4)
	if _, err := io.ReadFull(c.reader, mask); err != nil {
		return false, 0, nil, err
	}
	payload := make([]byte, int(length))
	if _, err := io.ReadFull(c.reader, payload); err != nil {
		return false, 0, nil, err
	}
	for i := range payload {
		payload[i] ^= mask[i%4]
	}
	return fin, opcode, payload, nil
}

func (c *WSConn) writeFrame(opcode byte, payload []byte) error {
	c.writeMu.Lock()
	defer c.writeMu.Unlock()

	header := []byte{0x80 | opcode}
	length := len(payload)
	switch {
	case length < 126:
		header = append(header, byte(length))
	case length <= 65535:
		header = append(header, 126, byte(length>>8), byte(length))
	default:
		header = append(header, 127)
		extended := make([]byte, 8)
		binary.BigEndian.PutUint64(extended, uint64(length))
		header = append(header, extended...)
	}
	if _, err := c.conn.Write(header); err != nil {
		return err
	}
	if length > 0 {
		_, err := c.conn.Write(payload)
		return err
	}
	return nil
}

func headerContainsToken(header http.Header, name, token string) bool {
	for _, value := range header.Values(name) {
		for _, part := range strings.Split(value, ",") {
			if strings.EqualFold(strings.TrimSpace(part), token) {
				return true
			}
		}
	}
	return false
}

func websocketAccept(key string) string {
	sum := sha1.Sum([]byte(key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"))
	return base64.StdEncoding.EncodeToString(sum[:])
}

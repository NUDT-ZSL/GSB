package dnswire

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
)

var ErrEmptyFrame = errors.New("zero-length DNS TCP frame")

func ReadTCPFrame(r io.Reader, max int) ([]byte, error) {
	if max <= 0 || max > 65535 {
		max = 65535
	}
	var lengthBytes [2]byte
	if _, err := io.ReadFull(r, lengthBytes[:]); err != nil {
		return nil, err
	}
	length := int(binary.BigEndian.Uint16(lengthBytes[:]))
	if length == 0 {
		return nil, ErrEmptyFrame
	}
	if length > max {
		return nil, fmt.Errorf("DNS TCP frame length %d exceeds limit %d", length, max)
	}
	payload := make([]byte, length)
	if _, err := io.ReadFull(r, payload); err != nil {
		return nil, err
	}
	return payload, nil
}

func WriteTCPFrame(w io.Writer, payload []byte) error {
	if len(payload) == 0 || len(payload) > 65535 {
		return fmt.Errorf("DNS TCP frame length %d is outside 1..65535", len(payload))
	}
	var lengthBytes [2]byte
	binary.BigEndian.PutUint16(lengthBytes[:], uint16(len(payload)))
	if _, err := w.Write(lengthBytes[:]); err != nil {
		return err
	}
	_, err := w.Write(payload)
	return err
}

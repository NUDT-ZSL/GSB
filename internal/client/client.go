package client

import (
	"context"
	"fmt"
	"net"
	"sync/atomic"
	"time"

	"localdns/internal/biz"
	"localdns/internal/dnswire"
	"localdns/internal/model"
	"localdns/internal/netutil"
	"localdns/internal/protocolmeta"
)

type Response struct {
	Message dnswire.Message
	Meta    protocolmeta.Metadata
}

type Client struct {
	udpAddr string
	tcpAddr string
	timeout time.Duration
	txid    atomic.Uint32
}

func New(udpAddr, tcpAddr string, timeout time.Duration) (*Client, error) {
	if err := netutil.EnsureLoopbackAddress(udpAddr); err != nil {
		return nil, fmt.Errorf("UDP client address: %w", err)
	}
	if err := netutil.EnsureLoopbackAddress(tcpAddr); err != nil {
		return nil, fmt.Errorf("TCP client address: %w", err)
	}
	if timeout <= 0 {
		timeout = 3 * time.Second
	}
	return &Client{udpAddr: udpAddr, tcpAddr: tcpAddr, timeout: timeout}, nil
}

func (c *Client) QueryUDP(ctx context.Context, name string, typ model.Type, class model.Class) (*Response, error) {
	payload, err := c.queryPayload(name, typ, class)
	if err != nil {
		return nil, err
	}
	return c.ExchangeUDP(ctx, payload)
}

func (c *Client) QueryTCP(ctx context.Context, name string, typ model.Type, class model.Class) (*Response, error) {
	payload, err := c.queryPayload(name, typ, class)
	if err != nil {
		return nil, err
	}
	return c.ExchangeTCP(ctx, payload)
}

func (c *Client) queryPayload(name string, typ model.Type, class model.Class) ([]byte, error) {
	id := uint16(c.txid.Add(1) & 0xffff)
	msg, appErr := dnswire.NewQuery(id, name, typ, class)
	if appErr != nil {
		return nil, appErr
	}
	payload, encodeErr := dnswire.EncodeMessage(msg)
	if encodeErr != nil {
		return nil, encodeErr
	}
	return payload, nil
}

func (c *Client) ExchangeUDP(ctx context.Context, payload []byte) (*Response, error) {
	dialer := net.Dialer{Timeout: c.timeout}
	conn, err := dialer.DialContext(ctx, "udp", c.udpAddr)
	if err != nil {
		return nil, &biz.Error{Code: biz.CodeUpstream, Detail: "cannot open local UDP client", Cause: err}
	}
	defer conn.Close()
	deadline := time.Now().Add(c.timeout)
	if d, ok := ctx.Deadline(); ok && d.Before(deadline) {
		deadline = d
	}
	_ = conn.SetDeadline(deadline)
	if _, err := conn.Write(payload); err != nil {
		return nil, &biz.Error{Code: biz.CodeUpstream, Detail: "cannot send UDP query", Cause: err}
	}
	buf := make([]byte, 65535)
	n, err := conn.Read(buf)
	if err != nil {
		return nil, &biz.Error{Code: biz.CodeUpstream, Detail: "cannot read UDP response", Cause: err}
	}
	return decodeResponse(buf[:n])
}

func (c *Client) ExchangeTCP(ctx context.Context, payload []byte) (*Response, error) {
	dialer := net.Dialer{Timeout: c.timeout}
	conn, err := dialer.DialContext(ctx, "tcp", c.tcpAddr)
	if err != nil {
		return nil, &biz.Error{Code: biz.CodeUpstream, Detail: "cannot open local TCP client", Cause: err}
	}
	defer conn.Close()
	deadline := time.Now().Add(c.timeout)
	if d, ok := ctx.Deadline(); ok && d.Before(deadline) {
		deadline = d
	}
	_ = conn.SetDeadline(deadline)
	if err := dnswire.WriteTCPFrame(conn, payload); err != nil {
		return nil, &biz.Error{Code: biz.CodeUpstream, Detail: "cannot send TCP query", Cause: err}
	}
	frame, err := dnswire.ReadTCPFrame(conn, 65535)
	if err != nil {
		return nil, &biz.Error{Code: biz.CodeUpstream, Detail: "cannot read TCP response", Cause: err}
	}
	return decodeResponse(frame)
}

func decodeResponse(payload []byte) (*Response, error) {
	msg, appErr := dnswire.ParseMessage(payload)
	if appErr != nil {
		return nil, &biz.Error{Code: biz.CodeWire, Detail: "local server returned invalid wire response", Cause: appErr}
	}
	response := &Response{Message: msg}
	if metaPayload, ok := dnswire.FindOption(msg, dnswire.MetadataOptionCode); ok {
		if meta, err := protocolmeta.Decode(metaPayload); err == nil {
			response.Meta = meta
		}
	}
	return response, nil
}

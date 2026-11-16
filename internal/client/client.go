package client

import (
	"bufio"
	"encoding/json"
	"fmt"
	"net"
	"sync"
	"sync/atomic"
	"time"

	"github.com/NUDT-ZSL/GSB/orderqueue/internal/protocol"
)

type Client struct {
	conn   net.Conn
	reader *bufio.Reader
	writer *bufio.Writer
	mu     sync.Mutex
	seq    atomic.Int64
}

func Dial(address string, timeout time.Duration) (*Client, error) {
	conn, err := net.DialTimeout("tcp", address, timeout)
	if err != nil {
		return nil, err
	}
	return &Client{
		conn:   conn,
		reader: bufio.NewReaderSize(conn, 64*1024),
		writer: bufio.NewWriterSize(conn, 64*1024),
	}, nil
}

func WaitReady(address string, deadline time.Time) error {
	var lastErr error
	for {
		conn, err := net.DialTimeout("tcp", address, 150*time.Millisecond)
		if err == nil {
			_ = conn.Close()
			return nil
		}
		lastErr = err
		if time.Now().After(deadline) {
			return fmt.Errorf("server did not become ready at %s: %w", address, lastErr)
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func (c *Client) Close() error {
	if c == nil || c.conn == nil {
		return nil
	}
	return c.conn.Close()
}

func (c *Client) Call(request protocol.Request) (*protocol.Response, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if request.RequestID == "" {
		request.RequestID = fmt.Sprintf("cli-%d", c.seq.Add(1))
	}
	data, err := json.Marshal(request)
	if err != nil {
		return nil, err
	}
	if _, err := c.writer.Write(data); err != nil {
		return nil, err
	}
	if err := c.writer.WriteByte('\n'); err != nil {
		return nil, err
	}
	if err := c.writer.Flush(); err != nil {
		return nil, err
	}
	line, err := c.reader.ReadBytes('\n')
	if err != nil {
		return nil, err
	}
	var response protocol.Response
	if err := json.Unmarshal(line, &response); err != nil {
		return nil, err
	}
	return &response, nil
}

func (c *Client) Append(partition, producerSession string, seq int64, body string) (*protocol.Response, error) {
	return c.Call(protocol.Request{Type: "append", PartitionKey: partition, ProducerSession: producerSession, ProducerSeq: seq, Body: body})
}

func (c *Client) Pull(partition, consumerSession string, limit int, timeoutMS int64) (*protocol.Response, error) {
	return c.Call(protocol.Request{Type: "pull", PartitionKey: partition, ConsumerSession: consumerSession, Limit: limit, TimeoutMS: timeoutMS})
}

func (c *Client) Commit(partition, consumerSession string, offset int64) (*protocol.Response, error) {
	return c.Call(protocol.Request{Type: "commit", PartitionKey: partition, ConsumerSession: consumerSession, Offset: offset})
}

func (c *Client) Fail(partition, consumerSession string, offset int64, reason string) (*protocol.Response, error) {
	return c.Call(protocol.Request{Type: "fail", PartitionKey: partition, ConsumerSession: consumerSession, Offset: offset, Reason: reason})
}

func (c *Client) Status(partition string) (*protocol.Response, error) {
	return c.Call(protocol.Request{Type: "status", PartitionKey: partition})
}

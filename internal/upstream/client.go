package upstream

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"sort"
	"sync/atomic"
	"time"

	"localdns/internal/biz"
	"localdns/internal/dnswire"
	"localdns/internal/model"
	"localdns/internal/netutil"
)

type Client struct {
	addr        string
	timeout     time.Duration
	negativeCap uint32
	maxCNAME    int
	txid        atomic.Uint32
	sourceSeq   atomic.Uint64
}

func NewClient(addr string, timeout time.Duration, negativeCap uint32, maxCNAME int) (*Client, error) {
	if err := netutil.EnsureLoopbackAddress(addr); err != nil {
		return nil, err
	}
	if timeout <= 0 {
		timeout = 2 * time.Second
	}
	if maxCNAME <= 0 {
		maxCNAME = 16
	}
	return &Client{addr: addr, timeout: timeout, negativeCap: negativeCap, maxCNAME: maxCNAME, txid: atomic.Uint32{}}, nil
}

func (c *Client) Addr() string { return c.addr }

func (c *Client) Query(ctx context.Context, q model.Question) (model.Resolution, error) {
	if q.Class == 0 {
		q.Class = model.ClassIN
	}
	name, err := model.CanonicalName(q.Name)
	if err != nil {
		return model.Resolution{}, biz.New(biz.CodeQuery, "invalid question name: "+err.Error())
	}
	q.Name = name
	txid := uint16(c.txid.Add(1) & 0xffff)
	query, queryErr := dnswire.NewQuery(txid, q.Name, q.Type, q.Class)
	if queryErr != nil {
		queryErr.Code = biz.CodeQuery
		return model.Resolution{}, queryErr
	}
	wire, encodeErr := dnswire.EncodeMessage(query)
	if encodeErr != nil {
		return model.Resolution{}, encodeErr
	}

	dialer := net.Dialer{Timeout: c.timeout}
	conn, dialErr := dialer.DialContext(ctx, "tcp", c.addr)
	if dialErr != nil {
		return model.Resolution{}, biz.Wrap(biz.CodeUpstream, "cannot connect to local upstream", dialErr)
	}
	defer conn.Close()
	deadline := time.Now().Add(c.timeout)
	if ctxDeadline, ok := ctx.Deadline(); ok && ctxDeadline.Before(deadline) {
		deadline = ctxDeadline
	}
	_ = conn.SetDeadline(deadline)
	if err := dnswire.WriteTCPFrame(conn, wire); err != nil {
		return model.Resolution{}, biz.Wrap(biz.CodeUpstream, "cannot send forwarded query", err)
	}
	payload, readErr := dnswire.ReadTCPFrame(conn, 65535)
	if readErr != nil {
		if errors.Is(readErr, io.EOF) {
			return model.Resolution{}, biz.New(biz.CodeUpstream, "local upstream closed the connection without a response")
		}
		return model.Resolution{}, biz.Wrap(biz.CodeUpstream, "cannot read forwarded response", readErr)
	}

	response, parseErr := dnswire.ParseMessage(payload)
	if parseErr != nil {
		return model.Resolution{}, &biz.Error{
			Code: biz.CodeChain, Offset: parseErr.Offset, HasOffset: parseErr.HasOffset,
			Detail: "upstream returned an invalid wire response: " + parseErr.Error(), Cause: parseErr,
		}
	}
	seq := c.sourceSeq.Add(1)
	if response.Header.ID != txid {
		return model.Resolution{}, biz.New(biz.CodeChain, fmt.Sprintf("upstream transaction id mismatch: got %d want %d", response.Header.ID, txid))
	}
	if !response.Header.IsResponse() {
		return model.Resolution{}, biz.New(biz.CodeChain, "upstream message does not have the QR response bit")
	}
	if len(response.Questions) != 1 {
		return model.Resolution{}, biz.New(biz.CodeChain, fmt.Sprintf("upstream question count is %d, want 1", len(response.Questions)))
	}
	echo := response.Questions[0]
	if echo.Name != q.Name || echo.Type != q.Type || echo.Class != q.Class {
		return model.Resolution{}, biz.New(biz.CodeChain, "upstream question echo does not match the forwarded query")
	}

	answers, chain, err := validateAnswerChain(q, response.Answers, c.maxCNAME)
	if err != nil {
		return model.Resolution{}, err
	}
	rcode := response.Header.RCode()
	if rcode != model.RCodeNoError && rcode != model.RCodeNXDomain {
		return model.Resolution{}, biz.New(biz.CodeChain, "upstream returned unsupported RCODE "+model.RCodeName(rcode))
	}
	if rcode == model.RCodeNXDomain {
		// A CNAME chain may precede an NXDOMAIN terminal, but a terminal answer
		// with data contradicts NXDOMAIN.
		if terminalDataPresent(q.Type, answers) {
			return model.Resolution{}, biz.New(biz.CodeChain, "upstream returned NXDOMAIN with terminal answer data")
		}
	}
	for i := range answers {
		answers[i].Provenance = model.Provenance{
			Source: model.SourceUpstream, Origin: "tcp-upstream", Match: model.MatchForward, SourceSeq: seq,
		}
	}

	negative := rcode == model.RCodeNXDomain || (rcode == model.RCodeNoError && len(answers) == 0)
	if negative {
		negativeTTL, authorities := c.negativeFromAuthority(response.Authorities, seq)
		source := model.SourceNegative
		return model.Resolution{
			RCode: rcode, Answers: answers, Authorities: authorities, Negative: true,
			NegativeTTL: negativeTTL, CNAMEChain: chain, PrimarySource: source, SourceSeq: seq,
		}, nil
	}
	if !terminalReached(q.Type, answers) {
		return model.Resolution{}, biz.New(biz.CodeChain, "upstream answer ends with an incomplete CNAME chain")
	}
	return model.Resolution{
		RCode: rcode, Answers: answers, CNAMEChain: chain,
		PrimarySource: model.SourceUpstream, SourceSeq: seq,
	}, nil
}

func validateAnswerChain(q model.Question, answers []dnswire.RR, maxCNAME int) ([]model.AnswerRecord, []string, error) {
	expected := q.Name
	terminal := false
	chain := make([]string, 0, 4)
	out := make([]model.AnswerRecord, 0, len(answers))
	seen := map[string]bool{expected: true}
	hops := 0
	for i, rr := range answers {
		name, err := model.CanonicalName(rr.Name)
		if err != nil {
			return nil, nil, biz.New(biz.CodeChain, fmt.Sprintf("answer[%d] has invalid owner: %s", i, err.Error()))
		}
		if model.Class(rr.Class) != q.Class {
			return nil, nil, biz.New(biz.CodeChain, fmt.Sprintf("answer[%d] class does not match question", i))
		}
		if terminal {
			if name != expected || rr.Type != q.Type {
				return nil, nil, biz.New(biz.CodeChain, fmt.Sprintf("answer[%d] appears after the terminal RRset but does not match it", i))
			}
		} else {
			if name != expected {
				return nil, nil, biz.New(biz.CodeChain, fmt.Sprintf("answer[%d] owner %s does not continue chain at %s", i, name, expected))
			}
			if rr.Type == model.TypeCNAME {
				if len(chain) >= maxCNAME {
					return nil, nil, biz.New(biz.CodeChain, fmt.Sprintf("upstream CNAME chain exceeds %d hops", maxCNAME))
				}
				target, targetErr := model.CanonicalHostName(rr.Data)
				if targetErr != nil {
					return nil, nil, biz.New(biz.CodeChain, fmt.Sprintf("answer[%d] has invalid CNAME target", i))
				}
				if seen[target] {
					return nil, nil, biz.New(biz.CodeChain, "upstream CNAME loop detected at "+target)
				}
				seen[target] = true
				chain = append(chain, expected+" -> "+target)
				hops++
				expected = target
				if q.Type == model.TypeCNAME {
					terminal = true
				}
			} else if rr.Type == q.Type {
				terminal = true
			} else {
				return nil, nil, biz.New(biz.CodeChain, fmt.Sprintf("answer[%d] type %s does not match question type %s", i, rr.Type.String(), q.Type.String()))
			}
		}
		rec, normErr := model.NormalizeRecord(name, rr.Type, model.Class(rr.Class), rr.TTL, rr.Data)
		if normErr != nil {
			return nil, nil, biz.New(biz.CodeChain, fmt.Sprintf("answer[%d] cannot be normalized: %s", i, normErr.Error()))
		}
		out = append(out, model.AnswerRecord{Record: rec})
	}
	return out, chain, nil
}

func terminalDataPresent(qtype model.Type, answers []model.AnswerRecord) bool {
	if len(answers) == 0 {
		return false
	}
	last := answers[len(answers)-1]
	return last.Record.Type == qtype && last.Record.Type != model.TypeCNAME
}

func terminalReached(qtype model.Type, answers []model.AnswerRecord) bool {
	if qtype == model.TypeCNAME {
		for _, ar := range answers {
			if ar.Record.Type == model.TypeCNAME {
				return true
			}
		}
		return false
	}
	for _, ar := range answers {
		if ar.Record.Type == qtype {
			return true
		}
	}
	return false
}

func (c *Client) negativeFromAuthority(authorities []dnswire.RR, seq uint64) (uint32, []model.AnswerRecord) {
	var soa model.Record
	found := false
	for _, rr := range authorities {
		if rr.Type != model.TypeSOA {
			continue
		}
		rec, err := model.NormalizeRecord(rr.Name, rr.Type, model.Class(rr.Class), rr.TTL, rr.Data)
		if err != nil {
			continue
		}
		soa = rec
		found = true
		break
	}
	if !found {
		return 0, nil
	}
	data, err := model.ParseSOAData(soa.Data)
	if err != nil {
		return 0, nil
	}
	ttl := soa.TTL
	if data.Minimum < ttl {
		ttl = data.Minimum
	}
	if c.negativeCap > 0 && c.negativeCap < ttl {
		ttl = c.negativeCap
	}
	soa.TTL = ttl
	return ttl, []model.AnswerRecord{{
		Record: soa,
		Provenance: model.Provenance{
			Source: model.SourceNegative, Origin: "tcp-upstream", Match: model.MatchNegative, SourceSeq: seq,
		},
	}}
}

// AnswerSummary is used by diagnostics and tests without exposing wire types.
func AnswerSummary(answers []model.AnswerRecord) []string {
	out := make([]string, 0, len(answers))
	for _, ar := range answers {
		out = append(out, fmt.Sprintf("%s %s %s", ar.Record.Name, ar.Record.Type.String(), ar.Record.Data))
	}
	sort.Strings(out)
	return out
}

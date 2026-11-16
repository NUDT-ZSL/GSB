package protocol

type Request struct {
	Type                  string `json:"type"`
	RequestID             string `json:"request_id,omitempty"`
	PartitionKey          string `json:"partition_key,omitempty"`
	ProducerSession       string `json:"producer_session,omitempty"`
	ProducerSeq           int64  `json:"producer_seq,omitempty"`
	Body                  string `json:"body,omitempty"`
	ConsumerSession       string `json:"consumer_session,omitempty"`
	Limit                 int    `json:"limit,omitempty"`
	TimeoutMS             int64  `json:"timeout_ms,omitempty"`
	Offset                int64  `json:"offset,omitempty"`
	Reason                string `json:"reason,omitempty"`
	Operator              string `json:"operator,omitempty"`
	ExpectedBlockedOffset int64  `json:"expected_blocked_offset,omitempty"`
}

type WindowInfo struct {
	Holder         string `json:"holder"`
	StartOffset    int64  `json:"start_offset"`
	EndOffset      int64  `json:"end_offset"`
	DeadlineUnixMS int64  `json:"deadline_unix_ms"`
	TTLMS          int64  `json:"ttl_ms"`
	Recovered      bool   `json:"recovered,omitempty"`
}

type MessageView struct {
	PartitionKey    string `json:"partition_key"`
	Offset          int64  `json:"offset"`
	ProducerSession string `json:"producer_session"`
	ProducerSeq     int64  `json:"producer_seq"`
	Body            string `json:"body"`
	AppendedUnixMS  int64  `json:"appended_unix_ms"`
	RetryCount      int    `json:"retry_count"`
}

type PartitionView struct {
	PartitionKey      string         `json:"partition_key"`
	NextOffset        int64          `json:"next_offset"`
	CommittedOffset   int64          `json:"committed_offset"`
	ConfirmedOffset   int64          `json:"confirmed_offset"`
	Window            *WindowInfo    `json:"window,omitempty"`
	RetryCounts       map[string]int `json:"retry_counts"`
	PendingRedelivery bool           `json:"pending_redelivery"`
	RedeliveryFrom    int64          `json:"redelivery_from,omitempty"`
	Blocked           bool           `json:"blocked"`
	BlockedOffset     int64          `json:"blocked_offset,omitempty"`
	BlockedRetryCount int            `json:"blocked_retry_count,omitempty"`
	LedgerVersion     int64          `json:"ledger_version"`
	StateRevision     int64          `json:"state_revision"`
}

type StatusView struct {
	LogFormatVersion    int64           `json:"log_format_version"`
	LedgerFormatVersion int64           `json:"ledger_format_version"`
	MaxRetries          int             `json:"max_retries"`
	Partitions          []PartitionView `json:"partitions"`
}

type Response struct {
	OK                bool           `json:"ok"`
	Code              string         `json:"code"`
	Message           string         `json:"message,omitempty"`
	RequestID         string         `json:"request_id,omitempty"`
	Type              string         `json:"type,omitempty"`
	PartitionKey      string         `json:"partition_key,omitempty"`
	Offset            int64          `json:"offset,omitempty"`
	NextOffset        int64          `json:"next_offset,omitempty"`
	CommittedOffset   int64          `json:"committed_offset,omitempty"`
	ConfirmedOffset   int64          `json:"confirmed_offset,omitempty"`
	Session           string         `json:"session,omitempty"`
	Window            *WindowInfo    `json:"window,omitempty"`
	Messages          []MessageView  `json:"messages,omitempty"`
	RetryCounts       map[string]int `json:"retry_counts,omitempty"`
	Holder            string         `json:"holder,omitempty"`
	RedeliveryFrom    int64          `json:"redelivery_from,omitempty"`
	BlockedOffset     int64          `json:"blocked_offset,omitempty"`
	BlockedRetryCount int            `json:"blocked_retry_count,omitempty"`
	Status            *StatusView    `json:"status,omitempty"`
	Fields            map[string]any `json:"fields,omitempty"`
}

func Success(requestID, requestType string) *Response {
	return &Response{OK: true, Code: CodeOK, RequestID: requestID, Type: requestType}
}

func Failure(requestID, requestType string, err *BizError) *Response {
	resp := &Response{
		OK:        false,
		Code:      err.Code,
		Message:   err.Message,
		RequestID: requestID,
		Type:      requestType,
		Fields:    err.Fields,
	}
	if v, ok := err.Fields["partition_key"].(string); ok {
		resp.PartitionKey = v
	}
	if v, ok := err.Fields["holder"].(string); ok {
		resp.Holder = v
	}
	if v, ok := err.Fields["blocked_offset"].(int64); ok {
		resp.BlockedOffset = v
	}
	if v, ok := err.Fields["blocked_retry_count"].(int); ok {
		resp.BlockedRetryCount = v
	}
	return resp
}

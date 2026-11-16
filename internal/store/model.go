package store

const (
	LogFormatVersion    int64 = 1
	LedgerFormatVersion int64 = 1
)

type PartitionRow struct {
	PartitionKey         string
	NextOffset           int64
	CommittedOffset      int64
	ConfirmedOffset      int64
	WindowHolder         string
	WindowStart          int64
	WindowEnd            int64
	WindowDeadlineUnixMS int64
	WindowTTLMS          int64
	WindowRecovered      bool
	PendingRedelivery    bool
	RedeliveryFrom       int64
	Blocked              bool
	BlockedOffset        int64
	BlockedRetryCount    int
	LedgerVersion        int64
	StateRevision        int64
}

func (p PartitionRow) HasWindow() bool {
	return p.WindowHolder != ""
}

type MessageRow struct {
	PartitionKey    string
	Offset          int64
	ProducerSession string
	ProducerSeq     int64
	Body            string
	AppendedUnixMS  int64
}

type EventRow struct {
	EventID         int64
	PartitionKey    string
	Kind            string
	Session         string
	Offset          int64
	StartOffset     int64
	EndOffset       int64
	CommittedBefore int64
	CommittedAfter  int64
	RetryCount      int
	Code            string
	Detail          string
	CreatedUnixMS   int64
}

type RetryRow struct {
	PartitionKey string
	Offset       int64
	Attempts     int
}

type Meta struct {
	LogFormatVersion    int64
	LedgerFormatVersion int64
	MaxRetries          int
	Raw                 map[string]string
}

type VersionReport struct {
	OriginalLogVersion    int64    `json:"original_log_format_version"`
	OriginalLedgerVersion int64    `json:"original_ledger_format_version"`
	LogVersion            int64    `json:"log_format_version"`
	LedgerVersion         int64    `json:"ledger_format_version"`
	Rebuilt               bool     `json:"rebuilt"`
	RebuiltPartitions     []string `json:"rebuilt_partitions,omitempty"`
	Reason                string   `json:"reason,omitempty"`
}

type PartitionSnapshot struct {
	Partition   PartitionRow
	RetryCounts map[int64]int
	Messages    []MessageRow
	Events      []EventRow
}

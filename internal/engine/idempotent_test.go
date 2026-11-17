package engine

import (
	"testing"
)

func TestIdempotentReapplySameManifest(t *testing.T) {
	v1 := applySample(t, nil, "manifest-v1.json", 1200)
	again := applySample(t, v1, "manifest-v1.json", 1200)
	if berr := Verify(again, v1); berr != nil {
		t.Fatalf("idempotent verify: %v (ledger=%+v)", berr, again.Ledger.Diff)
	}
	if berr := Replay(again, v1); berr != nil {
		t.Fatalf("idempotent replay: %v", berr)
	}
}

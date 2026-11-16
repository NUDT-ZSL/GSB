package selection

import (
	"testing"

	"localmesh/internal/rules"
)

func TestWeightedOrderDeterministicAndHonorsZeroWeight(t *testing.T) {
	instances := []rules.Instance{
		{ID: "a", Weight: 3},
		{ID: "b", Weight: 1},
		{ID: "c", Weight: 0},
	}
	first := WeightedOrder(instances, "r1", "req-7", 1)
	second := WeightedOrder(instances, "r1", "req-7", 1)
	if len(first) != 2 {
		t.Fatalf("weighted order len=%d", len(first))
	}
	for i := range first {
		if first[i].ID != second[i].ID {
			t.Fatalf("order differs at %d: %s vs %s", i, first[i].ID, second[i].ID)
		}
		if first[i].ID == "c" {
			t.Fatal("zero-weight instance must not be selected")
		}
	}
}

func TestWeightedFirstChoiceRoughlyTracksWeights(t *testing.T) {
	instances := []rules.Instance{{ID: "a", Weight: 3}, {ID: "b", Weight: 1}}
	counts := map[string]int{}
	for i := 0; i < 2000; i++ {
		order := WeightedOrder(instances, "round", "req-"+itoa(i), 1)
		counts[order[0].ID]++
	}
	if counts["a"] <= counts["b"] {
		t.Fatalf("weighted distribution not reflected: %+v", counts)
	}
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}

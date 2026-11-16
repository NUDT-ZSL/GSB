package selection

import (
	"fmt"
	"hash/fnv"
	"sort"

	"localmesh/internal/rules"
)

// WeightedOrder returns a deterministic order for a request attempt. The first
// item wins with probability proportional to its weight; subsequent items are
// ordered by clockwise distance on the same weighted ring and are used when a
// circuit is open.
func WeightedOrder(instances []rules.Instance, roundID, requestID string, attemptNo int) []rules.Instance {
	total := 0
	for _, inst := range instances {
		if inst.Weight > 0 {
			total += inst.Weight
		}
	}
	if total == 0 {
		return nil
	}
	h := fnv.New64a()
	_, _ = h.Write([]byte(fmt.Sprintf("%s\x00%s\x00%d", roundID, requestID, attemptNo)))
	mixed := h.Sum64()
	mixed ^= mixed >> 30
	mixed *= 0xbf58476d1ce4e5b9
	mixed ^= mixed >> 27
	mixed *= 0x94d049bb133111eb
	mixed ^= mixed >> 31
	position := int(mixed % uint64(total))

	type candidate struct {
		inst     rules.Instance
		distance int
	}
	candidates := make([]candidate, 0, len(instances))
	cumulative := 0
	for _, inst := range instances {
		if inst.Weight == 0 {
			continue
		}
		// Distance travelled backwards from the hashed slot to this instance's
		// interval start. The interval containing the slot therefore has the
		// smallest distance, giving first-choice probability equal to weight.
		distance := (position - cumulative + total) % total
		candidates = append(candidates, candidate{inst: inst, distance: distance})
		cumulative += inst.Weight
	}
	sort.SliceStable(candidates, func(i, j int) bool {
		if candidates[i].distance == candidates[j].distance {
			return candidates[i].inst.ID < candidates[j].inst.ID
		}
		return candidates[i].distance < candidates[j].distance
	})
	out := make([]rules.Instance, len(candidates))
	for i := range candidates {
		out[i] = candidates[i].inst
	}
	return out
}

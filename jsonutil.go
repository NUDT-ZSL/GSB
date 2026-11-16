package docquery

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math/big"
	"sort"
	"strconv"
	"strings"
)

func decodeJSON(data []byte) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	var value any
	if err := dec.Decode(&value); err != nil {
		return nil, err
	}
	var extra any
	if err := dec.Decode(&extra); err != io.EOF {
		if err == nil {
			return nil, fmt.Errorf("multiple JSON values are not allowed")
		}
		return nil, err
	}
	return value, nil
}

func decodeRawObject(data []byte) (map[string]json.RawMessage, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	var raw map[string]json.RawMessage
	if err := dec.Decode(&raw); err != nil {
		return nil, err
	}
	if raw == nil {
		return nil, fmt.Errorf("expected a JSON object")
	}
	var extra any
	if err := dec.Decode(&extra); err != io.EOF {
		if err == nil {
			return nil, fmt.Errorf("multiple JSON values are not allowed")
		}
		return nil, err
	}
	return raw, nil
}

// normalizeRawKeys case-folds object keys and rejects keys that differ only by
// case. It gives query objects order-independent, case-insensitive semantics.
func normalizeRawKeys(raw map[string]json.RawMessage) (map[string]json.RawMessage, error) {
	out := make(map[string]json.RawMessage, len(raw))
	original := make(map[string]string, len(raw))
	for key, value := range raw {
		normalized := strings.ToLower(key)
		if previous, ok := original[normalized]; ok {
			return nil, fmt.Errorf("duplicate key %q (also seen as %q)", key, previous)
		}
		original[normalized] = key
		out[normalized] = value
	}
	return out, nil
}

func rejectUnknownKeys(raw map[string]json.RawMessage, allowed ...string) error {
	allowedSet := make(map[string]struct{}, len(allowed))
	for _, key := range allowed {
		allowedSet[strings.ToLower(key)] = struct{}{}
	}
	for key := range raw {
		if _, ok := allowedSet[strings.ToLower(key)]; !ok {
			return fmt.Errorf("unsupported key %q", key)
		}
	}
	return nil
}

func decodeJSONString(raw json.RawMessage) (string, error) {
	var value string
	if err := json.Unmarshal(raw, &value); err != nil {
		return "", fmt.Errorf("expected string: %w", err)
	}
	return value, nil
}

func decodeJSONInt(raw json.RawMessage) (int, error) {
	var number json.Number
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&number); err != nil {
		return 0, fmt.Errorf("expected integer: %w", err)
	}
	text := number.String()
	if strings.ContainsAny(text, ".eE") {
		return 0, fmt.Errorf("expected integer, got %q", text)
	}
	value, err := strconv.Atoi(text)
	if err != nil {
		return 0, fmt.Errorf("invalid integer %q", text)
	}
	return value, nil
}

func normalizeQueryScalar(value any) (any, error) {
	switch v := value.(type) {
	case nil:
		return nil, nil
	case string, bool:
		return v, nil
	case json.Number:
		if rat, ok := numberRat(v); ok {
			if text, ok := ratToDecimal(rat); ok {
				return json.Number(text), nil
			}
		}
		return v, nil
	case float64, float32, int, int8, int16, int32, int64, uint, uint8, uint16, uint32, uint64:
		rat, ok := numberRatForAny(v)
		if !ok {
			return nil, fmt.Errorf("invalid numeric value %v", v)
		}
		text, ok := ratToDecimal(rat)
		if !ok {
			return nil, fmt.Errorf("invalid numeric value %v", v)
		}
		return json.Number(text), nil
	default:
		return nil, fmt.Errorf("expected a scalar value, got %T", value)
	}
}

// ratToDecimal returns a canonical, valid JSON decimal representation when the
// rational is representable with a finite decimal expansion.
func ratToDecimal(rat *big.Rat) (string, bool) {
	if rat == nil {
		return "", false
	}
	denominator := new(big.Int).Set(rat.Denom())
	twos := 0
	for denominator.Bit(0) == 0 {
		denominator.Rsh(denominator, 1)
		twos++
	}
	fives := 0
	five := big.NewInt(5)
	remainder := new(big.Int)
	for {
		quotient, rem := new(big.Int).QuoRem(denominator, five, remainder)
		if rem.Sign() != 0 {
			break
		}
		denominator = quotient
		fives++
	}
	if denominator.Cmp(big.NewInt(1)) != 0 {
		return "", false
	}
	scale := twos
	if fives > scale {
		scale = fives
	}
	text := rat.FloatString(scale)
	if strings.Contains(text, ".") {
		text = strings.TrimRight(text, "0")
		text = strings.TrimRight(text, ".")
	}
	if text == "-0" || text == "" {
		text = "0"
	}
	return text, true
}

func cloneJSONValue(value any) any {
	switch v := value.(type) {
	case map[string]any:
		out := make(map[string]any, len(v))
		for key, child := range v {
			out[key] = cloneJSONValue(child)
		}
		return out
	case Document:
		out := make(Document, len(v))
		for key, child := range v {
			out[key] = cloneJSONValue(child)
		}
		return out
	case []any:
		out := make([]any, len(v))
		for i, child := range v {
			out[i] = cloneJSONValue(child)
		}
		return out
	default:
		return v
	}
}

func canonicalScalarKey(value any) (string, bool) {
	switch v := value.(type) {
	case nil:
		return "null:", true
	case bool:
		if v {
			return "bool:true", true
		}
		return "bool:false", true
	case string:
		return "string:" + v, true
	case json.Number:
		if rat, ok := numberRat(v); ok {
			return "number:" + rat.RatString(), true
		}
		return "number:" + v.String(), true
	case float64:
		return "number:" + new(big.Rat).SetFloat64(v).RatString(), true
	case float32:
		return "number:" + new(big.Rat).SetFloat64(float64(v)).RatString(), true
	case int:
		return "number:" + big.NewRat(int64(v), 1).RatString(), true
	case int8:
		return "number:" + big.NewRat(int64(v), 1).RatString(), true
	case int16:
		return "number:" + big.NewRat(int64(v), 1).RatString(), true
	case int32:
		return "number:" + big.NewRat(int64(v), 1).RatString(), true
	case int64:
		return "number:" + big.NewRat(v, 1).RatString(), true
	case uint:
		return "number:" + new(big.Rat).SetInt(new(big.Int).SetUint64(uint64(v))).RatString(), true
	case uint8:
		return "number:" + big.NewRat(int64(v), 1).RatString(), true
	case uint16:
		return "number:" + big.NewRat(int64(v), 1).RatString(), true
	case uint32:
		return "number:" + big.NewRat(int64(v), 1).RatString(), true
	case uint64:
		return "number:" + new(big.Rat).SetInt(new(big.Int).SetUint64(v)).RatString(), true
	default:
		return "", false
	}
}

func numberRat(value json.Number) (*big.Rat, bool) {
	rat, ok := new(big.Rat).SetString(value.String())
	if ok {
		return rat, true
	}
	number, err := strconv.ParseFloat(value.String(), 64)
	if err != nil {
		return nil, false
	}
	return new(big.Rat).SetFloat64(number), true
}

func compareScalars(left, right any) (int, bool) {
	leftKey, leftOK := canonicalScalarKey(left)
	rightKey, rightOK := canonicalScalarKey(right)
	if !leftOK || !rightOK {
		return 0, false
	}
	leftType := strings.SplitN(leftKey, ":", 2)[0]
	rightType := strings.SplitN(rightKey, ":", 2)[0]
	if leftType != rightType {
		return 0, false
	}
	switch leftType {
	case "null":
		return 0, true
	case "bool":
		if left.(bool) == right.(bool) {
			return 0, true
		}
		if !left.(bool) {
			return -1, true
		}
		return 1, true
	case "string":
		return strings.Compare(left.(string), right.(string)), true
	case "number":
		leftRat, ok := numberRatForAny(left)
		if !ok {
			return 0, false
		}
		rightRat, ok := numberRatForAny(right)
		if !ok {
			return 0, false
		}
		return leftRat.Cmp(rightRat), true
	default:
		return 0, false
	}
}

func numberRatForAny(value any) (*big.Rat, bool) {
	if number, ok := value.(json.Number); ok {
		return numberRat(number)
	}
	switch v := value.(type) {
	case float64:
		return new(big.Rat).SetFloat64(v), true
	case float32:
		return new(big.Rat).SetFloat64(float64(v)), true
	case int:
		return big.NewRat(int64(v), 1), true
	case int8:
		return big.NewRat(int64(v), 1), true
	case int16:
		return big.NewRat(int64(v), 1), true
	case int32:
		return big.NewRat(int64(v), 1), true
	case int64:
		return big.NewRat(v, 1), true
	case uint:
		return new(big.Rat).SetInt(new(big.Int).SetUint64(uint64(v))), true
	case uint8:
		return big.NewRat(int64(v), 1), true
	case uint16:
		return big.NewRat(int64(v), 1), true
	case uint32:
		return big.NewRat(int64(v), 1), true
	case uint64:
		return new(big.Rat).SetInt(new(big.Int).SetUint64(v)), true
	default:
		return nil, false
	}
}

func sortedStringKeys(values map[string]any) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

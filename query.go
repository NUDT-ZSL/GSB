package docquery

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
)

// Query is the normalized, immutable-by-convention query representation. A
// single normalized text and SHA-256 identifier are derived from this shape.
type Query struct {
	Collection string    `json:"collection"`
	Filter     *Filter   `json:"filter,omitempty"`
	Projection []string  `json:"projection,omitempty"`
	Sort       []SortKey `json:"sort,omitempty"`
	Offset     int       `json:"offset"`
	Limit      *int      `json:"limit,omitempty"`
}

type SortKey struct {
	Path      string `json:"path"`
	Direction string `json:"direction"`
}

type RangeBounds struct {
	GT  any
	GTE any
	LT  any
	LTE any
}

// Filter supports eq, in, range, and, and or. The custom JSON form is canonical
// and includes all values needed for a stable query identifier.
type Filter struct {
	Op     string
	Path   string
	Value  any
	Values []any
	Bounds RangeBounds
	Args   []Filter
}

func (f Filter) MarshalJSON() ([]byte, error) {
	value := make(map[string]any, 8)
	value["op"] = f.Op
	switch f.Op {
	case "eq":
		value["path"] = f.Path
		value["value"] = f.Value
	case "in":
		value["path"] = f.Path
		value["values"] = f.Values
	case "range":
		value["path"] = f.Path
		if f.Bounds.GT != nil {
			value["gt"] = f.Bounds.GT
		}
		if f.Bounds.GTE != nil {
			value["gte"] = f.Bounds.GTE
		}
		if f.Bounds.LT != nil {
			value["lt"] = f.Bounds.LT
		}
		if f.Bounds.LTE != nil {
			value["lte"] = f.Bounds.LTE
		}
	case "and", "or":
		value["args"] = f.Args
	default:
		return nil, fmt.Errorf("unsupported filter op %q", f.Op)
	}
	return json.Marshal(value)
}

// CanonicalText returns compact, key-order-independent normalized query text.
func (q *Query) CanonicalText() string {
	if q == nil {
		return ""
	}
	body, _ := json.Marshal(q)
	return string(body)
}

// ID returns the SHA-256 of CanonicalText.
func (q *Query) ID() string {
	if q == nil {
		return ""
	}
	sum := sha256.Sum256([]byte(q.CanonicalText()))
	return hex.EncodeToString(sum[:])
}

func queryError(path, format string, args ...any) *BusinessError {
	e := newBusinessError(CodeQuery, format, args...)
	e.FieldPath = path
	return e
}

// ParseQuery parses and normalizes a JSON query. Whitespace, JSON object key
// order, query keyword case, projection order, and IN order do not affect the
// normalized text. Field paths and data values remain case-sensitive.
func ParseQuery(data []byte) (*Query, error) {
	raw, err := decodeRawObject(data)
	if err != nil {
		return nil, queryError("$", "invalid query JSON: %v", err)
	}
	normalized, err := normalizeRawKeys(raw)
	if err != nil {
		return nil, queryError("$", "invalid query JSON: %v", err)
	}
	if err := rejectUnknownKeys(normalized, "collection", "filter", "projection", "sort", "offset", "limit"); err != nil {
		return nil, queryError("$", "%v", err)
	}
	collectionRaw, ok := normalized["collection"]
	if !ok {
		return nil, queryError("collection", "query is missing collection")
	}
	collection, err := decodeJSONString(collectionRaw)
	if err != nil {
		return nil, queryError("collection", "collection must be a string")
	}
	collection = strings.ToLower(strings.TrimSpace(collection))
	if !validSingleSegment(collection) {
		return nil, queryError("collection", "invalid collection name %q", collection)
	}

	query := &Query{Collection: collection}
	if filterRaw, ok := normalized["filter"]; ok {
		filter, err := parseFilter(filterRaw)
		if err != nil {
			return nil, err
		}
		query.Filter = filter
	}
	if projectionRaw, ok := normalized["projection"]; ok {
		projection, err := parseProjection(projectionRaw)
		if err != nil {
			return nil, err
		}
		query.Projection = projection
	}
	if sortRaw, ok := normalized["sort"]; ok {
		sortKeys, err := parseSort(sortRaw)
		if err != nil {
			return nil, err
		}
		query.Sort = sortKeys
	}
	if offsetRaw, ok := normalized["offset"]; ok {
		offset, err := decodeJSONInt(offsetRaw)
		if err != nil || offset < 0 {
			return nil, queryError("offset", "offset must be a non-negative integer")
		}
		query.Offset = offset
	}
	if limitRaw, ok := normalized["limit"]; ok {
		limit, err := decodeJSONInt(limitRaw)
		if err != nil || limit < 0 {
			return nil, queryError("limit", "limit must be a non-negative integer")
		}
		query.Limit = &limit
	}
	return query, nil
}

func parseFilter(raw json.RawMessage) (*Filter, error) {
	if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
		return nil, nil
	}
	object, err := decodeRawObject(raw)
	if err != nil {
		return nil, queryError("filter", "filter must be an object: %v", err)
	}
	normalized, err := normalizeRawKeys(object)
	if err != nil {
		return nil, queryError("filter", "invalid filter: %v", err)
	}
	opRaw, ok := normalized["op"]
	if !ok {
		return nil, queryError("filter.op", "filter is missing op")
	}
	op, err := decodeJSONString(opRaw)
	if err != nil {
		return nil, queryError("filter.op", "filter op must be a string")
	}
	op = strings.ToLower(strings.TrimSpace(op))
	switch op {
	case "and", "or":
		if err := rejectUnknownKeys(normalized, "op", "args"); err != nil {
			return nil, queryError("filter", "%v", err)
		}
		argsRaw, ok := normalized["args"]
		if !ok {
			return nil, queryError("filter.args", "%s filter requires args", op)
		}
		var childrenRaw []json.RawMessage
		if err := json.Unmarshal(argsRaw, &childrenRaw); err != nil {
			return nil, queryError("filter.args", "%s filter args must be an array", op)
		}
		children := make([]Filter, 0, len(childrenRaw))
		for _, childRaw := range childrenRaw {
			child, err := parseFilter(childRaw)
			if err != nil {
				return nil, err
			}
			if child == nil {
				return nil, queryError("filter.args", "null is not a valid child filter")
			}
			if child.Op == op {
				children = append(children, child.Args...)
			} else {
				children = append(children, *child)
			}
		}
		children, err = normalizeFilterArgs(children)
		if err != nil {
			return nil, err
		}
		if len(children) == 0 {
			return nil, queryError("filter.args", "%s filter requires at least one argument", op)
		}
		if len(children) == 1 {
			return &children[0], nil
		}
		return &Filter{Op: op, Args: children}, nil
	case "eq":
		if err := rejectUnknownKeys(normalized, "op", "path", "value"); err != nil {
			return nil, queryError("filter", "%v", err)
		}
		path, err := parseFilterPath(normalized)
		if err != nil {
			return nil, err
		}
		valueRaw, ok := normalized["value"]
		if !ok {
			return nil, queryError(path, "eq filter requires value")
		}
		value, err := decodeJSON(valueRaw)
		if err != nil {
			return nil, queryError(path, "invalid eq value: %v", err)
		}
		value, err = normalizeQueryScalar(value)
		if err != nil {
			return nil, queryError(path, "invalid eq value: %v", err)
		}
		return &Filter{Op: "eq", Path: path, Value: value}, nil
	case "in":
		if err := rejectUnknownKeys(normalized, "op", "path", "values"); err != nil {
			return nil, queryError("filter", "%v", err)
		}
		path, err := parseFilterPath(normalized)
		if err != nil {
			return nil, err
		}
		valuesRaw, ok := normalized["values"]
		if !ok {
			return nil, queryError(path, "in filter requires values")
		}
		value, err := decodeJSON(valuesRaw)
		if err != nil {
			return nil, queryError(path, "invalid in values: %v", err)
		}
		list, ok := value.([]any)
		if !ok || len(list) == 0 {
			return nil, queryError(path, "in filter values must be a non-empty array")
		}
		type normalizedValue struct {
			key   string
			value any
			text  string
		}
		unique := make(map[string]normalizedValue, len(list))
		for _, item := range list {
			scalar, err := normalizeQueryScalar(item)
			if err != nil {
				return nil, queryError(path, "invalid in value: %v", err)
			}
			key, ok := canonicalScalarKey(scalar)
			if !ok {
				return nil, queryError(path, "invalid in value type %T", scalar)
			}
			text, _ := json.Marshal(scalar)
			candidate := normalizedValue{key: key, value: scalar, text: string(text)}
			if previous, exists := unique[key]; !exists || candidate.text < previous.text {
				unique[key] = candidate
			}
		}
		values := make([]normalizedValue, 0, len(unique))
		for _, item := range unique {
			values = append(values, item)
		}
		sort.Slice(values, func(i, j int) bool {
			if values[i].key != values[j].key {
				return values[i].key < values[j].key
			}
			return values[i].text < values[j].text
		})
		result := make([]any, 0, len(values))
		for _, item := range values {
			result = append(result, item.value)
		}
		return &Filter{Op: "in", Path: path, Values: result}, nil
	case "range", "gt", "gte", "lt", "lte":
		var allowed []string
		if op == "range" {
			allowed = []string{"op", "path", "gt", "gte", "lt", "lte"}
		} else {
			allowed = []string{"op", "path", "value"}
		}
		if err := rejectUnknownKeys(normalized, allowed...); err != nil {
			return nil, queryError("filter", "%v", err)
		}
		path, err := parseFilterPath(normalized)
		if err != nil {
			return nil, err
		}
		bounds := RangeBounds{}
		if op == "range" {
			hasBound := false
			for _, name := range []string{"gt", "gte", "lt", "lte"} {
				boundRaw, ok := normalized[name]
				if !ok {
					continue
				}
				value, err := decodeJSON(boundRaw)
				if err != nil {
					return nil, queryError(path, "invalid %s bound: %v", name, err)
				}
				value, err = normalizeQueryScalar(value)
				if err != nil {
					return nil, queryError(path, "invalid %s bound: %v", name, err)
				}
				if !isRangeScalar(value) {
					return nil, queryError(path, "range bound %s must be a string or number", name)
				}
				hasBound = true
				switch name {
				case "gt":
					bounds.GT = value
				case "gte":
					bounds.GTE = value
				case "lt":
					bounds.LT = value
				case "lte":
					bounds.LTE = value
				}
			}
			if !hasBound {
				return nil, queryError(path, "range filter requires at least one bound")
			}
		} else {
			valueRaw, ok := normalized["value"]
			if !ok {
				return nil, queryError(path, "%s filter requires value", op)
			}
			value, err := decodeJSON(valueRaw)
			if err != nil {
				return nil, queryError(path, "invalid %s value: %v", op, err)
			}
			value, err = normalizeQueryScalar(value)
			if err != nil || !isRangeScalar(value) {
				return nil, queryError(path, "%s value must be a string or number", op)
			}
			switch op {
			case "gt":
				bounds.GT = value
			case "gte":
				bounds.GTE = value
			case "lt":
				bounds.LT = value
			case "lte":
				bounds.LTE = value
			}
		}
		return &Filter{Op: "range", Path: path, Bounds: bounds}, nil
	default:
		return nil, queryError("filter.op", "unsupported filter op %q", op)
	}
}

func parseFilterPath(raw map[string]json.RawMessage) (string, error) {
	pathRaw, ok := raw["path"]
	if !ok {
		return "", queryError("filter.path", "filter is missing path")
	}
	path, err := decodeJSONString(pathRaw)
	if err != nil {
		return "", queryError("filter.path", "filter path must be a string")
	}
	if err := ValidateFieldPath(path); err != nil {
		return "", AsBusinessError(CodeQuery, err)
	}
	return path, nil
}

func isRangeScalar(value any) bool {
	switch value.(type) {
	case string, json.Number:
		return true
	default:
		return false
	}
}

func normalizeFilterArgs(args []Filter) ([]Filter, error) {
	type canonicalArg struct {
		text   string
		filter Filter
	}
	unique := make(map[string]canonicalArg, len(args))
	for _, arg := range args {
		body, err := json.Marshal(arg)
		if err != nil {
			return nil, queryError("filter.args", "cannot canonicalize filter: %v", err)
		}
		text := string(body)
		unique[text] = canonicalArg{text: text, filter: arg}
	}
	result := make([]canonicalArg, 0, len(unique))
	for _, item := range unique {
		result = append(result, item)
	}
	sort.Slice(result, func(i, j int) bool { return result[i].text < result[j].text })
	filters := make([]Filter, 0, len(result))
	for _, item := range result {
		filters = append(filters, item.filter)
	}
	return filters, nil
}

func parseProjection(raw json.RawMessage) ([]string, error) {
	value, err := decodeJSON(raw)
	if err != nil {
		return nil, queryError("projection", "projection must be an array")
	}
	list, ok := value.([]any)
	if !ok {
		return nil, queryError("projection", "projection must be an array")
	}
	unique := make(map[string]struct{}, len(list))
	fields := make([]string, 0, len(list))
	for _, item := range list {
		path, ok := item.(string)
		if !ok {
			return nil, queryError("projection", "projection entries must be strings")
		}
		if err := ValidateFieldPath(path); err != nil {
			return nil, AsBusinessError(CodeQuery, err)
		}
		if _, exists := unique[path]; exists {
			continue
		}
		unique[path] = struct{}{}
		fields = append(fields, path)
	}
	sort.Strings(fields)
	filtered := make([]string, 0, len(fields))
	for _, path := range fields {
		covered := false
		for _, ancestor := range filtered {
			if strings.HasPrefix(path, ancestor+".") {
				covered = true
				break
			}
		}
		if !covered {
			filtered = append(filtered, path)
		}
	}
	return filtered, nil
}

func parseSort(raw json.RawMessage) ([]SortKey, error) {
	value, err := decodeJSON(raw)
	if err != nil {
		return nil, queryError("sort", "sort must be an array")
	}
	list, ok := value.([]any)
	if !ok {
		return nil, queryError("sort", "sort must be an array")
	}
	keys := make([]SortKey, 0, len(list))
	seen := make(map[string]struct{}, len(list))
	for index, item := range list {
		object, ok := item.(map[string]any)
		if !ok {
			return nil, queryError("sort", "sort entry %d must be an object", index)
		}
		normalized := make(map[string]any, len(object))
		originalKeys := make(map[string]string, len(object))
		for key, child := range object {
			normalizedKey := strings.ToLower(key)
			if previous, exists := originalKeys[normalizedKey]; exists {
				return nil, queryError("sort", "sort entry %d has duplicate keys %q and %q", index, previous, key)
			}
			originalKeys[normalizedKey] = key
			normalized[normalizedKey] = child
		}
		pathValue, ok := normalized["path"]
		if !ok {
			return nil, queryError("sort", "sort entry %d is missing path", index)
		}
		path, ok := pathValue.(string)
		if !ok {
			return nil, queryError("sort", "sort entry %d path must be a string", index)
		}
		if err := ValidateFieldPath(path); err != nil {
			return nil, AsBusinessError(CodeQuery, err)
		}
		direction := "asc"
		directionValue, hasDirection := normalized["direction"]
		dirValue, hasDir := normalized["dir"]
		if hasDirection && hasDir {
			return nil, queryError("sort", "sort entry %d cannot have both direction and dir", index)
		}
		if hasDir {
			directionValue = dirValue
		}
		if hasDirection || hasDir {
			text, ok := directionValue.(string)
			if !ok {
				return nil, queryError("sort", "sort entry %d direction must be a string", index)
			}
			direction = strings.ToLower(strings.TrimSpace(text))
		}
		if direction != "asc" && direction != "desc" {
			return nil, queryError("sort", "sort entry %d direction must be asc or desc", index)
		}
		for key := range normalized {
			if key != "path" && key != "direction" && key != "dir" {
				return nil, queryError("sort", "sort entry %d has unsupported key %q", index, key)
			}
		}
		if _, duplicate := seen[path]; duplicate {
			return nil, queryError("sort", "sort path %q appears more than once", path)
		}
		seen[path] = struct{}{}
		keys = append(keys, SortKey{Path: path, Direction: direction})
	}
	return keys, nil
}

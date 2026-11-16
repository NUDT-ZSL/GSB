package docquery

import (
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strings"
)

var pathSegmentPattern = regexp.MustCompile(`^[A-Za-z0-9_]+$`)

// Document is a nested JSON object. Numeric values loaded from JSON use
// json.Number so serialization does not silently alter their representation.
type Document map[string]any

// ValidateFieldPath reports an E_QUERY error when a dot-separated path has an
// empty or unsupported segment. Document validation converts this into E_DOC.
func ValidateFieldPath(path string) error {
	if path == "" {
		return newBusinessError(CodeQuery, "field path must not be empty")
	}
	for _, segment := range strings.Split(path, ".") {
		if !pathSegmentPattern.MatchString(segment) {
			return newBusinessError(CodeQuery, "invalid field path %q: segment %q must contain only letters, digits, or underscore", path, segment)
		}
	}
	return nil
}

func validSingleSegment(value string) bool {
	return pathSegmentPattern.MatchString(value)
}

func splitFieldPath(path string) []string {
	return strings.Split(path, ".")
}

// GetField resolves a dot path through nested objects. Arrays are not part of
// the document model and therefore never resolve to a value.
func GetField(doc Document, path string) (any, bool) {
	segments := splitFieldPath(path)
	var current any = map[string]any(doc)
	for _, segment := range segments {
		object, ok := current.(map[string]any)
		if !ok {
			if typed, typedOK := current.(Document); typedOK {
				object = map[string]any(typed)
			} else {
				return nil, false
			}
		}
		current, ok = object[segment]
		if !ok {
			return nil, false
		}
	}
	return current, true
}

func setField(doc Document, path string, value any) bool {
	segments := splitFieldPath(path)
	current := map[string]any(doc)
	for i, segment := range segments {
		if i == len(segments)-1 {
			current[segment] = cloneJSONValue(value)
			return true
		}
		next, ok := current[segment]
		if !ok {
			child := make(map[string]any)
			current[segment] = child
			current = child
			continue
		}
		child, ok := next.(map[string]any)
		if !ok {
			if typed, typedOK := next.(Document); typedOK {
				child = map[string]any(typed)
			} else {
				return false
			}
		}
		current = child
	}
	return false
}

func cloneDocument(doc Document) Document {
	out := make(Document, len(doc))
	for key, value := range doc {
		out[key] = cloneJSONValue(value)
	}
	return out
}

func validateDocumentShape(value any, index int, path string) *BusinessError {
	switch typed := value.(type) {
	case map[string]any:
		keys := make([]string, 0, len(typed))
		for key := range typed {
			keys = append(keys, key)
		}
		sort.Strings(keys)
		for _, key := range keys {
			childPath := key
			if path != "" {
				childPath = path + "." + key
			}
			if childPath == "" {
				childPath = "$"
			}
			if !validSingleSegment(key) {
				return docError(index, childPath, "invalid field path %q: segment %q must contain only letters, digits, or underscore", childPath, key)
			}
			if err := validateDocumentShape(typed[key], index, childPath); err != nil {
				return err
			}
		}
		return nil
	case Document:
		return validateDocumentShape(map[string]any(typed), index, path)
	case []any:
		if path == "" {
			path = "$"
		}
		return docError(index, path, "array values are not supported at field path %q", path)
	case string, bool, json.Number, nil:
		return nil
	case float32, float64, int, int8, int16, int32, int64, uint, uint8, uint16, uint32, uint64:
		return nil
	default:
		if path == "" {
			path = "$"
		}
		return docError(index, path, "unsupported value type %T at field path %q", typed, path)
	}
}

func primaryKeyValue(doc Document, primaryKey string) (any, error) {
	value, ok := GetField(doc, primaryKey)
	if !ok {
		return nil, fmt.Errorf("primary key field %q is missing", primaryKey)
	}
	switch value.(type) {
	case string, json.Number, float64, float32, int, int8, int16, int32, int64, uint, uint8, uint16, uint32, uint64:
		return value, nil
	default:
		return nil, fmt.Errorf("primary key field %q must be a non-null string or number", primaryKey)
	}
}

func documentCanonicalBytes(doc Document) []byte {
	data, _ := json.Marshal(doc)
	return data
}

package docquery

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"
)

// Collection is an immutable-by-convention document collection. Callers should
// use NewCollection, LoadCollection, or methods that return a new collection.
type Collection struct {
	Name       string     `json:"name"`
	PrimaryKey string     `json:"primary_key"`
	Documents  []Document `json:"documents"`
	Version    string     `json:"version"`
}

// LoadCollection reads and strongly validates a local collection file. Any
// failure is reported as E_DOC and the whole collection is rejected.
func LoadCollection(path string) (*Collection, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		e := newBusinessError(CodeDoc, "cannot read collection %q: %v", path, err)
		e.FieldPath = "$"
		return nil, e
	}
	return ParseCollection(data)
}

// ParseCollection validates a collection JSON document. The accepted top-level
// shape is {"name":"products","primary_key":"id","documents":[...]}; the
// "collection" alias is also accepted for the name field.
func ParseCollection(data []byte) (*Collection, error) {
	raw, err := decodeRawObject(data)
	if err != nil {
		e := newBusinessError(CodeDoc, "invalid collection JSON: %v", err)
		e.FieldPath = "$"
		return nil, e
	}
	normalized, err := normalizeRawKeys(raw)
	if err != nil {
		e := newBusinessError(CodeDoc, "invalid collection JSON: %v", err)
		e.FieldPath = "$"
		return nil, e
	}
	if err := rejectUnknownKeys(normalized, "name", "collection", "primary_key", "primarykey", "documents"); err != nil {
		e := newBusinessError(CodeDoc, "%v", err)
		e.FieldPath = "$"
		return nil, e
	}

	name, err := pickAliasString(normalized, "name", "collection")
	if err != nil {
		e := newBusinessError(CodeDoc, "%v", err)
		e.FieldPath = "name"
		return nil, e
	}
	primaryKey, err := pickAliasString(normalized, "primary_key", "primarykey")
	if err != nil {
		e := newBusinessError(CodeDoc, "%v", err)
		e.FieldPath = "primary_key"
		return nil, e
	}
	documentsRaw, ok := normalized["documents"]
	if !ok {
		e := newBusinessError(CodeDoc, "collection definition is missing documents")
		e.FieldPath = "documents"
		return nil, e
	}
	if trimmed := strings.TrimSpace(string(documentsRaw)); trimmed == "" || trimmed[0] != '[' {
		e := newBusinessError(CodeDoc, "documents must be an array")
		e.FieldPath = "documents"
		return nil, e
	}
	var raws []json.RawMessage
	if err := json.Unmarshal(documentsRaw, &raws); err != nil {
		e := newBusinessError(CodeDoc, "documents must be an array: %v", err)
		e.FieldPath = "documents"
		return nil, e
	}

	documents := make([]Document, 0, len(raws))
	for index, documentRaw := range raws {
		value, err := decodeJSON(documentRaw)
		if err != nil {
			e := docError(index, "$", "invalid document JSON: %v", err)
			return nil, e
		}
		object, ok := value.(map[string]any)
		if !ok {
			e := docError(index, "$", "document must be a JSON object")
			return nil, e
		}
		if err := validateDocumentShape(object, index, ""); err != nil {
			return nil, err
		}
		documents = append(documents, Document(object))
	}
	return NewCollection(name, primaryKey, documents)
}

func pickAliasString(raw map[string]json.RawMessage, aliases ...string) (string, error) {
	var foundKey string
	var foundValue string
	for _, alias := range aliases {
		valueRaw, ok := raw[alias]
		if !ok {
			continue
		}
		if foundKey != "" {
			return "", fmt.Errorf("fields %q and %q are aliases and cannot both be present", foundKey, alias)
		}
		value, err := decodeJSONString(valueRaw)
		if err != nil {
			return "", fmt.Errorf("field %q must be a string", alias)
		}
		foundKey = alias
		foundValue = strings.TrimSpace(value)
	}
	if foundKey == "" {
		return "", fmt.Errorf("missing required field %q", aliases[0])
	}
	return foundValue, nil
}

// NewCollection validates documents and computes a content-derived version.
// The returned collection owns a deep copy of documents.
func NewCollection(name, primaryKey string, documents []Document) (*Collection, error) {
	name = strings.TrimSpace(name)
	if name == "" {
		e := newBusinessError(CodeDoc, "collection name must not be empty")
		e.FieldPath = "name"
		return nil, e
	}
	if !validSingleSegment(name) {
		e := newBusinessError(CodeDoc, "invalid collection name %q: use letters, digits, or underscore", name)
		e.FieldPath = "name"
		return nil, e
	}
	if err := ValidateFieldPath(primaryKey); err != nil {
		return nil, &BusinessError{Code: CodeDoc, Message: err.Error(), FieldPath: primaryKey}
	}

	copied := make([]Document, 0, len(documents))
	firstByKey := make(map[string]int, len(documents))
	for index, doc := range documents {
		if err := validateDocumentShape(map[string]any(doc), index, ""); err != nil {
			return nil, err
		}
		value, err := primaryKeyValue(doc, primaryKey)
		if err != nil {
			return nil, docError(index, primaryKey, "%v", err)
		}
		key, ok := canonicalScalarKey(value)
		if !ok {
			return nil, docError(index, primaryKey, "primary key value at %q is not a supported scalar", primaryKey)
		}
		if previous, exists := firstByKey[key]; exists {
			return nil, docError(index, primaryKey, "duplicate primary key at %q (first seen at document index %d)", primaryKey, previous)
		}
		firstByKey[key] = index
		copied = append(copied, cloneDocument(doc))
	}

	collection := &Collection{
		Name:       name,
		PrimaryKey: primaryKey,
		Documents:  copied,
	}
	collection.Version = computeCollectionVersion(collection)
	return collection, nil
}

func computeCollectionVersion(collection *Collection) string {
	payload := struct {
		Name       string     `json:"name"`
		PrimaryKey string     `json:"primary_key"`
		Documents  []Document `json:"documents"`
	}{
		Name:       collection.Name,
		PrimaryKey: collection.PrimaryKey,
		Documents:  collection.Documents,
	}
	body, _ := json.Marshal(payload)
	sum := sha256.Sum256(append([]byte("docquery.collection.v1\x00"), body...))
	return hex.EncodeToString(sum[:])
}

func (c *Collection) Clone() *Collection {
	if c == nil {
		return nil
	}
	documents := make([]Document, len(c.Documents))
	for i, doc := range c.Documents {
		documents[i] = cloneDocument(doc)
	}
	return &Collection{
		Name:       c.Name,
		PrimaryKey: c.PrimaryKey,
		Documents:  documents,
		Version:    c.Version,
	}
}

func (c *Collection) primaryKeyMap() map[string]int {
	result := make(map[string]int, len(c.Documents))
	for index, doc := range c.Documents {
		value, err := primaryKeyValue(doc, c.PrimaryKey)
		if err != nil {
			continue
		}
		if key, ok := canonicalScalarKey(value); ok {
			result[key] = index
		}
	}
	return result
}

// Catalog enforces collection-name uniqueness. Names are unique case
// insensitively so query case folding remains well-defined.
type Catalog struct {
	collections map[string]*Collection
}

func NewCatalog() *Catalog {
	return &Catalog{collections: make(map[string]*Collection)}
}

func (c *Catalog) Add(collection *Collection) error {
	if collection == nil {
		return newBusinessError(CodeDoc, "collection must not be nil")
	}
	validated, err := NewCollection(collection.Name, collection.PrimaryKey, collection.Documents)
	if err != nil {
		return err
	}
	key := strings.ToLower(validated.Name)
	if previous, ok := c.collections[key]; ok {
		e := newBusinessError(CodeDoc, "duplicate collection name %q (already loaded as %q)", validated.Name, previous.Name)
		e.FieldPath = "name"
		e.Collection = validated.Name
		return e
	}
	c.collections[key] = validated
	return nil
}

func (c *Catalog) Get(name string) (*Collection, bool) {
	collection, ok := c.collections[strings.ToLower(name)]
	return collection, ok
}

func (c *Catalog) Names() []string {
	names := make([]string, 0, len(c.collections))
	for _, collection := range c.collections {
		names = append(names, collection.Name)
	}
	sort.Strings(names)
	return names
}

// LoadCatalog loads several collection files and rejects duplicate names as a
// single E_DOC failure.
func LoadCatalog(paths ...string) (*Catalog, error) {
	catalog := NewCatalog()
	for _, path := range paths {
		collection, err := LoadCollection(path)
		if err != nil {
			return nil, err
		}
		if err := catalog.Add(collection); err != nil {
			return nil, err
		}
	}
	return catalog, nil
}

type collectionPatch struct {
	Name    string
	Upserts []json.RawMessage
	Deletes []any
}

func parseCollectionPatch(data []byte) (*collectionPatch, error) {
	raw, err := decodeRawObject(data)
	if err != nil {
		e := newBusinessError(CodeDoc, "invalid update patch: %v", err)
		e.FieldPath = "$"
		return nil, e
	}
	normalized, err := normalizeRawKeys(raw)
	if err != nil {
		e := newBusinessError(CodeDoc, "invalid update patch: %v", err)
		e.FieldPath = "$"
		return nil, e
	}
	if err := rejectUnknownKeys(normalized, "name", "collection", "upserts", "deletes"); err != nil {
		e := newBusinessError(CodeDoc, "invalid update patch: %v", err)
		e.FieldPath = "$"
		return nil, e
	}
	name, err := pickAliasString(normalized, "name", "collection")
	if err != nil {
		e := newBusinessError(CodeDoc, "invalid update patch: %v", err)
		e.FieldPath = "name"
		return nil, e
	}
	patch := &collectionPatch{Name: name}
	if upsertsRaw, ok := normalized["upserts"]; ok {
		if trimmed := strings.TrimSpace(string(upsertsRaw)); trimmed == "" || trimmed[0] != '[' {
			e := newBusinessError(CodeDoc, "update patch upserts must be an array")
			e.FieldPath = "upserts"
			return nil, e
		}
		if err := json.Unmarshal(upsertsRaw, &patch.Upserts); err != nil {
			e := newBusinessError(CodeDoc, "update patch upserts must be an array: %v", err)
			e.FieldPath = "upserts"
			return nil, e
		}
	}
	if deletesRaw, ok := normalized["deletes"]; ok {
		value, err := decodeJSON(deletesRaw)
		if err != nil {
			e := newBusinessError(CodeDoc, "invalid update patch deletes: %v", err)
			e.FieldPath = "deletes"
			return nil, e
		}
		values, ok := value.([]any)
		if !ok {
			e := newBusinessError(CodeDoc, "update patch deletes must be an array")
			e.FieldPath = "deletes"
			return nil, e
		}
		for _, item := range values {
			scalar, err := normalizeQueryScalar(item)
			if err != nil {
				e := newBusinessError(CodeDoc, "invalid delete key in update patch: %v", err)
				e.FieldPath = "deletes"
				return nil, e
			}
			patch.Deletes = append(patch.Deletes, scalar)
		}
	}
	return patch, nil
}

// ApplyPatchFile applies an upsert/delete patch and returns a new collection.
// The original collection is not modified.
func (c *Collection) ApplyPatchFile(path string) (*Collection, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, newBusinessError(CodeDoc, "cannot read update patch %q: %v", path, err)
	}
	patch, err := parseCollectionPatch(data)
	if err != nil {
		return nil, err
	}
	return c.applyPatch(patch)
}

func (c *Collection) applyPatch(patch *collectionPatch) (*Collection, error) {
	if !strings.EqualFold(patch.Name, c.Name) {
		return nil, newBusinessError(CodeDoc, "update patch name %q does not match collection %q", patch.Name, c.Name)
	}
	documents := make([]Document, 0, len(c.Documents)+len(patch.Upserts))
	for _, doc := range c.Documents {
		documents = append(documents, cloneDocument(doc))
	}

	for index, raw := range patch.Upserts {
		value, err := decodeJSON(raw)
		if err != nil {
			return nil, docError(index, "$", "invalid upsert document JSON: %v", err)
		}
		object, ok := value.(map[string]any)
		if !ok {
			return nil, docError(index, "$", "upsert document must be a JSON object")
		}
		doc := Document(object)
		if err := validateDocumentShape(object, index, ""); err != nil {
			return nil, err
		}
		keyValue, err := primaryKeyValue(doc, c.PrimaryKey)
		if err != nil {
			return nil, docError(index, c.PrimaryKey, "%v", err)
		}
		key, _ := canonicalScalarKey(keyValue)
		replaced := false
		for i, existing := range documents {
			existingKeyValue, err := primaryKeyValue(existing, c.PrimaryKey)
			if err != nil {
				continue
			}
			existingKey, _ := canonicalScalarKey(existingKeyValue)
			if existingKey == key {
				documents[i] = cloneDocument(doc)
				replaced = true
				break
			}
		}
		if !replaced {
			documents = append(documents, cloneDocument(doc))
		}
	}

	if len(patch.Deletes) > 0 {
		deleteKeys := make(map[string]struct{}, len(patch.Deletes))
		for _, value := range patch.Deletes {
			key, ok := canonicalScalarKey(value)
			if !ok {
				return nil, newBusinessError(CodeDoc, "invalid delete key type %T", value)
			}
			deleteKeys[key] = struct{}{}
		}
		filtered := make([]Document, 0, len(documents))
		found := make(map[string]bool, len(deleteKeys))
		for _, doc := range documents {
			value, err := primaryKeyValue(doc, c.PrimaryKey)
			if err != nil {
				filtered = append(filtered, doc)
				continue
			}
			key, _ := canonicalScalarKey(value)
			if _, remove := deleteKeys[key]; remove {
				found[key] = true
				continue
			}
			filtered = append(filtered, doc)
		}
		for key := range deleteKeys {
			if !found[key] {
				return nil, newBusinessError(CodeDoc, "delete target %q does not exist", key)
			}
		}
		documents = filtered
	}

	return NewCollection(c.Name, c.PrimaryKey, documents)
}

// Package docquery implements a deterministic local JSON document query engine.
//
// Collections are strongly validated, queries are normalized to stable text and
// SHA-256 identifiers, plans use a fixed scan/index cost rule, and executions
// produce byte-stable result envelopes. Sessions support version-bound plan
// caches, statistics snapshots, self-contained saved snapshots, scan/index
// consistency validation, and structured business errors.
package docquery

# Local deterministic sample

All input files are UTF-8 JSON/JSONL and are intentionally small. The sample exercises:

- weighted template and slot sampling, including zero-weight variants and sources;
- zero-target and all-zero-weight task skips;
- an instruction exactly at the soft length breakpoint (68 characters);
- candidates whose Jaccard similarity is `1/3`, adjacent to the configured threshold;
- missing-field, out-of-range-weight, empty-subword, and precision-sensitive injections.

The configuration is processed as data only. No model, network, or external sample is used.


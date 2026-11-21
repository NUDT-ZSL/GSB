"""Restricted query subset: field equality, range, multi-field conjunction,
and fragment fetch-back for hits. A failing template never interrupts the
other templates in the same batch and never pollutes committed state."""

from .errors import BusinessError, ErrorCode
from .stablejson import bytes_sha256, digest


def validate_template(template):
    """Structural validation of a single query template."""
    if not isinstance(template, dict):
        raise BusinessError(
            ErrorCode.E_QUERY_SYNTAX, "template must be a JSON object"
        )
    template_id = template.get("template_id")
    if not isinstance(template_id, str) or not template_id:
        raise BusinessError(
            ErrorCode.E_QUERY_SYNTAX,
            "template requires a non-empty template_id",
        )
    op = template.get("op")
    if op == "and":
        clauses = template.get("clauses")
        if not isinstance(clauses, list) or not clauses:
            raise BusinessError(
                ErrorCode.E_QUERY_SYNTAX,
                "'and' template requires a non-empty clauses list",
                template_id=template_id,
            )
        for clause in clauses:
            _validate_clause(clause, template_id)
    elif op in ("eq", "range"):
        _validate_clause(template, template_id)
    else:
        raise BusinessError(
            ErrorCode.E_QUERY_SYNTAX,
            "unsupported op; expected 'eq', 'range' or 'and'",
            template_id=template_id,
            op=op,
        )
    fetch = template.get("fetch_fragments", False)
    if not isinstance(fetch, bool):
        raise BusinessError(
            ErrorCode.E_QUERY_SYNTAX,
            "fetch_fragments must be a boolean",
            template_id=template_id,
        )


def _validate_clause(clause, template_id):
    if not isinstance(clause, dict) or not isinstance(clause.get("field"), str):
        raise BusinessError(
            ErrorCode.E_QUERY_SYNTAX,
            "clause requires a string field name",
            template_id=template_id,
        )
    has_eq = "eq" in clause
    has_range = "range" in clause
    if has_eq == has_range:
        raise BusinessError(
            ErrorCode.E_QUERY_SYNTAX,
            "clause requires exactly one of 'eq' or 'range'",
            template_id=template_id,
            field=clause.get("field"),
        )
    if has_range:
        bounds = clause["range"]
        if (
            not isinstance(bounds, list)
            or len(bounds) != 2
            or any(isinstance(b, bool) or not isinstance(b, (int, float))
                   for b in bounds)
            or bounds[0] > bounds[1]
        ):
            raise BusinessError(
                ErrorCode.E_QUERY_SYNTAX,
                "range must be [low, high] of numbers with low <= high",
                template_id=template_id,
                field=clause.get("field"),
            )
    if has_eq:
        value = clause["eq"]
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise BusinessError(
                ErrorCode.E_QUERY_SYNTAX,
                "eq value must be a string or number",
                template_id=template_id,
                field=clause.get("field"),
            )


def _clauses_of(template):
    if template["op"] == "and":
        return list(template["clauses"])
    return [{k: template[k] for k in ("field", "eq", "range") if k in template}]


def _eval_clause(clause, index, template_id):
    field = clause["field"]
    fi = index.field_index(field)
    if fi is None:
        return set()
    if "range" in clause:
        if fi.value_type() == "string":
            raise BusinessError(
                ErrorCode.E_PREDICATE_TYPE,
                "range predicate on a string-typed field",
                template_id=template_id,
                field=field,
                field_type="string",
                predicate="range",
            )
        return set(fi.range(clause["range"][0], clause["range"][1]))
    value = clause["eq"]
    ftype = fi.value_type()
    if ftype == "string" and not isinstance(value, str):
        raise BusinessError(
            ErrorCode.E_PREDICATE_TYPE,
            "eq predicate type incompatible with string-typed field",
            template_id=template_id,
            field=field,
            field_type="string",
            predicate="eq",
        )
    if ftype == "number" and isinstance(value, str):
        raise BusinessError(
            ErrorCode.E_PREDICATE_TYPE,
            "eq predicate type incompatible with number-typed field",
            template_id=template_id,
            field=field,
            field_type="number",
            predicate="eq",
        )
    return set(fi.eq(value))


def execute_template(template, index, fragment_store, generation):
    """Execute one template against the unified index. Returns a
    JSON-serializable result dict; errors are captured, not raised."""
    template_id = template.get("template_id")
    try:
        validate_template(template)
        clauses = _clauses_of(template)
        matched_fields = sorted({c["field"] for c in clauses})
        hit_ids = None
        for clause in clauses:
            ids = _eval_clause(clause, index, template_id)
            hit_ids = ids if hit_ids is None else (hit_ids & ids)
        hits = []
        fetched = 0
        failed = 0
        for doc_id in sorted(hit_ids):
            provenance = index.provenance(doc_id)
            fields = {}
            fragments = []
            fetch_failed = False
            for field in matched_fields:
                rec = provenance.get(field)
                if rec is None:
                    continue
                fields[field] = {
                    "value": rec["value"],
                    "source": rec["source"],
                    "version": rec["version"],
                    "rule": rec["rule"],
                }
                if template.get("fetch_fragments") and rec.get("span"):
                    span = rec["span"]
                    try:
                        data = fragment_store.fetch_span(
                            span["fragment"],
                            span["offset"],
                            span["length"],
                            doc_id=doc_id,
                            field=field,
                        )
                        fragments.append(
                            {
                                "field": field,
                                "checksum": span["fragment"],
                                "offset": span["offset"],
                                "length": span["length"],
                                "content_digest": bytes_sha256(data),
                            }
                        )
                        fetched += 1
                    except BusinessError as exc:
                        failed += 1
                        fetch_failed = True
                        fragments.append(
                            {
                                "field": field,
                                "checksum": span["fragment"],
                                "offset": span["offset"],
                                "length": span["length"],
                                "error": exc.to_dict(),
                            }
                        )
            hit = {
                "doc_id": doc_id,
                "fields": fields,
                "fragments": fragments,
                "index_generation": generation,
            }
            if fetch_failed:
                hit["fetch_status"] = "fragment_checksum_error"
            hits.append(hit)
        status = "ok" if hits else "no_hit"
        return {
            "template_id": template_id,
            "status": status,
            "hits": hits,
            "scanned_fields": matched_fields,
            "fetched_fragments": fetched,
            "failed_fetches": failed,
        }
    except BusinessError as exc:
        return {
            "template_id": template_id,
            "status": "error",
            "error": exc.to_dict(),
            "hits": [],
            "scanned_fields": [],
            "fetched_fragments": 0,
            "failed_fetches": 0,
        }


def execute_batch(templates, index, fragment_store, generation):
    """Execute a whole batch; per-template isolation is guaranteed."""
    results = [
        execute_template(t, index, fragment_store, generation)
        for t in templates
    ]
    scan = digest(
        {
            field: index.field_index(field).interval_digest()
            for field in sorted(index.fields)
        }
    )
    fetched = sum(r["fetched_fragments"] for r in results)
    failed = sum(r["failed_fetches"] for r in results) + sum(
        1 for r in results if r["status"] == "error"
    )
    return {
        "results": results,
        "scan_digest": scan,
        "fetched": fetched,
        "failed": failed,
        "result_digest": digest(results),
    }

"""Business error codes (E_xxx) shared by the whole pipeline."""

# Successful run; at least one track contributed audio.
E_OK = "E_OK"
# Manifest JSON file does not exist on disk.
E_MISSING = "E_MISSING"
# Manifest JSON is syntactically invalid or fails schema validation.
E_SCHEMA = "E_SCHEMA"
# Target sample rate is outside the allowed set.
E_RATE = "E_RATE"
# A track WAV violates the required format (sample rate / bit depth / channels).
E_FORMAT = "E_FORMAT"
# Every track failed or produced no usable samples.
E_EMPTY = "E_EMPTY"
# Output (WAV / report) could not be written or replaced atomically.
E_WRITE = "E_WRITE"

# Process exit codes (kept distinct from the business code strings).
EXIT_CODES = {
    E_OK: 0,
    E_MISSING: 2,
    E_SCHEMA: 3,
    E_RATE: 4,
    E_FORMAT: 5,
    E_EMPTY: 6,
    E_WRITE: 7,
}


def describe(code):
    """Human-readable summary for a business code."""
    return {
        E_OK: "ok",
        E_MISSING: "manifest or track file missing",
        E_SCHEMA: "manifest schema invalid",
        E_RATE: "target sample rate not allowed",
        E_FORMAT: "track format not supported (rate/bit-depth/channel)",
        E_EMPTY: "no usable audio after alignment",
        E_WRITE: "output write/replace failed",
    }.get(code, "unknown error")

# Runtime directories

- `cache/`: latest response summary and recent business errors.
- `snapshots/active.json`: the single active, fully checksummed snapshot.
- `snapshots/meta.json`: monotonic version and active/invalidated status.
- `snapshots/invalidated/`: archived snapshots made invalid by precision changes,
  explicit resets, or demo resets.

Snapshots are replaced as a whole using atomic file replacement. No timestamp is
part of matrix, coordinate, or checksum content.

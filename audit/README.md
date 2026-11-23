# Audit runtime directory

`audit/` is the default Windows-local output directory for SQLite databases,
WAL side files, and lock files produced by the CLI and examples.

Typical generated files:

- `audit.db`
- `audit.db-wal`
- `audit.db-shm`
- `audit.db.lock`

These runtime files are ignored by Git. The directory itself is retained.

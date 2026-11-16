-- Deterministic seed rows for the transaction/lock laboratory.
-- The application creates the schema, clears both tables, then runs this file.
INSERT INTO lab_rows(row_key, payload, row_version, updated_by) VALUES
    ('account:A', '{"balance":100,"owner":"Alice"}', 0, 'seed'),
    ('account:B', '{"balance":100,"owner":"Bob"}',   0, 'seed'),
    ('item:1',    '{"name":"widget","quantity":10}', 0, 'seed'),
    ('item:2',    '{"name":"gadget","quantity":20}', 0, 'seed');

INSERT INTO lab_meta(meta_key, meta_value) VALUES ('data_version', '0')
ON CONFLICT(meta_key) DO UPDATE SET meta_value=excluded.meta_value;


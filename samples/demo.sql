-- Deterministic local sample for the SQL cardinality lab.
-- The markers are metadata only; they do not change SQLite table semantics.
-- @primary-key customers.id
-- @primary-key orders.id
-- @skew orders.status=paid:0.583333

CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    region TEXT NOT NULL,
    segment TEXT NOT NULL,
    lifetime_value INTEGER NOT NULL
);

INSERT INTO customers (id, region, segment, lifetime_value) VALUES
    (1, 'east', 'enterprise', 1200),
    (2, 'east', 'consumer', 180),
    (3, 'west', 'enterprise', 990),
    (4, 'west', 'consumer', 240),
    (5, 'north', 'enterprise', 1500),
    (6, 'north', 'consumer', 160),
    (7, 'south', 'enterprise', 860),
    (8, 'south', 'consumer', 210);

CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    amount INTEGER NOT NULL,
    channel TEXT NOT NULL
);

INSERT INTO orders (id, customer_id, status, amount, channel) VALUES
    (101, 1, 'paid',     420, 'web'),
    (102, 1, 'paid',     180, 'web'),
    (103, 2, 'paid',      90, 'mobile'),
    (104, 2, 'pending',  220, 'mobile'),
    (105, 3, 'paid',     640, 'web'),
    (106, 3, 'paid',     310, 'web'),
    (107, 4, 'refunded', 120, 'store'),
    (108, 4, 'paid',     155, 'store'),
    (109, 5, 'paid',     880, 'web'),
    (110, 5, 'paid',     470, 'web'),
    (111, 6, 'paid',      75, 'mobile'),
    (112, 6, 'pending',  260, 'mobile'),
    (113, 7, 'paid',     510, 'web'),
    (114, 7, 'paid',     390, 'store'),
    (115, 8, 'cancelled', 40, 'mobile'),
    (116, 8, 'paid',     130, 'store'),
    (117, 1, 'pending',  115, 'web'),
    (118, 2, 'paid',     260, 'store'),
    (119, 3, 'refunded', 210, 'web'),
    (120, 4, 'paid',     340, 'mobile'),
    (121, 5, 'paid',    1050, 'web'),
    (122, 6, 'pending',   85, 'store'),
    (123, 7, 'cancelled', 60, 'mobile'),
    (124, 8, 'paid',     195, 'web');

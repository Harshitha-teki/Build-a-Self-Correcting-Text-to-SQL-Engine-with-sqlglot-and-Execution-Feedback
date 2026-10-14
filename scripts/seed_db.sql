-- =============================================================================
-- seed_db.sql :: Intentional "messy" e-commerce analytics schema
-- Runs automatically on first Postgres container start via
-- /docker-entrypoint-initdb.d/. Deterministic: no random(), so row counts and
-- values are reproducible across runs and machines.
-- =============================================================================

-- Drop in dependency order so the script is idempotent when re-run manually.
DROP TABLE IF EXISTS line_items CASCADE;
DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS customers CASCADE;

-- -----------------------------------------------------------------------------
-- Messiness #1: primary key is `customer_id`, NOT `id`.
-- Messiness #2: `region` is NULLABLE (some customers have no region on file).
-- -----------------------------------------------------------------------------
CREATE TABLE customers (
    customer_id   SERIAL PRIMARY KEY,
    name          TEXT        NOT NULL,
    region        TEXT        -- nullable on purpose
);

-- -----------------------------------------------------------------------------
-- Messiness #3: foreign key to customers is named `cust_id`, which does not
-- match the referenced primary key `customer_id`. This is the classic
-- "ambiguous joins" trap: an LLM will happily write customers.id or
-- orders.customer_id and blow up at the schema-validation gate.
-- Messiness #4: `total_amount` is a DENORMALIZED rollup of line_items. It has
-- deliberately diverged from SUM(qty * unit_price) for every 5th order (a
-- "discount"), so aggregate questions must choose which source of truth to use.
-- -----------------------------------------------------------------------------
CREATE TABLE orders (
    id            SERIAL PRIMARY KEY,
    cust_id       INTEGER      NOT NULL REFERENCES customers(customer_id),
    total_amount  NUMERIC(12,2) NOT NULL,
    order_date    DATE         NOT NULL
);

-- -----------------------------------------------------------------------------
-- Granular pricing data. Aggregating this table directly (instead of the
-- denormalized orders.total_amount) is the second semantic trap.
-- -----------------------------------------------------------------------------
CREATE TABLE line_items (
    id          SERIAL PRIMARY KEY,
    order_id    INTEGER       NOT NULL REFERENCES orders(id),
    qty         INTEGER       NOT NULL,
    unit_price  NUMERIC(12,2) NOT NULL,
    product     TEXT          NOT NULL
);

-- =============================================================================
-- Seed data (deterministic, generated from the series index)
-- =============================================================================

-- 120 customers (>= 100 required). Regions rotate through a fixed list that
-- includes 'New York' (used by the API smoke test) and NULL.
INSERT INTO customers (name, region)
SELECT
    'Customer ' || g,
    (ARRAY['New York', 'California', 'Texas', 'Washington', 'Florida', NULL])[1 + (g % 6)]
FROM generate_series(1, 120) AS g;

-- 600 orders (>= 500 required) spread across a date range so quarter-boundary
-- date filters are meaningful.
INSERT INTO orders (cust_id, total_amount, order_date)
SELECT
    1 + (g % 120),                       -- spread across all 120 customers
    0,                                    -- placeholder, computed below
    DATE '2025-01-01' + (g % 420)         -- ~14 months of history
FROM generate_series(1, 600) AS g;

-- Three line items per order => 600 * 3 = 1800 rows (>= 1500 required).
INSERT INTO line_items (order_id, qty, unit_price, product)
SELECT
    o.id,
    1 + ((o.id * 3 + k) % 5)                                             AS qty,
    ROUND((5 + ((o.id * 13 + k * 7) % 90) + ((o.id % 100) / 100.0))::numeric, 2) AS unit_price,
    (ARRAY['Widget', 'Gadget', 'Gizmo', 'Doohickey'])[1 + ((o.id + k) % 4)] AS product
FROM orders o
CROSS JOIN generate_series(0, 2) AS k;

-- Denormalized rollup: total_amount = SUM(qty * unit_price) ...
UPDATE orders o
SET total_amount = li.total
FROM (
    SELECT order_id, ROUND(SUM(qty * unit_price)::numeric, 2) AS total
    FROM line_items
    GROUP BY order_id
) li
WHERE li.order_id = o.id;

-- ... EXCEPT every 5th order carries a 10% discount, so the denormalized value
-- intentionally diverges from the granular aggregation. A "total revenue"
-- query that sums line_items will over-report relative to orders.total_amount.
UPDATE orders
SET total_amount = ROUND(total_amount * 0.90, 2)
WHERE id % 5 = 0;

-- Helpful indexes (optional, do not affect correctness).
CREATE INDEX idx_orders_cust ON orders(cust_id);
CREATE INDEX idx_line_items_order ON line_items(order_id);

-- -----------------------------------------------------------------------------
-- Self-check: fail loudly at init time if the data volume is too small for the
-- semantic heuristics (Cartesian products) to trigger.
-- -----------------------------------------------------------------------------
DO $$
DECLARE
    n_customers INTEGER;
    n_orders    INTEGER;
    n_items     INTEGER;
BEGIN
    SELECT COUNT(*) INTO n_customers FROM customers;
    SELECT COUNT(*) INTO n_orders    FROM orders;
    SELECT COUNT(*) INTO n_items     FROM line_items;

    IF n_customers < 100 OR n_orders < 500 OR n_items < 1500 THEN
        RAISE EXCEPTION 'Seed volume too small: customers=%, orders=%, line_items=%',
            n_customers, n_orders, n_items;
    END IF;

    RAISE NOTICE 'Seed OK: customers=%, orders=%, line_items=%',
        n_customers, n_orders, n_items;
END $$;

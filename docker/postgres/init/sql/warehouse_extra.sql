-- Five more demo datasets behind the demo Metabase. Same approach as warehouse.sql:
-- views generated relative to current_date with the deterministic demo_rand(),
-- so they always have recent data and re-downloads are reproducible.

CREATE SCHEMA support;
CREATE SCHEMA marketing;
CREATE SCHEMA ops;
CREATE SCHEMA web;
CREATE SCHEMA billing;

-- ---------------------------------------------------------------------------
-- Support: daily ticket volume per channel and priority
-- ---------------------------------------------------------------------------
CREATE VIEW support.ticket_daily AS
SELECT
    d::date                                                         AS ticket_date,
    c.channel,
    p.priority,
    p.team,
    t.opened                                                        AS tickets_opened,
    floor(t.opened * (0.7 + 0.3 * demo_rand(t.k || 'r')))::int      AS tickets_resolved,
    round((p.base_minutes * (0.6 + 0.9 * demo_rand(t.k || 'f')))::numeric, 1) AS median_first_response_min,
    -- No survey answers on some days: CSAT is genuinely unknown (NULL), not 0.
    CASE WHEN demo_rand(t.k || 'n') < 0.12 THEN NULL
         ELSE round((3.2 + 1.8 * demo_rand(t.k || 'c'))::numeric, 2) END AS csat_score,
    floor(t.opened * p.breach_rate * demo_rand(t.k || 'b'))::int    AS sla_breaches
FROM generate_series(current_date - 90, current_date - 1, interval '1 day') AS d
CROSS JOIN (VALUES ('Email'), ('Chat'), ('Phone')) AS c(channel)
CROSS JOIN (VALUES
    ('Low',    'Tier 1', 480, 0.02),
    ('Normal', 'Tier 1', 240, 0.05),
    ('High',   'Tier 2',  90, 0.10),
    ('Urgent', 'Tier 3',  20, 0.18)
) AS p(priority, team, base_minutes, breach_rate)
CROSS JOIN LATERAL (
    SELECT d::date || c.channel || p.priority AS k,
           2 + floor(demo_rand(d::date || c.channel || p.priority) * 40)::int AS opened
) AS t;

-- ---------------------------------------------------------------------------
-- Marketing: campaign performance per day
-- ---------------------------------------------------------------------------
CREATE TABLE marketing.campaigns (
    campaign_id text PRIMARY KEY,
    campaign    text NOT NULL,
    channel     text NOT NULL,
    daily_budget numeric(10,2) NOT NULL
);
INSERT INTO marketing.campaigns VALUES
    ('CMP-01', 'Autumn Desk Setup',     'Paid Search', 900),
    ('CMP-02', 'Ultrawide Launch',      'Paid Social', 1400),
    ('CMP-03', 'Remote Work Bundle',    'Display',     600),
    ('CMP-04', 'Audio Week',            'Paid Social', 750),
    ('CMP-05', 'Brand - Always On',     'Paid Search', 1200),
    ('CMP-06', 'Newsletter Promotions', 'Email',       150);

CREATE VIEW marketing.campaign_daily AS
SELECT
    d::date                                                               AS report_date,
    c.channel,
    c.campaign_id,
    c.campaign,
    m.impressions,
    m.clicks,
    greatest(1, floor(m.clicks * (0.01 + 0.05 * demo_rand(m.k || 'cv'))))::int AS conversions,
    round((c.daily_budget * (0.75 + 0.35 * demo_rand(m.k || 's')))::numeric, 2) AS spend,
    round((c.daily_budget * (0.8 + 3.4 * demo_rand(m.k || 'rv')))::numeric, 2)  AS revenue
FROM generate_series(current_date - 90, current_date - 1, interval '1 day') AS d
CROSS JOIN marketing.campaigns AS c
CROSS JOIN LATERAL (
    SELECT d::date || c.campaign_id AS k,
           (20000 + floor(demo_rand(d::date || c.campaign_id || 'i') * 180000))::bigint AS impressions,
           (200 + floor(demo_rand(d::date || c.campaign_id || 'c') * 4800))::bigint     AS clicks
) AS m;

-- ---------------------------------------------------------------------------
-- Operations: today's inventory per warehouse and SKU (a snapshot)
-- ---------------------------------------------------------------------------
CREATE TABLE ops.suppliers (
    supplier_id    text PRIMARY KEY,
    name           text NOT NULL,
    country        text NOT NULL,
    lead_time_days integer NOT NULL
);
INSERT INTO ops.suppliers VALUES
    ('SUP-1', 'Lumen Works',       'Germany',  12),
    ('SUP-2', 'Pacific Displays',  'Taiwan',   28),
    ('SUP-3', 'Northwind Audio',   'Denmark',  15),
    ('SUP-4', 'Keystone Office',   'USA',       7);

CREATE TABLE ops.skus (
    sku          text PRIMARY KEY,
    product      text NOT NULL,
    supplier_id  text NOT NULL REFERENCES ops.suppliers,
    unit_cost    numeric(10,2) NOT NULL,
    reorder_point integer NOT NULL
);
INSERT INTO ops.skus VALUES
    ('SKU-1001', 'Aurora Desk Lamp',          'SUP-1',  21.40,  40),
    ('SKU-1002', 'Nimbus Standing Desk',      'SUP-4', 212.00,  10),
    ('SKU-1003', 'Pulse Wireless Mouse',      'SUP-4',  11.90,  80),
    ('SKU-1004', 'Pulse Mechanical Keyboard', 'SUP-4',  48.50,  35),
    ('SKU-1005', 'Vista 27" Monitor',         'SUP-2', 158.00,  20),
    ('SKU-1006', 'Vista 34" Ultrawide',       'SUP-2', 301.00,  12),
    ('SKU-1007', 'Echo Noise-Cancel Headset', 'SUP-3',  76.00,  25),
    ('SKU-1008', 'Echo Desk Speakers',        'SUP-3',  39.00,  30);

CREATE VIEW ops.inventory_snapshot AS
SELECT
    current_date                                     AS snapshot_date,
    w.warehouse,
    s.sku,
    s.product,
    -- Two records are corrupt upstream (negative stock) - the pipeline must catch them.
    CASE WHEN (w.warehouse, s.sku) IN (('Rotterdam', 'SKU-1003'), ('Reno', 'SKU-1007')) THEN -5
         ELSE floor(demo_rand(w.warehouse || s.sku || current_date) * 260)::int END AS on_hand,
    floor(demo_rand(w.warehouse || s.sku || 'res') * 20)::int                      AS reserved,
    s.reorder_point,
    s.unit_cost,
    jsonb_build_object(
        'name', sup.name,
        'country', sup.country,
        'lead_time_days', sup.lead_time_days
    )                                                AS supplier
FROM ops.skus AS s
JOIN ops.suppliers AS sup USING (supplier_id)
CROSS JOIN (VALUES ('Reno'), ('Rotterdam'), ('Singapore')) AS w(warehouse);

-- ---------------------------------------------------------------------------
-- Web analytics: hourly traffic per page
-- ---------------------------------------------------------------------------
CREATE VIEW web.traffic_hourly AS
SELECT
    h                                                                  AS hour_start,
    pg.page,
    t.sessions,
    floor(t.sessions * (1.4 + 2.2 * demo_rand(t.k || 'pv')))::int      AS pageviews,
    round((0.25 + 0.5 * demo_rand(t.k || 'b'))::numeric, 3)            AS bounce_rate,
    floor(40 + 260 * demo_rand(t.k || 'd'))::int                       AS avg_session_seconds
FROM generate_series(
        date_trunc('hour', now() AT TIME ZONE 'UTC') - interval '7 days',
        date_trunc('hour', now() AT TIME ZONE 'UTC') - interval '1 hour',
        interval '1 hour') AS h
CROSS JOIN (VALUES ('/'), ('/pricing'), ('/docs'), ('/blog'), ('/signup')) AS pg(page)
CROSS JOIN LATERAL (
    SELECT h::text || pg.page AS k,
           -- a daily curve: busy afternoons, quiet nights
           floor((30 + 400 * (0.5 + 0.5 * sin((extract(hour FROM h) - 8) / 24.0 * 2 * pi())))
                 * (0.7 + 0.6 * demo_rand(h::text || pg.page)))::int AS sessions
) AS t;

-- ---------------------------------------------------------------------------
-- Billing: monthly recurring revenue movements per plan
-- ---------------------------------------------------------------------------
CREATE VIEW billing.mrr_monthly AS
SELECT
    m::date                                                              AS month,
    p.plan,
    round((p.base * 0.08 * (0.6 + 0.8 * demo_rand(m::date || p.plan || 'n')))::numeric, 2) AS new_mrr,
    round((p.base * 0.03 * demo_rand(m::date || p.plan || 'e'))::numeric, 2)               AS expansion_mrr,
    round((p.base * 0.01 * demo_rand(m::date || p.plan || 'c'))::numeric, 2)               AS contraction_mrr,
    round((p.base * 0.04 * demo_rand(m::date || p.plan || 'x'))::numeric, 2)               AS churned_mrr,
    round((p.base * (1 + 0.02 * (12 - (date_part('year', age(date_trunc('month', current_date), m)) * 12
                                     + date_part('month', age(date_trunc('month', current_date), m))))))::numeric, 2)
                                                                                           AS ending_mrr,
    (p.customers + floor(demo_rand(m::date || p.plan || 'cu') * 20))::int                  AS customers
FROM generate_series(date_trunc('month', current_date) - interval '12 months',
                     date_trunc('month', current_date) - interval '1 month',
                     interval '1 month') AS m
CROSS JOIN (VALUES ('Starter', 18000, 140), ('Growth', 64000, 85), ('Enterprise', 210000, 22)) AS p(plan, base, customers);

-- SQLite version of clean.py. Run it with: python sql/run_clean.py
-- It reads raw_sod, raw_merger_events, ref_booking_review and
-- ref_booking_adjustments (loaded by run_clean.py) and builds the same star
-- schema. Decisions referenced (D1...) are in the README.
-- Every CREATE is preceded by DROP TABLE IF EXISTS, so it can be rerun.

-- Step 2: one fixed county -> market map from each county's latest year (D1, D5).
DROP TABLE IF EXISTS dim_county;
CREATE TABLE dim_county AS
WITH ranked AS (
    SELECT CAST(STCNTYBR AS INTEGER)            AS county_fips,
           CNTYNAMB, STALPBR, MSANAMB,
           CAST(YEAR AS INTEGER)                AS yr,
           CAST(COALESCE(MSABR, 0) AS INTEGER)  AS msa,
           ROW_NUMBER() OVER (PARTITION BY STCNTYBR ORDER BY YEAR DESC, UNINUMBR) AS rn
    FROM raw_sod
)
SELECT county_fips,
       CNTYNAMB || ', ' || STALPBR AS county_name,
       STALPBR                     AS state,
       yr                          AS map_year,
       CASE WHEN msa > 0 THEN 'MSA-' || msa ELSE 'CTY-' || county_fips END AS market_id,
       CASE WHEN msa > 0 THEN MSANAMB
            ELSE CNTYNAMB || ', ' || STALPBR || ' (county)' END           AS market_name
FROM ranked
WHERE rn = 1;

DROP TABLE IF EXISTS dim_market;
CREATE TABLE dim_market AS
SELECT c.market_id,
       c.market_name,
       CASE WHEN c.market_id LIKE 'MSA-%' THEN 'MSA' ELSE 'County' END AS market_type,
       (SELECT group_concat(state, ', ')
          FROM (SELECT DISTINCT state FROM dim_county c2
                 WHERE c2.market_id = c.market_id ORDER BY state)) AS states,
       COUNT(*) AS counties
FROM dim_county c
GROUP BY c.market_id, c.market_name;

-- Step 3: every bank -> the bank holding it in 2026, following merger chains (D6).
DROP TABLE IF EXISTS cert_successor;
CREATE TABLE cert_successor AS
WITH RECURSIVE
successor AS (
    SELECT CAST(OUT_CERT AS INTEGER) AS out_cert, CAST(SUR_CERT AS INTEGER) AS sur_cert
    FROM raw_merger_events
),
chain(cert, cur, depth) AS (
    SELECT DISTINCT CAST(CERT AS INTEGER), CAST(CERT AS INTEGER), 0 FROM raw_sod
    UNION ALL
    SELECT chain.cert, s.sur_cert, chain.depth + 1
    FROM chain JOIN successor s ON s.out_cert = chain.cur
    WHERE chain.depth < 50                      -- a loop stops here; run_clean.py checks for it
)
SELECT cert, cur AS final_cert
FROM chain
WHERE NOT EXISTS (SELECT 1 FROM successor s WHERE s.out_cert = chain.cur);

-- Step 4: reported owner = holding company, or the bank itself if none (D2).
DROP TABLE IF EXISTS branch_year;
CREATE TABLE branch_year AS
SELECT CAST(YEAR AS INTEGER)       AS year,
       CAST(UNINUMBR AS INTEGER)   AS branch_id,
       CAST(CERT AS INTEGER)       AS cert,
       CAST(STCNTYBR AS INTEGER)   AS county_fips,
       COALESCE(DEPSUMBR, 0)       AS deposits_000,
       CAST(BKMO AS INTEGER)       AS bkmo,
       BKCLASS                     AS bank_class,
       CASE WHEN COALESCE(RSSDHCR, 0) > 0 THEN 'HC-' || CAST(RSSDHCR AS INTEGER)
            ELSE 'CERT-' || CAST(CERT AS INTEGER) END AS reported_owner_id,
       CASE WHEN COALESCE(RSSDHCR, 0) > 0 THEN NAMEHCR ELSE NAMEFULL END AS owner_name
FROM raw_sod;

-- Step 4: restated owner = the 2026 owner (D2, D6).
DROP TABLE IF EXISTS branch_restated;
CREATE TABLE branch_restated AS
WITH last_row AS (      -- each branch's latest year
    SELECT * FROM (
        SELECT b.*, ROW_NUMBER() OVER (PARTITION BY branch_id ORDER BY year DESC) AS rn
        FROM branch_year b) WHERE rn = 1
),
cert_latest_owner AS (  -- latest owner seen for each bank
    SELECT cert, reported_owner_id AS owner FROM (
        SELECT cert, reported_owner_id,
               ROW_NUMBER() OVER (PARTITION BY cert ORDER BY year DESC, branch_id) AS rn
        FROM branch_year) WHERE rn = 1
),
certs_2026 AS (SELECT DISTINCT cert FROM branch_year WHERE year = 2026)
SELECT l.branch_id,
       CASE WHEN l.year = 2026 THEN l.reported_owner_id
            WHEN cs.final_cert <> l.cert AND co.owner IS NOT NULL THEN co.owner
            ELSE l.reported_owner_id END AS restated_owner_id,
       CASE WHEN l.year = 2026 THEN 'open in 2026'
            WHEN cs.final_cert <> l.cert AND co.owner IS NOT NULL THEN 'merger chain'
            WHEN l.cert IN (SELECT cert FROM certs_2026) THEN 'closed, bank still operating'
            ELSE 'closed, no successor in data' END AS restate_method
FROM last_row l
JOIN cert_successor cs        ON cs.cert = l.cert
LEFT JOIN cert_latest_owner co ON co.cert = cs.final_cert;

-- Step 5: owners, named by their latest year.
DROP TABLE IF EXISTS dim_owner;
CREATE TABLE dim_owner AS
SELECT owner_id, owner_name,
       CASE WHEN owner_id LIKE 'HC-%' THEN 'Holding company'
            ELSE 'Bank (no holding company)' END AS owner_type,
       owner_id = 'HC-1071276'                     AS is_focus    -- First Financial Bancorp
FROM (
    SELECT reported_owner_id AS owner_id, owner_name,
           ROW_NUMBER() OVER (PARTITION BY reported_owner_id ORDER BY year DESC, branch_id) AS rn
    FROM branch_year)
WHERE rn = 1;

-- Step 6: the fact table before booking adjustments.
DROP TABLE IF EXISTS fact_base;
CREATE TABLE fact_base AS
SELECT b.year,
       b.branch_id,
       b.cert,
       b.county_fips,
       c.market_id,
       b.deposits_000,
       b.bkmo = 1                             AS is_main_office,
       b.bank_class,
       b.bank_class IN ('SB', 'SL', 'SA', 'SI') AS is_savings_institution,  -- O11
       b.reported_owner_id,
       r.restated_owner_id,
       r.restate_method
FROM branch_year b
LEFT JOIN dim_county c      ON c.county_fips = b.county_fips
LEFT JOIN branch_restated r ON r.branch_id = b.branch_id
ORDER BY b.year, b.branch_id;

-- Step 7: BPI cap = 3x the market-year 99th percentile (D9). SQLite has no
-- percentile function, so this rebuilds pandas' linear interpolation: the
-- value at position 0.99 * (n - 1) of the sorted list, between its neighbours.
DROP TABLE IF EXISTS market_year_cap;
CREATE TABLE market_year_cap AS
WITH ranked AS (
    SELECT market_id, year, deposits_000 AS d,
           ROW_NUMBER() OVER (PARTITION BY market_id, year ORDER BY deposits_000) - 1 AS idx,
           COUNT(*)     OVER (PARTITION BY market_id, year)                          AS n
    FROM fact_base
),
pos AS (
    SELECT DISTINCT market_id, year, n,
           (n - 1) * 0.99                                    AS vi,
           CAST((n - 1) * 0.99 AS INTEGER)                   AS lo,
           MIN(CAST((n - 1) * 0.99 AS INTEGER) + 1, n - 1)   AS hi
    FROM ranked
),
ends AS (
    SELECT p.market_id, p.year, p.vi - p.lo AS t, a.d AS a, b.d AS b
    FROM pos p
    JOIN ranked a ON a.market_id = p.market_id AND a.year = p.year AND a.idx = p.lo
    JOIN ranked b ON b.market_id = p.market_id AND b.year = p.year AND b.idx = p.hi
)
SELECT market_id, year,
       ROUND(3 * CASE WHEN t >= 0.5 THEN b - (b - a) * (1 - t)   -- same path as numpy
                      ELSE a + (b - a) * t END, 3) AS cap           -- 3 decimals, as in clean.py
FROM ends;

-- The cap applies only in markets where it binds in 9 or more of the 11 years.
DROP TABLE IF EXISTS capped_market;
CREATE TABLE capped_market AS
SELECT f.market_id
FROM fact_base f
JOIN market_year_cap c ON c.market_id = f.market_id AND c.year = f.year
WHERE f.deposits_000 > c.cap
GROUP BY f.market_id
HAVING COUNT(DISTINCT f.year) >= 9;

-- Steps 8-9: review decisions (D9) and manual booking caps (D10, O17, O19).
-- An excluded branch is removed in every year; a capped row keeps at most
-- max_deposits_000.
DROP TABLE IF EXISTS fact_branch_year;
CREATE TABLE fact_branch_year AS
WITH capped AS (
    SELECT f.*,
           CASE WHEN m.market_id IS NOT NULL AND f.deposits_000 > c.cap THEN c.cap
                ELSE f.deposits_000 END AS deposits_capped_000
    FROM fact_base f
    JOIN market_year_cap c     ON c.market_id = f.market_id AND c.year = f.year
    LEFT JOIN capped_market m  ON m.market_id = f.market_id
)
SELECT f.*,
       CASE WHEN a.branch_id IS NOT NULL THEN 'manual booking cap'
            WHEN r.decision = 'exclude'  THEN 'excluded'
            WHEN r.decision = 'keep'     THEN 'kept'
            WHEN r.decision IS NOT NULL  THEN 'not reviewed'
            ELSE 'not flagged' END AS booking_status,
       CASE WHEN r.decision = 'exclude'  THEN 0
            WHEN a.branch_id IS NOT NULL THEN MIN(f.deposits_capped_000, a.max_deposits_000)
            ELSE f.deposits_capped_000 END AS deposits_adjusted_000
FROM capped f
LEFT JOIN ref_booking_review r      ON r.branch_id = f.branch_id
LEFT JOIN ref_booking_adjustments a ON a.branch_id = f.branch_id AND a.year = f.year
ORDER BY f.year, f.branch_id;

DROP TABLE IF EXISTS dim_year;
CREATE TABLE dim_year AS SELECT DISTINCT year FROM fact_branch_year ORDER BY year;

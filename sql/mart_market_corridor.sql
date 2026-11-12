CREATE SCHEMA IF NOT EXISTS mart;

DROP TABLE IF EXISTS mart.mart_market_corridor;

CREATE TABLE mart.mart_market_corridor AS
WITH base_deals AS (
    SELECT
        pd.price_date,
        TO_CHAR(pd.price_date, 'YYYY-MM') AS loading_month,
        oc.gc_id AS loading_city_gcid,
        oc.full_name_local AS loading_city_name,
        uc.gc_id AS unloading_city_gcid,
        uc.full_name_local AS unloading_city_name,
        p.distance,
        p.price_rub,
        p.weight_category,
        COALESCE(bt.std_id, p.body_type) AS body_std_id,
        CASE
            WHEN bt.std_id IN (56, 57, 58, 59, 60, 61) THEN 'Тент'
            WHEN bt.std_id IN (4, 46, 47, 48) THEN 'Рефрижератор'
            WHEN bt.std_id = 2 THEN 'Изотерм'
            WHEN bt.std_id IN (1, 15) THEN 'Борт'
            ELSE 'Прочие / Не указан'
        END AS body_group,
        p.id AS price_id
    FROM core.cleaned_prices p
    JOIN price_dates pd ON p.id = pd.price_id
    JOIN (
        SELECT DISTINCT ON (gc_id) id, gc_id, full_name_local
        FROM cities
        ORDER BY gc_id, id ASC
    ) oc ON p.loading_city_gcid = oc.gc_id
    JOIN (
        SELECT DISTINCT ON (gc_id) id, gc_id, full_name_local
        FROM cities
        ORDER BY gc_id, id ASC
    ) uc ON p.unloading_city_gcid = uc.gc_id
    LEFT JOIN price_body_types pbt ON p.id = pbt.price_id
    LEFT JOIN body_types bt ON pbt.body_type_id = bt.id
    WHERE p.is_fact = true
      AND p.complex = false
      AND p.outlier_mark = false
      AND p.price_rub > 0
      AND p.distance > 0
      AND pd.price_date <= '2026-10-26'
),
aggregated_metrics AS (
    SELECT
        loading_month,
        loading_city_name,
        unloading_city_name,
        body_group,
        weight_category,
        PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY price_rub) AS p25_trip,
        PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY price_rub) AS median_trip,
        PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY price_rub) AS p75_trip,
        AVG(price_rub) AS avg_trip,
        PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY price_rub / distance) AS p25_km,
        PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY price_rub / distance) AS median_km,
        PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY price_rub / distance) AS p75_km,
        COUNT(DISTINCT price_id) AS trips_count,
        COUNT(DISTINCT loading_city_gcid) AS carriers_count, -- Условный срез уникальности направлений/участников
        STDDEV(price_rub) / NULLIF(AVG(price_rub), 0) AS cv_rate
    FROM base_deals
    GROUP BY loading_month, loading_city_name, unloading_city_name, body_group, weight_category
),
with_window AS (
    SELECT
        m.*,
        LAG(median_trip, 1) OVER (PARTITION BY loading_city_name, unloading_city_name, body_group, weight_category ORDER BY loading_month) AS prev_month_median,
        LAG(median_trip, 12) OVER (PARTITION BY loading_city_name, unloading_city_name, body_group, weight_category ORDER BY loading_month) AS prev_year_median
    FROM aggregated_metrics m
)
SELECT
    w.*,
	ROUND(CAST(((median_trip - prev_month_median) / NULLIF(prev_month_median, 0)) * 100 AS numeric), 2) AS mom_pct,
    ROUND(CAST(((median_trip - prev_year_median) / NULLIF(prev_year_median, 0)) * 100 AS numeric), 2) AS yoy_pct,
    CASE
        WHEN trips_count >= 30 THEN 'HIGH'
        WHEN trips_count >= 10 THEN 'MEDIUM'
        ELSE 'LOW_DATA'
    END AS reliability_level
FROM with_window w;



SELECT
    loading_month,
    COUNT(*) AS corridors_count,
    SUM(trips_count) AS total_trips_in_mart
FROM mart.mart_market_corridor
GROUP BY loading_month
ORDER BY loading_month;
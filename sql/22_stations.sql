-- 22_stations.sql
-- station linking (the same physical site reported by naps, openaq and the aqhi feed becomes one site_id)
-- and matching every site to its nearest eccc climate station and open-meteo grid point.
-- distances use geography::Point(...).STDistance, which returns metres on the wgs84 ellipsoid.

-- name key: lowercase letters only, repeated letters collapsed. "Misssissauga" and "MISSISSAUGA" both become
-- "misisauga", "Toronto West" becomes "torontowest".
CREATE OR ALTER FUNCTION core.fn_name_key (@name NVARCHAR(200))
RETURNS VARCHAR(200)
WITH SCHEMABINDING
AS
BEGIN
    DECLARE @s VARCHAR(200) = LOWER(CAST(ISNULL(@name, '') AS VARCHAR(200)));
    DECLARE @out VARCHAR(200) = '', @i INT = 1, @c CHAR(1), @prev CHAR(1) = '';
    WHILE @i <= LEN(@s)
    BEGIN
        SET @c = SUBSTRING(@s, @i, 1);
        IF @c LIKE '[a-z]' AND @c <> @prev
            SET @out = @out + @c;
        IF @c LIKE '[a-z]'
            SET @prev = @c;
        SET @i += 1;
    END;
    RETURN @out;
END;
GO

-- naps names look like "TORONTO WEST - 125 RESOURCES RD"; the part before " - " is the site name
CREATE OR ALTER FUNCTION core.fn_site_name (@name NVARCHAR(200))
RETURNS NVARCHAR(200)
WITH SCHEMABINDING
AS
BEGIN
    RETURN IIF(CHARINDEX(' - ', @name) > 0, LEFT(@name, CHARINDEX(' - ', @name) - 1), @name);
END;
GO

CREATE OR ALTER PROCEDURE core.usp_link_stations
    @run_id INT,
    @max_link_m FLOAT = 250,          -- naps <-> openaq monitor: same site
    @max_aqhi_link_m FLOAT = 10000    -- aqhi locations are community points, a few km from the monitor
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @started DATETIME2(3) = SYSUTCDATETIME();

    -- rebuilt from scratch every run: it's a few hundred rows and rules may change
    CREATE TABLE #link (station_id INT PRIMARY KEY, site_id INT, linked_to INT NULL, method VARCHAR(30),
                        distance_m FLOAT NULL);

    -- 1. every naps station heads its own site
    INSERT INTO #link (station_id, site_id, linked_to, method, distance_m)
    SELECT station_id, station_id, NULL, 'self', NULL FROM core.station WHERE source = 'naps';

    -- 2. openaq government monitors join the nearest naps station within 250 m with the same site name
    WITH cand AS (
        SELECT o.station_id, n.station_id AS naps_id,
               geography::Point(o.lat, o.lon, 4326).STDistance(geography::Point(n.lat, n.lon, 4326)) AS dist_m
        FROM core.station AS o
        JOIN core.station AS n ON n.source = 'naps'
        WHERE o.source = 'openaq' AND o.is_monitor = 1 AND o.lat IS NOT NULL AND n.lat IS NOT NULL
          AND (core.fn_name_key(o.name) = core.fn_name_key(core.fn_site_name(n.name))
               OR core.fn_name_key(o.name) = core.fn_name_key(n.city))
    ),
    best AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY station_id ORDER BY dist_m) AS rn FROM cand WHERE dist_m <= @max_link_m
    )
    INSERT INTO #link (station_id, site_id, linked_to, method, distance_m)
    SELECT station_id, naps_id, naps_id, 'distance_name', dist_m FROM best WHERE rn = 1;

    -- 3. remaining openaq monitors: openaq re-registered some sites, so same-name monitors within 250 m share a site,
    --    headed by the one that reported most recently
    WITH rest AS (
        SELECT s.* FROM core.station AS s
        WHERE s.source = 'openaq' AND s.is_monitor = 1 AND NOT EXISTS (SELECT 1 FROM #link AS l WHERE l.station_id = s.station_id)
    ),
    grp AS (
        SELECT a.station_id,
               (SELECT TOP (1) b.station_id FROM rest AS b
                WHERE core.fn_name_key(b.name) = core.fn_name_key(a.name)
                  AND geography::Point(a.lat, a.lon, 4326).STDistance(geography::Point(b.lat, b.lon, 4326)) <= @max_link_m
                ORDER BY b.last_utc DESC, b.station_id) AS head_id
        FROM rest AS a
    )
    INSERT INTO #link (station_id, site_id, linked_to, method, distance_m)
    SELECT g.station_id, ISNULL(g.head_id, g.station_id), NULLIF(g.head_id, g.station_id),
           IIF(g.head_id = g.station_id OR g.head_id IS NULL, 'self', 'openaq_duplicate'), NULL
    FROM grp AS g;

    -- 4. low-cost sensors are always their own site (different instrument class, never merged with a monitor)
    INSERT INTO #link (station_id, site_id, linked_to, method, distance_m)
    SELECT station_id, station_id, NULL, 'self', NULL
    FROM core.station WHERE source = 'openaq' AND is_monitor = 0;

    -- 5. aqhi locations join the site of the same-named monitor within 10 km, otherwise stand alone.
    --    monitors still reporting win over stale registrations of the same site, then the nearest one.
    WITH cand AS (
        SELECT a.station_id, l.site_id,
               geography::Point(a.lat, a.lon, 4326).STDistance(geography::Point(m.lat, m.lon, 4326)) AS dist_m,
               IIF(m.last_utc IS NULL OR m.last_utc >= DATEADD(DAY, -30, SYSUTCDATETIME()), 0, 1) AS stale
        FROM core.station AS a
        JOIN core.station AS m ON m.source IN ('naps', 'openaq') AND m.is_monitor = 1 AND m.lat IS NOT NULL
        JOIN #link AS l ON l.station_id = m.station_id
        WHERE a.source = 'aqhi' AND a.lat IS NOT NULL
          AND (core.fn_name_key(a.name) = core.fn_name_key(m.name)
               OR core.fn_name_key(a.name) = core.fn_name_key(core.fn_site_name(m.name)))
    ),
    best AS (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY station_id ORDER BY stale, dist_m) AS rn
        FROM cand WHERE dist_m <= @max_aqhi_link_m
    )
    INSERT INTO #link (station_id, site_id, linked_to, method, distance_m)
    SELECT a.station_id, ISNULL(b.site_id, a.station_id), b.site_id, IIF(b.site_id IS NULL, 'self', 'aqhi_name'), b.dist_m
    FROM core.station AS a
    LEFT JOIN best AS b ON b.station_id = a.station_id AND b.rn = 1
    WHERE a.source = 'aqhi';

    MERGE core.station_xref AS t
    USING #link AS s ON t.station_id = s.station_id
    WHEN MATCHED AND (t.site_id <> s.site_id OR ISNULL(t.linked_to, -1) <> ISNULL(s.linked_to, -1) OR t.link_method <> s.method) THEN
        UPDATE SET site_id = s.site_id, linked_to = s.linked_to, link_method = s.method, distance_m = s.distance_m,
                   linked_run = @run_id
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (station_id, site_id, linked_to, link_method, distance_m, linked_run)
        VALUES (s.station_id, s.site_id, s.linked_to, s.method, s.distance_m, @run_id)
    WHEN NOT MATCHED BY SOURCE THEN DELETE;

    INSERT INTO etl.stage_run (run_id, stage, status, started_at, ended_at, seconds, rows_written, message)
    SELECT @run_id, 'link', 'succeeded', @started, SYSUTCDATETIME(),
           DATEDIFF_BIG(MILLISECOND, @started, SYSUTCDATETIME()) / 1000.0, COUNT(*),
           CONCAT(SUM(IIF(method <> 'self', 1, 0)), ' stations linked into ', COUNT(DISTINCT site_id), ' sites')
    FROM #link;
END;
GO

-- a site = the station that heads it in core.station_xref
CREATE OR ALTER VIEW core.v_site
AS
SELECT s.station_id AS site_id, s.station_key, s.source, s.name, s.city, s.lat, s.lon, s.elevation_m, s.site_type,
       s.urbanization, s.provider, s.is_low_cost_sensor, s.last_utc
FROM core.station AS s
JOIN core.station_xref AS x ON x.station_id = s.station_id AND x.site_id = s.station_id;
GO

CREATE OR ALTER PROCEDURE core.usp_match_stations
    @run_id INT, @max_match_km FLOAT = 25
AS
BEGIN
    SET NOCOUNT ON;
    DECLARE @started DATETIME2(3) = SYSUTCDATETIME();

    WITH sites AS (
        SELECT site_id, geography::Point(lat, lon, 4326) AS g FROM core.v_site WHERE lat IS NOT NULL
    ),
    matched AS (
        SELECT s.site_id, c.climate_id, c.dist_m AS climate_m, p.point_id, p.dist_m AS point_m
        FROM sites AS s
        OUTER APPLY (
            SELECT TOP (1) cs.climate_id, s.g.STDistance(geography::Point(cs.lat, cs.lon, 4326)) AS dist_m
            FROM core.climate_station AS cs
            WHERE cs.lat IS NOT NULL
            ORDER BY s.g.STDistance(geography::Point(cs.lat, cs.lon, 4326))
        ) AS c
        OUTER APPLY (
            SELECT TOP (1) wp.point_id, s.g.STDistance(geography::Point(wp.lat, wp.lon, 4326)) AS dist_m
            FROM core.weather_point AS wp
            ORDER BY s.g.STDistance(geography::Point(wp.lat, wp.lon, 4326))
        ) AS p
    )
    MERGE core.station_weather_match AS t
    USING matched AS s ON t.station_id = s.site_id
    WHEN MATCHED THEN
        UPDATE SET climate_id = s.climate_id, climate_distance_km = s.climate_m / 1000.0,
                   is_used = IIF(s.climate_m <= @max_match_km * 1000, 1, 0),
                   point_id = s.point_id, point_distance_km = s.point_m / 1000.0, matched_run = @run_id
    WHEN NOT MATCHED BY TARGET THEN
        INSERT (station_id, climate_id, climate_distance_km, is_used, point_id, point_distance_km, matched_run)
        VALUES (s.site_id, s.climate_id, s.climate_m / 1000.0, IIF(s.climate_m <= @max_match_km * 1000, 1, 0),
                s.point_id, s.point_m / 1000.0, @run_id)
    WHEN NOT MATCHED BY SOURCE THEN DELETE;

    INSERT INTO etl.stage_run (run_id, stage, status, started_at, ended_at, seconds, rows_written, message)
    SELECT @run_id, 'match', 'succeeded', @started, SYSUTCDATETIME(),
           DATEDIFF_BIG(MILLISECOND, @started, SYSUTCDATETIME()) / 1000.0, COUNT(*),
           CONCAT(SUM(CAST(is_used AS INT)), ' of ', COUNT(*), ' sites within ', @max_match_km, ' km of a climate station')
    FROM core.station_weather_match;
END;
GO

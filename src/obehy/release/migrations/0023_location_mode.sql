-- The modes of the lines with a passenger call at each location (ddl.DERIVED_TABLES), but a
-- station's replacement buses (NAD, CZPTT's only buses) where it has trains: stop search shows
-- them, and a station is paired only with a stop that is served. Loads derive it
-- on each partition before ATTACH (load._derive); here it is derived for the loads already
-- loaded, which takes a few minutes on a national release.

CREATE TABLE static.location_mode (
    load_id integer NOT NULL,
    location_id text NOT NULL,
    mode text NOT NULL,
    PRIMARY KEY (load_id, location_id, mode)
) PARTITION BY LIST (load_id);

DO $$
DECLARE
    loaded integer;
BEGIN
    FOR loaded IN
        SELECT l.load_id FROM control.load l
        WHERE l.status = 'loaded'
            AND to_regclass(format('static.trip_call_l%s', l.load_id)) IS NOT NULL
        ORDER BY l.load_id
    LOOP
        EXECUTE format(
            'CREATE TABLE static.location_mode_l%1$s (LIKE static.location_mode '
            'INCLUDING DEFAULTS, CHECK (load_id = %1$s))', loaded);
        EXECUTE format(
            'INSERT INTO static.location_mode_l%1$s (load_id, location_id, mode) '
            'SELECT DISTINCT %1$s, c.location_id, r.mode FROM static.trip_call_l%1$s c '
            'JOIN static.trip_l%1$s t ON t.trip_id = c.trip_id '
            'JOIN static.route_l%1$s r ON r.route_id = t.route_id '
            'WHERE c.passenger_service', loaded);
        EXECUTE format(
            'DELETE FROM static.location_mode_l%1$s b USING static.location_l%1$s l '
            'WHERE b.mode = ''bus'' AND l.location_id = b.location_id '
            'AND l.domain = ''heavy_rail'' AND EXISTS (SELECT 1 FROM static.location_mode_l%1$s r '
            'WHERE r.location_id = b.location_id AND r.mode = ''rail'')', loaded);
        EXECUTE format(
            'ALTER TABLE static.location_mode_l%1$s ADD PRIMARY KEY (load_id, location_id, mode)',
            loaded);
        EXECUTE format(
            'ALTER TABLE static.location_mode ATTACH PARTITION static.location_mode_l%1$s '
            'FOR VALUES IN (%1$s)', loaded);
    END LOOP;
END
$$;

-- The active view, over the published loads (activation rewrites it with every other view).
DO $$
DECLARE
    loads text;
BEGIN
    SELECT string_agg(x.load_id::text, ', ') INTO loads
    FROM control.publication p, unnest(ARRAY[p.jdf_load_id, p.czptt_load_id]) AS x (load_id)
    WHERE x.load_id IS NOT NULL;
    EXECUTE format(
        'CREATE OR REPLACE VIEW active.location_mode AS SELECT location_id, mode '
        'FROM static.location_mode WHERE %s',
        CASE WHEN loads IS NULL THEN 'false' ELSE format('load_id IN (%s)', loads) END);
END
$$;
ANALYZE static.location_mode;

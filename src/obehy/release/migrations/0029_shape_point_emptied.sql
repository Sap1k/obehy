-- A shape's geometry is kept only as static.shape_line: the realtime worker reads its vertices
-- from there, and the loader empties each load's shape_point rows once the line is built. This
-- empties those of the loads already in place (about 2.5 GB nationally).
TRUNCATE static.shape_point;

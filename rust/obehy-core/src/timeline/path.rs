//! A trip's path in metres: shape vertices, distances and where each call lies on it.
//!
//! Without a shape the path runs straight through the call locations. Calls without coordinates
//! get a distance interpolated by schedule time between their neighbours. Projection uses a local
//! equirectangular plane, which is exact enough at the scale of a tolerance of tens of metres.
//!
//! The API is over plain coordinates, not the index's types: [`build_path`] takes each call's
//! location (its `placed_at`), the shape's vertices and distances, and a closure giving a call's
//! schedule time (`Call.time`, an error without one). Shapes are simplified with
//! Douglas-Peucker ([`douglas_peucker`]).

use crate::config::policy::ProgressPolicy;
use crate::geo::{EARTH_RADIUS_M, LonLat, cumulative_m, degrees, radians};

/// Per segment: start (lon, lat) and its (dlon, dlat), precomputed for projection.
pub type Segment = (f64, f64, f64, f64);

#[derive(Debug, Clone, PartialEq)]
pub struct Path {
    /// (lon, lat)
    pub points: Vec<LonLat>,
    pub distances_m: Vec<f64>,
    pub call_distances_m: Vec<f64>,
    pub segments: Vec<Segment>,
    /// A real shape (trusted closely) or straight chords between stops (trusted less).
    pub shaped: bool,
    /// Per run of [`BLOCK`] segments, the box around them: (min lon, max lon, min lat, max lat).
    pub blocks: Vec<(f64, f64, f64, f64)>,
}

/// Segments per box of [`Path::blocks`].
pub const BLOCK: usize = 16;

fn blocks(points: &[LonLat]) -> Vec<(f64, f64, f64, f64)> {
    let segments = points.len().saturating_sub(1);
    (0..segments.div_ceil(BLOCK))
        .map(|b| {
            let run = &points[b * BLOCK..=((b + 1) * BLOCK).min(segments)];
            run.iter().fold(
                (
                    f64::INFINITY,
                    f64::NEG_INFINITY,
                    f64::INFINITY,
                    f64::NEG_INFINITY,
                ),
                |(x0, x1, y0, y1), &(x, y)| (x0.min(x), x1.max(x), y0.min(y), y1.max(y)),
            )
        })
        .collect()
}

impl Path {
    pub fn length_m(&self) -> f64 {
        self.distances_m.last().copied().unwrap_or(0.0)
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Projection {
    pub along_m: f64,
    pub lateral_m: f64,
    pub segment_m: f64,
}

/// Where a fix may be on the path: its projection onto one segment.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Candidate {
    pub along_m: f64,
    pub lateral_m: f64,
    /// Combined uncertainty of the fix and of the geometry of this segment.
    pub sigma_m: f64,
    /// Direction of travel along the segment, clockwise from north.
    pub heading_deg: f64,
}

/// `math.pi / 180 * EARTH_RADIUS_M`, then `rad * cos(radians(lat0))`.
fn plane(lat0: f64) -> (f64, f64) {
    let rad = std::f64::consts::PI / 180.0 * EARTH_RADIUS_M;
    (rad * radians(lat0).cos(), rad)
}

/// A point's projection onto one segment in the plane of scale `kx`, `ky`: (fraction along
/// it, lateral metres, the segment's x and y in metres).
fn onto(segment: Segment, lon: f64, lat: f64, kx: f64, ky: f64) -> (f64, f64, f64, f64) {
    let (t, ex, ey, ux, uy) = offset(segment, lon, lat, kx, ky);
    (t, ex.hypot(ey), ux, uy)
}

/// [`onto`] before the norm: (fraction along, the lateral offset's x and y, the segment's x
/// and y), all in metres but the fraction.
fn offset(segment: Segment, lon: f64, lat: f64, kx: f64, ky: f64) -> (f64, f64, f64, f64, f64) {
    let (ax, ay, dx, dy) = segment;
    let (ux, uy) = (dx * kx, dy * ky);
    let (px, py) = ((lon - ax) * kx, (lat - ay) * ky);
    let length2 = ux * ux + uy * uy;
    let t = if length2 == 0.0 {
        0.0
    } else {
        ((px * ux + py * uy) / length2).clamp(0.0, 1.0)
    };
    (t, px - t * ux, py - t * uy, ux, uy)
}

/// The projection of the point nearest the path: the first of the least lateral distance
/// (`min(project_all(...), key=lateral_m)`), None for a path without segments.
///
/// The least (lateral, segment) pair is the same in any order of visiting the segments, so
/// the run of [`BLOCK`] segments whose box is nearest goes first, and segments that cannot be
/// nearer than the best so far are skipped: a run whose box is farther, and a segment whose
/// plain `sqrt(x² + y²)` is beyond it (its correctly rounded norm is then not computed). Both
/// bounds differ from the norm by rounding only, far below their margin (1e-9 relative plus a
/// micrometre), so a skipped segment is never the one a full scan picks, ties included.
pub fn nearest(path: &Path, lon: f64, lat: f64) -> Option<Projection> {
    if !(lon.is_finite() && lat.is_finite()) {
        return first_nearest(path, lon, lat); // NaN: the full scan's own answer
    }
    let (kx, ky) = plane(lat);
    let (scale_x, scale_y) = (kx.abs(), ky.abs());
    let bound = |&(x0, x1, y0, y1): &(f64, f64, f64, f64)| {
        let dx = (x0 - lon).max(lon - x1).max(0.0) * scale_x;
        let dy = (y0 - lat).max(lat - y1).max(0.0) * scale_y;
        (dx * dx + dy * dy).sqrt()
    };
    let beyond = |d: f64, best: &(Projection, usize)| d > best.0.lateral_m * (1.0 + 1e-9) + 1e-6;
    let mut best: Option<(Projection, usize)> = None;
    let scan = |b: usize, best: &mut Option<(Projection, usize)>| {
        for i in b * BLOCK..((b + 1) * BLOCK).min(path.segments.len()) {
            let (t, ex, ey, _, _) = offset(path.segments[i], lon, lat, kx, ky);
            if let Some(found) = best
                && beyond((ex * ex + ey * ey).sqrt(), found)
            {
                continue;
            }
            let lateral = ex.hypot(ey);
            let better = match best {
                None => true,
                Some((p, at)) => lateral < p.lateral_m || (lateral == p.lateral_m && i < *at),
            };
            if better {
                let (start, end) = (path.distances_m[i], path.distances_m[i + 1]);
                let projection = Projection {
                    along_m: start + t * (end - start),
                    lateral_m: lateral,
                    segment_m: end - start,
                };
                *best = Some((projection, i));
            }
        }
    };
    let mut first: Option<(usize, f64)> = None;
    for (b, block) in path.blocks.iter().enumerate() {
        let d = bound(block);
        if first.is_none_or(|(_, least)| d < least) {
            first = Some((b, d));
        }
    }
    let (first, _) = first?;
    scan(first, &mut best);
    for (b, block) in path.blocks.iter().enumerate() {
        if b == first
            || best
                .as_ref()
                .is_some_and(|found| beyond(bound(block), found))
        {
            continue;
        }
        scan(b, &mut best);
    }
    best.map(|(projection, _)| projection)
}

/// [`nearest`] as `min(project_all(...), key=lateral_m)` scans: every segment, in order.
fn first_nearest(path: &Path, lon: f64, lat: f64) -> Option<Projection> {
    let mut best: Option<Projection> = None;
    for p in project_all(path, lon, lat) {
        if best.is_none_or(|b| p.lateral_m < b.lateral_m) {
            best = Some(p);
        }
    }
    best
}

fn segments(points: &[LonLat]) -> Vec<Segment> {
    points
        .windows(2)
        .map(|w| {
            let ((ax, ay), (bx, by)) = (w[0], w[1]);
            (ax, ay, bx - ax, by - ay)
        })
        .collect()
}

/// The first index whose value is greater than `x`.
pub(crate) fn bisect_right(a: &[f64], x: f64) -> usize {
    a.partition_point(|&v| v <= x)
}

/// The first index whose value is not less than `x`.
fn bisect_left(a: &[f64], x: f64) -> usize {
    a.partition_point(|&v| v < x)
}

/// `v[i]`, a negative `i` counted from the end (a one-point path's segment is -1).
fn at<T: Copy>(v: &[T], i: i64) -> T {
    let j = if i < 0 { i + v.len() as i64 } else { i };
    let j = usize::try_from(j).unwrap_or_else(|_| panic!("index {i} out of range"));
    v[j]
}

/// The segment index `point_at` and `heading_at` read:
/// `min(max(bisect_right(distances, along) - 1, 0), len(distances) - 2)` (may be -1).
fn segment_at(distances: &[f64], along_m: f64) -> i64 {
    let i = (bisect_right(distances, along_m) as i64 - 1).max(0);
    i.min(distances.len() as i64 - 2)
}

/// The point's projection onto every segment of the path.
pub fn project_all(path: &Path, lon: f64, lat: f64) -> Vec<Projection> {
    let (kx, ky) = plane(lat);
    let mut out = Vec::with_capacity(path.segments.len());
    for (i, &segment) in path.segments.iter().enumerate() {
        let (t, lateral, _, _) = onto(segment, lon, lat, kx, ky);
        let (start, end) = (path.distances_m[i], path.distances_m[i + 1]);
        out.push(Projection {
            along_m: start + t * (end - start),
            lateral_m: lateral,
            segment_m: end - start,
        });
    }
    out
}

/// The (lon, lat) point `along_m` metres along the path (clamped to its ends).
pub fn point_at(path: &Path, along_m: f64) -> LonLat {
    let distances = &path.distances_m;
    let i = segment_at(distances, along_m);
    let span = at(distances, i + 1) - at(distances, i);
    let t = if span <= 0.0 {
        0.0
    } else {
        ((along_m - at(distances, i)) / span).clamp(0.0, 1.0)
    };
    let ((ax, ay), (bx, by)) = (at(&path.points, i), at(&path.points, i + 1));
    (ax + t * (bx - ax), ay + t * (by - ay))
}

/// The direction of travel `along_m` metres along the path, clockwise from north.
pub fn heading_at(path: &Path, along_m: f64) -> f64 {
    let i = segment_at(&path.distances_m, along_m);
    let ((ax, ay), (bx, by)) = (at(&path.points, i), at(&path.points, i + 1));
    let (kx, ky) = plane(ay);
    degrees(((bx - ax) * kx).atan2((by - ay) * ky)).rem_euclid(360.0)
}

pub fn make_path(
    points: Vec<LonLat>,
    distances: Vec<f64>,
    call_distances: Vec<f64>,
    shaped: bool,
) -> Path {
    let segments = segments(&points);
    let blocks = blocks(&points);
    Path {
        points,
        distances_m: distances,
        call_distances_m: call_distances,
        segments,
        shaped,
        blocks,
    }
}

/// One candidate per segment within `reach_sigmas` of its own uncertainty (`sigma(path,
/// segment length)`), optionally only segments overlapping the along-path `window`.
pub fn candidates(
    path: &Path,
    lon: f64,
    lat: f64,
    mut sigma: impl FnMut(&Path, f64) -> f64,
    reach_sigmas: f64,
    window: Option<(f64, f64)>,
) -> Vec<Candidate> {
    let (kx, ky) = plane(lat);
    let distances = &path.distances_m;
    let segments = &path.segments;
    let (mut first, mut last) = (0, segments.len());
    if let Some((lo, hi)) = window {
        first = bisect_left(distances, lo).saturating_sub(1);
        last = last.min(bisect_right(distances, hi));
    }
    let mut out = Vec::new();
    for i in first..last {
        let (t, ex, ey, ux, uy) = offset(segments[i], lon, lat, kx, ky);
        let (start, end) = (distances[i], distances[i + 1]);
        let segment = end - start;
        let s = sigma(path, segment);
        let reach = reach_sigmas * s;
        // Clearly out of reach by the plain norm (it differs from the correctly rounded one
        // by rounding only): the norm is not computed.
        if (ex * ex + ey * ey).sqrt() > reach * (1.0 + 1e-9) + 1e-6 {
            continue;
        }
        let lateral = ex.hypot(ey);
        if lateral <= reach {
            let heading = degrees(ux.atan2(uy)).rem_euclid(360.0);
            out.push(Candidate {
                along_m: start + t * segment,
                lateral_m: lateral,
                sigma_m: s,
                heading_deg: heading,
            });
        }
    }
    out
}

/// Where a stop may lie on the path: (along, lateral) of each local minimum of its distance
/// from the shape within `off_m`. A road driven out and back gives one per leg.
fn stop_candidates(
    path_points: &[LonLat],
    distances: &[f64],
    lon: f64,
    lat: f64,
    off_m: f64,
) -> Vec<(f64, f64)> {
    let (kx, ky) = plane(lat);
    let projected: Vec<(f64, f64)> = segments(path_points)
        .into_iter()
        .enumerate()
        .map(|(i, segment)| {
            let (t, lateral, _, _) = onto(segment, lon, lat, kx, ky);
            let (start, end) = (distances[i], distances[i + 1]);
            (start + t * (end - start), lateral)
        })
        .collect();
    let mut out = Vec::new();
    for (i, &(along, lateral)) in projected.iter().enumerate() {
        if lateral > off_m {
            continue;
        }
        if i > 0 && projected[i - 1].1 < lateral {
            continue;
        }
        if i + 1 < projected.len() && projected[i + 1].1 <= lateral {
            continue;
        }
        out.push((along, lateral));
    }
    out
}

/// `(a0, a1) < (b0, b1)` on floats: the first unequal pair decides.
fn tuple_lt(a: (f64, f64), b: (f64, f64)) -> bool {
    if a.0 == b.0 { a.1 < b.1 } else { a.0 < b.0 }
}

type Choice = (f64, f64, Option<(usize, usize)>);

/// Each stop's distance along the path, chosen for all stops at once: in call order, as close
/// to the shape as possible in total . A stop is left unplaced, at
/// the cost `off_m`, when no candidate fits.
fn place_stops(
    path_points: &[LonLat],
    distances: &[f64],
    located: &[Option<LonLat>],
    off_m: f64,
) -> Vec<Option<f64>> {
    let stops: Vec<(usize, Vec<(f64, f64)>)> = located
        .iter()
        .enumerate()
        .filter_map(|(i, point)| {
            point.map(|(lon, lat)| (i, stop_candidates(path_points, distances, lon, lat, off_m)))
        })
        .collect();
    // best[k][c]: (cost, along, previous (k, c)) with stop k placed at its candidate c.
    let mut best: Vec<Vec<Choice>> = Vec::with_capacity(stops.len());
    for (k, (_, found)) in stops.iter().enumerate() {
        let mut row: Vec<Choice> = Vec::with_capacity(found.len());
        for &(along, lateral) in found {
            let mut choice: Choice = (lateral + k as f64 * off_m, along, None);
            for (j, earlier) in best.iter().enumerate() {
                let skipped = (k - j - 1) as f64 * off_m;
                for (c, &(cost, before, _)) in earlier.iter().enumerate() {
                    if before <= along {
                        let total = cost + skipped + lateral;
                        if tuple_lt((total, along), (choice.0, choice.1)) {
                            choice = (total, along, Some((j, c)));
                        }
                    }
                }
            }
            row.push(choice);
        }
        best.push(row);
    }
    let n = stops.len();
    let mut end: (f64, Option<(usize, usize)>) = (n as f64 * off_m, None);
    for (k, row) in best.iter().enumerate() {
        for (c, &(cost, _, _)) in row.iter().enumerate() {
            let total = cost + (n - k - 1) as f64 * off_m;
            if total < end.0 {
                end = (total, Some((k, c)));
            }
        }
    }
    let mut out = vec![None; located.len()];
    let mut cursor = end.1;
    while let Some((k, c)) = cursor {
        out[stops[k].0] = Some(best[k][c].1);
        cursor = best[k][c].2;
    }
    out
}

/// Without a shape the path is the located stops themselves, in order.
fn straight(located: &[Option<LonLat>], distances: &[f64]) -> Vec<Option<f64>> {
    let mut vertices = distances.iter();
    located
        .iter()
        .map(|point| point.map(|_| *vertices.next().expect("a vertex per located stop")))
        .collect()
}

/// Planar distance from `p` to the segment `a`-`b`.
fn point_to_segment(p: LonLat, a: LonLat, b: LonLat) -> f64 {
    let (dx, dy) = (b.0 - a.0, b.1 - a.1);
    let length2 = dx * dx + dy * dy;
    let t = if length2 == 0.0 {
        0.0
    } else {
        (((p.0 - a.0) * dx + (p.1 - a.1) * dy) / length2).clamp(0.0, 1.0)
    };
    (p.0 - (a.0 + t * dx)).hypot(p.1 - (a.1 + t * dy))
}

/// Douglas-Peucker: the indices of the vertices kept (the ends always), dropping every
/// vertex of a section within `tolerance` of the line through its ends.
pub fn douglas_peucker(points: &[LonLat], tolerance: f64) -> Vec<usize> {
    if points.len() <= 2 {
        return (0..points.len()).collect();
    }
    let mut keep = vec![false; points.len()];
    keep[0] = true;
    keep[points.len() - 1] = true;
    let mut sections = vec![(0, points.len() - 1)];
    while let Some((i, j)) = sections.pop() {
        // The first of equally far vertices.
        let farthest = (i + 1..j)
            .map(|k| (k, point_to_segment(points[k], points[i], points[j])))
            .reduce(|best, next| if next.1 > best.1 { next } else { best });
        if let Some((k, distance)) = farthest
            && distance > tolerance
        {
            keep[k] = true;
            sections.push((i, k));
            sections.push((k, j));
        }
    }
    (0..points.len()).filter(|&k| keep[k]).collect()
}

/// Drop vertices within `tolerance_deg` of the line through their neighbours. Kept vertices
/// keep their original along-path distances, so call distances and progress stay on the same
/// scale.
pub fn simplify(
    points: &[LonLat],
    distances: &[f64],
    tolerance_deg: f64,
) -> (Vec<LonLat>, Vec<f64>) {
    let kept = douglas_peucker(points, tolerance_deg);
    (
        kept.iter().map(|&i| points[i]).collect(),
        kept.iter().map(|&i| distances[i]).collect(),
    )
}

/// A trip's path, over plain data:
///
/// - `located`: per call, its `placed_at` location's (lon, lat), None without coordinates;
/// - `shape`: the trip's shape vertices and their distances (`Shape.points`,
///   `Shape.distances_m`), None for a trip without a shape;
/// - `time(i)`: call `i`'s schedule time (`Call.time`); an error (an `IndexMiss`) is
///   returned as is. It is only asked for when the path has two points or more, in call order.
pub fn build_path<E>(
    located: &[Option<LonLat>],
    shape: Option<(&[LonLat], &[f64])>,
    mut time: impl FnMut(usize) -> Result<i64, E>,
    policy: &ProgressPolicy,
) -> Result<Path, E> {
    let shaped_shape = shape.filter(|(points, _)| points.len() >= 2);
    let shaped = shaped_shape.is_some();
    let (points, distances) = match shaped_shape {
        Some((points, distances)) => simplify(points, distances, policy.shape_simplify_deg),
        None => {
            let points: Vec<LonLat> = located.iter().flatten().copied().collect();
            let distances = cumulative_m(&points);
            (points, distances)
        }
    };
    if points.len() < 2 {
        let zeros = vec![0.0; located.len()];
        return Ok(make_path(points, distances, zeros, false));
    }
    let known = if shaped {
        place_stops(&points, &distances, located, policy.stop_off_shape_m)
    } else {
        straight(located, &distances)
    };
    let times = (0..located.len())
        .map(&mut time)
        .collect::<Result<Vec<i64>, E>>()?;
    let length = *distances.last().expect("two points or more");
    let call_distances = fill(&known, &times, length);
    Ok(make_path(points, distances, call_distances, shaped))
}

/// Interpolate missing call distances by schedule time between known neighbours.
fn fill(known: &[Option<f64>], times: &[i64], length: f64) -> Vec<f64> {
    let mut out = Vec::with_capacity(known.len());
    for (i, value) in known.iter().enumerate() {
        if let Some(value) = value {
            out.push(*value);
            continue;
        }
        let before = (0..i).rev().find_map(|j| known[j].map(|d| (times[j], d)));
        let after = (i + 1..known.len()).find_map(|j| known[j].map(|d| (times[j], d)));
        let (lo_t, lo_d) = before.unwrap_or((times[0], 0.0));
        let (hi_t, hi_d) = after.unwrap_or((times[times.len() - 1], length));
        // int / int true division: exact operands below 2^53, so one correctly rounded division.
        let share = if hi_t == lo_t {
            0.0
        } else {
            (times[i] - lo_t) as f64 / (hi_t - lo_t) as f64
        };
        out.push(lo_d + share * (hi_d - lo_d));
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bisect_finds_the_insertion_points() {
        let a = [0.0, 1.0, 1.0, 2.0];
        assert_eq!(bisect_right(&a, 1.0), 3);
        assert_eq!(bisect_left(&a, 1.0), 1);
        assert_eq!(bisect_right(&a, -1.0), 0);
        assert_eq!(bisect_left(&a, 5.0), 4);
    }

    #[test]
    fn one_point_paths_read_their_only_point() {
        let path = make_path(vec![(14.0, 50.0)], vec![0.0], vec![0.0, 0.0], false);
        assert_eq!(point_at(&path, 10.0), (14.0, 50.0));
        assert_eq!(heading_at(&path, 10.0).to_bits(), 0.0_f64.to_bits());
        assert!(project_all(&path, 14.0, 50.0).is_empty());
    }

    #[test]
    fn heading_due_south_west_is_positive() {
        let path = make_path(
            vec![(14.0, 50.0), (14.0, 49.9)],
            vec![0.0, 11_000.0],
            vec![],
            false,
        );
        assert_eq!(heading_at(&path, 1.0), 180.0);
        let west = make_path(
            vec![(14.0, 50.0), (13.9, 50.0)],
            vec![0.0, 7000.0],
            vec![],
            false,
        );
        assert!((heading_at(&west, 1.0) - 270.0).abs() < 1e-9);
    }
}

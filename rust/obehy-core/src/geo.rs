//! Small geometry on WGS84 coordinates (lon, lat in degrees, metres out).

/// Mean Earth radius (IUGG), metres.
pub const EARTH_RADIUS_M: f64 = 6_371_008.8;

/// A (lon, lat) point in degrees.
pub type LonLat = (f64, f64);

pub fn radians(degrees: f64) -> f64 {
    degrees.to_radians()
}

pub fn degrees(radians: f64) -> f64 {
    radians.to_degrees()
}

/// Great-circle distance between two (lon, lat) points (haversine).
pub fn distance_m(a: LonLat, b: LonLat) -> f64 {
    let (lon1, lat1, lon2, lat2) = (radians(a.0), radians(a.1), radians(b.0), radians(b.1));
    let s_lat = ((lat2 - lat1) / 2.0).sin();
    let s_lon = ((lon2 - lon1) / 2.0).sin();
    let h = s_lat * s_lat + lat1.cos() * lat2.cos() * s_lon * s_lon;
    2.0 * EARTH_RADIUS_M * h.sqrt().asin()
}

/// Distance travelled at each vertex of a polyline (summed left to right).
pub fn cumulative_m(points: &[LonLat]) -> Vec<f64> {
    let mut out = Vec::with_capacity(points.len().max(1));
    let mut total = 0.0;
    out.push(0.0);
    for pair in points.windows(2) {
        total += distance_m(pair[0], pair[1]);
        out.push(total);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_kilometre_north_and_the_running_total() {
        // 0.009 degrees of latitude is about 1 km.
        let d = distance_m((14.0, 50.0), (14.0, 50.009));
        assert!((d - 1000.75).abs() < 0.1, "{d}");
        assert_eq!(distance_m((14.0, 50.0), (14.0, 50.0)), 0.0);
        let total = cumulative_m(&[(14.0, 50.0), (14.0, 50.009), (14.0, 50.0)]);
        assert_eq!(total.len(), 3);
        assert_eq!(total[2], d + distance_m((14.0, 50.009), (14.0, 50.0)));
        assert_eq!(cumulative_m(&[]), vec![0.0]);
    }
}

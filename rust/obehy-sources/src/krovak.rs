//! S-JTSK / Krovak East North (EPSG:5514) to WGS 84 longitude and latitude (SZ-Q1), in pure
//! Rust: the inverse Krovak projection on the Bessel ellipsoid, then to geocentric
//! coordinates, the seven-parameter Helmert transformation "S-JTSK to WGS 84 (5)" (EPSG:5239,
//! Czechia, about 1 m), and back to geodetic coordinates on WGS 84.

use std::f64::consts::{FRAC_PI_2, FRAC_PI_4};

const ARCSEC: f64 = std::f64::consts::PI / (180.0 * 3600.0);

/// Latitude of the pseudo standard parallel, 78°30'N.
const S0: f64 = 1.370_083_462_815_55;
/// 59°42'42.69689".
const UQ: f64 = 1.042_168_563_804_74;

#[derive(Debug, Clone, Copy)]
struct Ellipsoid {
    a: f64,
    /// Flattening.
    f: f64,
    /// First eccentricity squared.
    es: f64,
}

impl Ellipsoid {
    const fn new(a: f64, inverse_flattening: f64) -> Ellipsoid {
        let f = 1.0 / inverse_flattening;
        Ellipsoid {
            a,
            f,
            es: 2.0 * f - f * f,
        }
    }

    /// Geodetic (radians, height 0) to geocentric metres.
    fn to_geocentric(self, lon: f64, lat: f64) -> [f64; 3] {
        let n = self.a / (1.0 - self.es * lat.sin().powi(2)).sqrt();
        [
            n * lat.cos() * lon.cos(),
            n * lat.cos() * lon.sin(),
            n * (1.0 - self.es) * lat.sin(),
        ]
    }

    /// Geocentric metres to geodetic longitude and latitude (radians), Bowring's method.
    fn to_geodetic(self, [x, y, z]: [f64; 3]) -> (f64, f64) {
        let b = self.a * (1.0 - self.f);
        let ep2 = (self.a * self.a - b * b) / (b * b);
        let p = x.hypot(y);
        let theta = (z * self.a).atan2(p * b);
        let lat =
            (z + ep2 * b * theta.sin().powi(3)).atan2(p - self.es * self.a * theta.cos().powi(3));
        (y.atan2(x), lat)
    }
}

const BESSEL: Ellipsoid = Ellipsoid::new(6_377_397.155, 299.152_812_8);
const WGS84: Ellipsoid = Ellipsoid::new(6_378_137.0, 298.257_223_563);

/// The EPSG:5514 to EPSG:4326 transformer.
#[derive(Debug, Clone, Copy)]
pub struct Transformer {
    e: f64,
    lon0: f64,
    alpha: f64,
    k: f64,
    n: f64,
    rho0: f64,
}

impl Default for Transformer {
    fn default() -> Self {
        Self::new()
    }
}

impl Transformer {
    pub fn new() -> Transformer {
        let es = BESSEL.es;
        let e = es.sqrt();
        let lat0 = 49.5_f64.to_radians();
        let lon0 = (24.0 + 50.0 / 60.0_f64).to_radians();
        let k0 = 0.9999;
        let alpha = (1.0 + es * lat0.cos().powi(4) / (1.0 - es)).sqrt();
        let u0 = (lat0.sin() / alpha).asin();
        let g = ((1.0 + e * lat0.sin()) / (1.0 - e * lat0.sin())).powf(alpha * e / 2.0);
        let k = (u0 / 2.0 + FRAC_PI_4).tan() / (lat0 / 2.0 + FRAC_PI_4).tan().powf(alpha) * g;
        let n0 = (1.0 - es).sqrt() / (1.0 - es * lat0.sin().powi(2));
        Transformer {
            e,
            lon0,
            alpha,
            k,
            n: S0.sin(),
            rho0: k0 * n0 / S0.tan(),
        }
    }

    /// Easting and northing (metres) to Bessel longitude and latitude (radians).
    fn inverse_krovak(&self, easting: f64, northing: f64) -> (f64, f64) {
        // East-North axes: both negated and swapped into the classic southing/westing.
        let x = -northing / BESSEL.a;
        let y = -easting / BESSEL.a;
        let rho = x.hypot(y);
        let d = y.atan2(x) / S0.sin();
        let s = if rho == 0.0 {
            FRAC_PI_2
        } else {
            2.0 * (((self.rho0 / rho).powf(1.0 / self.n) * (S0 / 2.0 + FRAC_PI_4).tan()).atan()
                - FRAC_PI_4)
        };
        let ad = FRAC_PI_2 - UQ;
        let u = (ad.cos() * s.sin() - ad.sin() * s.cos() * d.cos()).asin();
        let deltav = (s.cos() * d.sin() / u.cos()).asin();
        let lon = self.lon0 - deltav / self.alpha;
        let mut lat = u;
        for _ in 0..100 {
            let next = 2.0
                * ((self.k.powf(-1.0 / self.alpha)
                    * (u / 2.0 + FRAC_PI_4).tan().powf(1.0 / self.alpha)
                    * ((1.0 + self.e * lat.sin()) / (1.0 - self.e * lat.sin()))
                        .powf(self.e / 2.0))
                .atan()
                    - FRAC_PI_4);
            let done = (next - lat).abs() < 1e-15;
            lat = next;
            if done {
                break;
            }
        }
        (lon, lat)
    }

    /// `(lon, lat)` in degrees of an EPSG:5514 easting and northing.
    pub fn transform(&self, easting: f64, northing: f64) -> (f64, f64) {
        let (lon, lat) = self.inverse_krovak(easting, northing);
        let [x, y, z] = BESSEL.to_geocentric(lon, lat);
        // EPSG:5239, coordinate frame rotation (small angles), scale in ppm.
        let (rx, ry, rz) = (-4.9732 * ARCSEC, -1.529 * ARCSEC, -5.2484 * ARCSEC);
        let scale = 1.0 + 3.5378e-6;
        let moved = [
            572.213 + scale * (x + rz * y - ry * z),
            85.334 + scale * (-rz * x + y + rx * z),
            461.94 + scale * (ry * x - rx * y + z),
        ];
        let (lon, lat) = WGS84.to_geodetic(moved);
        (lon.to_degrees(), lat.to_degrees())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn matches_proj_inside_czechia() {
        // PROJ 9.8.1 (pyproj) gave these for operation (5), to 1e-11 degrees.
        let t = Transformer::new();
        for ((x, y), (lon, lat)) in [
            (
                (-742_400.0, -1_043_680.0),
                (14.428_194_966_94, 50.082_024_963_64),
            ),
            (
                (-469_700.0, -1_100_600.0),
                (18.293_942_771_75, 49.844_869_338_29),
            ),
        ] {
            let (got_lon, got_lat) = t.transform(x, y);
            assert!((got_lon - lon).abs() < 1e-10, "{got_lon}");
            assert!((got_lat - lat).abs() < 1e-10, "{got_lat}");
        }
    }
}

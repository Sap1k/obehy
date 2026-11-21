//! S-JTSK / Krovak East North (EPSG:5514) to WGS 84 longitude and latitude (SZ-Q1), as
//! `Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)` does it with PROJ 9.8.1:
//! pure Rust, no PROJ library.
//!
//! PROJ chooses one of four operations per point (decode spec §5): among the operations whose
//! area of use (as a box in EPSG:5514) holds the point, the most accurate, then the smallest
//! area, then the first listed; with none, the first listed. Each is the same pipeline with its
//! own Helmert step:
//!
//! `inv krovak` → `cart` (Bessel) → `helmert` → `inv cart` (WGS 84) → `unitconvert` rad→deg.
//!
//! The steps follow PROJ's `krovak.cpp`, `cart.cpp`, `helmert.cpp` (small-angle rotation
//! matrix) and `ell_set.cpp` operation for operation, so results agree to the last bit or
//! nearly; a fixture of pyproj outputs over a day of SŽ positions checks the rounded values.

use std::f64::consts::{FRAC_PI_2, FRAC_PI_4, PI};

const DEG_TO_RAD: f64 = 0.017_453_292_519_943_295;
const ARCSEC_TO_RAD: f64 = DEG_TO_RAD / 3600.0;

/// Latitude of the pseudo standard parallel, 78°30'N.
const S0: f64 = 1.370_083_462_815_55;
/// 59°42'42.69689".
const UQ: f64 = 1.042_168_563_804_74;
const EPS: f64 = 1e-15;
const MAX_ITER: usize = 100;

/// An ellipsoid's parameters as `ell_set.cpp` derives them from `a` and `rf`.
#[derive(Debug, Clone, Copy)]
struct Ellipsoid {
    a: f64,
    ra: f64,
    f: f64,
    es: f64,
    e2s: f64,
}

impl Ellipsoid {
    fn from_rf(a: f64, rf: f64) -> Ellipsoid {
        let f = 1.0 / rf;
        let es = 2.0 * f - f * f;
        let e = es.sqrt();
        let alpha = e.asin();
        let e2 = alpha.tan();
        Ellipsoid {
            a,
            ra: 1.0 / a,
            f,
            es,
            e2s: e2 * e2,
        }
    }
}

/// The inverse Krovak projection's constants (`krovak_setup`).
#[derive(Debug, Clone, Copy)]
struct Krovak {
    ra: f64,
    e: f64,
    lam0: f64,
    alpha: f64,
    k: f64,
    n: f64,
    rho0: f64,
    ad: f64,
}

impl Krovak {
    fn new() -> Krovak {
        // `ellps=bessel` gives `ra`; the setup then fixes a and es to Bessel's own values.
        let bessel = Ellipsoid::from_rf(6_377_397.155, 299.152_812_8);
        let es: f64 = 0.006_674_372_230_614;
        let e = es.sqrt();
        let phi0 = 0.0 + 49.5 * DEG_TO_RAD;
        let lam0 = 0.0 + 24.833_333_333_333_3 * DEG_TO_RAD;
        let k0 = 0.9999;
        let alpha = (1.0 + (es * phi0.cos().powf(4.0)) / (1.0 - es)).sqrt();
        let u0 = (phi0.sin() / alpha).asin();
        let g = ((1.0 + e * phi0.sin()) / (1.0 - e * phi0.sin())).powf(alpha * e / 2.0);
        let k = (u0 / 2.0 + FRAC_PI_4).tan() / (phi0 / 2.0 + FRAC_PI_4).tan().powf(alpha) * g;
        let n0 = (1.0 - es).sqrt() / (1.0 - es * phi0.sin().powf(2.0));
        Krovak {
            ra: bessel.ra,
            e,
            lam0,
            alpha,
            k,
            n: S0.sin(),
            rho0: k0 * n0 / S0.tan(),
            ad: FRAC_PI_2 - UQ,
        }
    }

    /// Easting and northing (metres) to longitude and latitude (radians).
    fn inverse(&self, easting: f64, northing: f64) -> (f64, f64) {
        // pj_inv's prepare: false origin 0, then scaled by 1/a.
        let x = (easting - 0.0) * self.ra;
        let y = (northing - 0.0) * self.ra;
        // East-North: both signs reversed (the false origin is 0), then swapped.
        let (x, y) = (-y - 0.0, -x - 0.0);
        let rho = (x * x + y * y).sqrt();
        let eps = y.atan2(x);
        let d = eps / S0.sin();
        let s = if rho == 0.0 {
            FRAC_PI_2
        } else {
            2.0 * (((self.rho0 / rho).powf(1.0 / self.n) * (S0 / 2.0 + FRAC_PI_4).tan()).atan()
                - FRAC_PI_4)
        };
        let u = (self.ad.cos() * s.sin() - self.ad.sin() * s.cos() * d.cos()).asin();
        let deltav = (s.cos() * d.sin() / u.cos()).asin();
        let mut lam = self.lam0 - deltav / self.alpha;
        let mut fi1 = u;
        let mut phi = 0.0;
        for _ in 0..MAX_ITER {
            phi = 2.0
                * ((self.k.powf(-1.0 / self.alpha)
                    * (u / 2.0 + FRAC_PI_4).tan().powf(1.0 / self.alpha)
                    * ((1.0 + self.e * fi1.sin()) / (1.0 - self.e * fi1.sin()))
                        .powf(self.e / 2.0))
                .atan()
                    - FRAC_PI_4);
            if (fi1 - phi).abs() < EPS {
                break;
            }
            fi1 = phi;
        }
        lam -= self.lam0;
        // pj_inv's finalize.
        lam = lam + 0.0 + self.lam0;
        (adjlon(lam), phi)
    }
}

fn adjlon(lon: f64) -> f64 {
    if lon.abs() < PI + 1e-12 {
        return lon;
    }
    // Never reached for Czech and Slovak points; PROJ's wrap, simplified.
    let two_pi = 2.0 * PI;
    let mut l = lon + PI;
    l -= two_pi * (l / two_pi).floor();
    l - PI
}

/// Geodetic (radians, height 0) to geocentric (`cart.cpp` `cartesian`).
fn cartesian(ell: &Ellipsoid, lam: f64, phi: f64) -> [f64; 3] {
    let h = 0.0;
    let cosphi = phi.cos();
    let sinphi = phi.sin();
    let n = if ell.es == 0.0 {
        ell.a
    } else {
        ell.a / (1.0 - ell.es * sinphi * sinphi).sqrt()
    };
    [
        (n + h) * cosphi * lam.cos(),
        (n + h) * cosphi * lam.sin(),
        (n * (1.0 - ell.es) + h) * sinphi,
    ]
}

/// Geocentric to geodetic longitude and latitude (`cart.cpp` `geodetic`).
fn geodetic(ell: &Ellipsoid, p: [f64; 3]) -> (f64, f64) {
    let x_div_a = p[0] * ell.ra;
    let y_div_a = p[1] * ell.ra;
    let z_div_a = p[2] * ell.ra;
    let p_div_a = (x_div_a * x_div_a + y_div_a * y_div_a).sqrt();
    let b_div_a = 1.0 - ell.f;
    let p_div_a_b_div_a = p_div_a * b_div_a;
    let norm = (z_div_a * z_div_a + p_div_a_b_div_a * p_div_a_b_div_a).sqrt();
    let (c, s) = if norm != 0.0 {
        let inv_norm = 1.0 / norm;
        (p_div_a_b_div_a * inv_norm, z_div_a * inv_norm)
    } else {
        (1.0, 0.0)
    };
    let y_phi = z_div_a + ell.e2s * b_div_a * s * s * s;
    let x_phi = p_div_a - ell.es * c * c * c;
    let phi = if x_phi <= 0.0 {
        if p[2] >= 0.0 { FRAC_PI_2 } else { -FRAC_PI_2 }
    } else {
        (y_phi / x_phi).atan()
    };
    (y_div_a.atan2(x_div_a), phi)
}

/// A seven-parameter Helmert step (`helmert.cpp`, small-angle matrix), or translation only.
#[derive(Debug, Clone, Copy)]
struct Helmert {
    t: [f64; 3],
    /// None: no rotation and no scale (the translation-only fast path).
    rotation: Option<([[f64; 3]; 3], f64)>,
}

impl Helmert {
    fn translation(x: f64, y: f64, z: f64) -> Helmert {
        Helmert {
            t: [x, y, z],
            rotation: None,
        }
    }

    /// Rotations in arc-seconds, scale in ppm.
    fn seven(t: [f64; 3], rx: f64, ry: f64, rz: f64, s: f64, position_vector: bool) -> Helmert {
        let (f, th, p) = (rx * ARCSEC_TO_RAD, ry * ARCSEC_TO_RAD, rz * ARCSEC_TO_RAD);
        let mut r = [[1.0, p, -th], [-p, 1.0, f], [th, -f, 1.0]];
        if position_vector {
            let m = r;
            for (i, row) in r.iter_mut().enumerate() {
                for (j, cell) in row.iter_mut().enumerate() {
                    *cell = m[j][i];
                }
            }
        }
        Helmert {
            t,
            rotation: Some((r, 1.0 + s * 1e-6)),
        }
    }

    fn forward(&self, p: [f64; 3]) -> [f64; 3] {
        let Some((r, scale)) = self.rotation else {
            return [p[0] + self.t[0], p[1] + self.t[1], p[2] + self.t[2]];
        };
        let (x, y, z) = (p[0] - 0.0, p[1] - 0.0, p[2] - 0.0);
        let mut out = [0.0; 3];
        for (o, row) in out.iter_mut().zip(r) {
            *o = scale * (row[0] * x + row[1] * y + row[2] * z);
        }
        [out[0] + self.t[0], out[1] + self.t[1], out[2] + self.t[2]]
    }
}

/// Which operation PROJ uses at a point.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Operation {
    /// S-JTSK to WGS 84 (4), Slovakia.
    Slovakia,
    /// S-JTSK to WGS 84 (5), Czechia.
    Czechia,
    /// S-JTSK to WGS 84 (3), the rest (Czechia and Slovakia, 6 m).
    Fallback,
}

struct Area {
    x: (f64, f64),
    y: (f64, f64),
}

impl Area {
    fn holds(&self, x: f64, y: f64) -> bool {
        self.x.0 <= x && x <= self.x.1 && self.y.0 <= y && y <= self.y.1
    }
}

/// The areas of use of operations (4) and (5) in EPSG:5514 (found by bisection against
/// pyproj, rust/docs/DECODE_SPEC.md §5).
const SLOVAKIA: Area = Area {
    x: (-598_456.538_238_312_8, -164_288.746_304_334_1),
    y: (-1_353_322.886_203_908_4, -1_116_718.869_400_390_4),
};
const CZECHIA: Area = Area {
    x: (-935_779.701_843_200_4, -418_730.001_284_826_1),
    y: (-1_244_199.854_390_998_1, -911_137.750_900_793_5),
};

pub fn operation(x: f64, y: f64) -> Operation {
    if SLOVAKIA.holds(x, y) {
        Operation::Slovakia
    } else if CZECHIA.holds(x, y) {
        Operation::Czechia
    } else {
        Operation::Fallback
    }
}

/// The EPSG:5514 to EPSG:4326 transformer.
#[derive(Debug, Clone, Copy)]
pub struct Transformer {
    krovak: Krovak,
    bessel: Ellipsoid,
    wgs84: Ellipsoid,
    slovakia: Helmert,
    czechia: Helmert,
    fallback: Helmert,
    xy_factor: f64,
}

impl Default for Transformer {
    fn default() -> Self {
        Self::new()
    }
}

impl Transformer {
    pub fn new() -> Transformer {
        Transformer {
            krovak: Krovak::new(),
            bessel: Ellipsoid::from_rf(6_377_397.155, 299.152_812_8),
            wgs84: Ellipsoid::from_rf(6_378_137.0, 298.257_223_563),
            slovakia: Helmert::seven([485.0, 169.5, 483.8], 7.786, 4.398, 4.103, 0.0, true),
            czechia: Helmert::seven(
                [572.213, 85.334, 461.94],
                -4.9732,
                -1.529,
                -5.2484,
                3.5378,
                false,
            ),
            fallback: Helmert::translation(589.0, 76.0, 480.0),
            xy_factor: 1.0 / DEG_TO_RAD,
        }
    }

    /// `(lon, lat)` in degrees of an EPSG:5514 easting and northing.
    pub fn transform(&self, x: f64, y: f64) -> (f64, f64) {
        let helmert = match operation(x, y) {
            Operation::Slovakia => &self.slovakia,
            Operation::Czechia => &self.czechia,
            Operation::Fallback => &self.fallback,
        };
        let (lam, phi) = self.krovak.inverse(x, y);
        let xyz = helmert.forward(cartesian(&self.bessel, lam, phi));
        let (lam, phi) = geodetic(&self.wgs84, xyz);
        (lam * self.xy_factor, phi * self.xy_factor)
    }
}

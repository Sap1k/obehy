//! Python parity: `float.__repr__`, `json`'s float text, `round(x, n)`, `math.hypot`, float
//! `%` and `//`, and haversine distances, bit for bit against `tools/gen_fixtures.py`.

mod common;

use obehy_core::geo;
use obehy_core::py::json::float_text;
use obehy_core::py::num::{py_ffloordiv, py_float_repr, py_fmod, py_hypot, py_round_digits};

#[test]
fn float_repr_and_json_text_match_python() {
    let text = common::text("pyfloat.txt");
    let mut count = 0;
    for line in text.lines() {
        let mut parts = line.splitn(3, ' ');
        let x = common::bits(parts.next().unwrap());
        let (repr, json) = (parts.next().unwrap(), parts.next().unwrap());
        assert_eq!(py_float_repr(x), repr, "repr of {x:e}");
        assert_eq!(float_text(x), json, "json of {x:e}");
        count += 1;
    }
    assert!(count > 10_000, "{count} floats");
}

fn same(a: f64, b: f64) -> bool {
    a.to_bits() == b.to_bits() || (a.is_nan() && b.is_nan())
}

#[test]
fn rounding_hypot_and_modulo_match_python() {
    let data = common::json("pynum.json");
    for case in data["round"].as_array().unwrap() {
        let (x, n, expected) = (
            common::float(&case[0]),
            common::int(&case[1]),
            common::float(&case[2]),
        );
        assert!(
            same(py_round_digits(x, n as usize), expected),
            "round({x}, {n})"
        );
    }
    for case in data["hypot"].as_array().unwrap() {
        let (a, b, expected) = (
            common::float(&case[0]),
            common::float(&case[1]),
            common::float(&case[2]),
        );
        assert!(same(py_hypot(&[a, b]), expected), "hypot({a}, {b})");
    }
    for case in data["mod"].as_array().unwrap() {
        let (a, b) = (common::float(&case[0]), common::float(&case[1]));
        assert!(same(py_fmod(a, b), common::float(&case[2])), "{a} % {b}");
        assert!(
            same(py_ffloordiv(a, b), common::float(&case[3])),
            "{a} // {b}"
        );
    }
}

#[test]
fn distances_match_python_to_the_bit() {
    let data = common::json("geo.json");
    let mut differ = 0;
    let pairs = data["pairs"].as_array().unwrap();
    for case in pairs {
        let a = (common::float(&case[0]), common::float(&case[1]));
        let b = (common::float(&case[2]), common::float(&case[3]));
        if !same(geo::distance_m(a, b), common::float(&case[4])) {
            differ += 1;
        }
    }
    // The same platform libm on both sides: no difference at all.
    assert_eq!(
        differ,
        0,
        "{differ} of {} distances differ in the last bit",
        pairs.len()
    );
    let line: Vec<(f64, f64)> = data["line"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| (common::float(&p[0]), common::float(&p[1])))
        .collect();
    let expected: Vec<u64> = data["cumulative"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| common::float(v).to_bits())
        .collect();
    let got: Vec<u64> = geo::cumulative_m(&line)
        .iter()
        .map(|v| v.to_bits())
        .collect();
    assert_eq!(got, expected);
}

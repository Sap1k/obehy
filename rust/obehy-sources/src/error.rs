//! A payload that does not decode: Python's exception class and text, as the facts harness
//! records them (`payload_error`).

use std::fmt;

/// A successful poll whose body cannot be decoded. `class` is the Python exception's class
/// name: `PayloadError` (the source's documented shape is missing), `ValueError` (a value that
/// Python lets out of the connector: an ISO time that does not parse, `int()` of a digit that is
/// no decimal), `OverflowError`, `UnicodeDecodeError`, `TypeError`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PayloadError {
    pub class: &'static str,
    pub message: String,
}

impl PayloadError {
    pub fn payload(message: impl Into<String>) -> PayloadError {
        PayloadError {
            class: "PayloadError",
            message: message.into(),
        }
    }

    pub fn value(message: impl Into<String>) -> PayloadError {
        PayloadError {
            class: "ValueError",
            message: message.into(),
        }
    }

    /// `int(text)` refused.
    pub fn int_literal(text: &str) -> PayloadError {
        PayloadError::value(format!(
            "invalid literal for int() with base 10: {}",
            obehy_core::py::text::repr_str(text)
        ))
    }

    /// `float(text)` refused.
    pub fn float_literal(text: &str) -> PayloadError {
        PayloadError::value(format!(
            "could not convert string to float: {}",
            obehy_core::py::text::repr_str(text)
        ))
    }
}

impl fmt::Display for PayloadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.class, self.message)
    }
}

impl std::error::Error for PayloadError {}

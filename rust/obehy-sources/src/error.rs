//! A payload that does not decode.

/// A successful poll whose body cannot be decoded: not the shape the source documents, a body
/// that is not JSON or HTML, or a value that cannot be read.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("{message}")]
pub struct PayloadError {
    pub message: String,
}

impl PayloadError {
    pub fn new(message: impl Into<String>) -> PayloadError {
        PayloadError {
            message: message.into(),
        }
    }
}

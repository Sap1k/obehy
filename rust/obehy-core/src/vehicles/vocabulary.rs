//! The vehicle attribute vocabulary (`src/obehy/data/vehicles/attributes.toml`): the part of
//! `reference/vocabulary.py` the vehicle book uses (identifier schemes and how they compare,
//! source precedence, pairing settings, `touch_s`).

use indexmap::IndexMap;
use serde::Deserialize;

use super::pairing::Settings;

/// The vocabulary file, relative to the Python package's `data/` directory.
pub const VOCABULARY_FILE: &str = "vehicles/attributes.toml";

/// A vocabulary file that does not load.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
#[error("vehicle vocabulary: {0}")]
pub struct VocabularyError(pub String);

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
pub struct Attribute {
    #[serde(rename = "type")]
    pub kind: String,
    pub cs: String,
    pub en: String,
    pub icon: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Vocabulary {
    pub version: String,
    pub touch_s: i64,
    /// In document order.
    pub attributes: IndexMap<String, Attribute>,
    /// Identifier scheme -> how values compare, in the order a report is matched by them.
    pub identifiers: IndexMap<String, String>,
    pub pairing: Settings,
    /// Live statements that clash: the first source listed wins.
    pub live_sources: Vec<String>,
    pub attribute_sources: IndexMap<String, Vec<String>>,
}

#[derive(Deserialize)]
struct Document {
    attributes_version: toml::Value,
    touch_s: i64,
    attributes: IndexMap<String, Attribute>,
    identifiers: IndexMap<String, String>,
    pairing: PairingTable,
    #[serde(default)]
    precedence: Precedence,
}

#[derive(Deserialize)]
struct PairingTable {
    distance_m: f64,
    apart_m: f64,
    max_fix_gap_s: f64,
    min_fixes: i64,
    confirm_s: f64,
    hold_s: f64,
}

#[derive(Deserialize, Default)]
struct Precedence {
    #[serde(default)]
    live_sources: Vec<String>,
    #[serde(default)]
    attributes: IndexMap<String, Vec<String>>,
}

const RULES: [&str; 2] = ["letters_digits", "digits"];

impl Vocabulary {
    /// `load_vocabulary` of a TOML document.
    pub fn from_toml(text: &str) -> Result<Vocabulary, VocabularyError> {
        let doc: Document = toml::from_str(text).map_err(|e| VocabularyError(e.to_string()))?;
        for (scheme, rule) in &doc.identifiers {
            if !RULES.contains(&rule.as_str()) || !doc.attributes.contains_key(scheme) {
                return Err(VocabularyError(format!(
                    "identifier {scheme} = {rule:?} is not known"
                )));
            }
        }
        let version = match doc.attributes_version {
            toml::Value::String(s) => s,
            other => other.to_string(),
        };
        let p = doc.pairing;
        Ok(Vocabulary {
            version,
            touch_s: doc.touch_s,
            attributes: doc.attributes,
            identifiers: doc.identifiers,
            pairing: Settings {
                distance_m: p.distance_m,
                apart_m: p.apart_m,
                max_fix_gap_s: p.max_fix_gap_s,
                min_fixes: p.min_fixes,
                confirm_s: p.confirm_s,
                hold_s: p.hold_s,
            },
            live_sources: doc.precedence.live_sources,
            attribute_sources: doc.precedence.attributes,
        })
    }

    pub fn load(path: &std::path::Path) -> Result<Vocabulary, VocabularyError> {
        let text = std::fs::read_to_string(path)
            .map_err(|e| VocabularyError(format!("{}: {e}", path.display())))?;
        Vocabulary::from_toml(&text)
    }

    /// An identifier as compared (a plate: letters and digits, upper case). Panics on a scheme
    /// that is not one (Python raises).
    pub fn normalise(&self, scheme: &str, value: &str) -> String {
        match self.identifiers.get(scheme).map(String::as_str) {
            Some("letters_digits") => value
                .chars()
                .filter(char::is_ascii_alphanumeric)
                .map(|c| c.to_ascii_uppercase())
                .collect(),
            Some("digits") => value.chars().filter(char::is_ascii_digit).collect(),
            _ => panic!("{scheme:?} is not an identifier scheme"),
        }
    }

    /// Where a source's live statement about `attribute` ranks; lower wins.
    pub fn source_rank(&self, attribute: &str, source: &str) -> usize {
        let order = self
            .attribute_sources
            .get(attribute)
            .unwrap_or(&self.live_sources);
        order
            .iter()
            .position(|s| s == source)
            .unwrap_or(order.len())
    }
}

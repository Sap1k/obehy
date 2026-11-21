//! Stop names as sources write them, compared the one way the keyless inference and its replay
//! report both use (port of `realtime/names.py`). Pure.

use crate::py::text::casefold;

/// Stop names compared without commas, spaces and case (ARRIVA-Q6: `Brno, Benešova
/// tř.,hotel GRAND` is `Brno,Benešova tř.hotel GRAND`).
pub fn same_name(a: &str, b: &str) -> bool {
    normal(a) == normal(b)
}

/// The name without commas and spaces (U+0020 only), case-folded as `str.casefold`.
pub fn normal(name: &str) -> String {
    let kept: String = name.chars().filter(|c| *c != ',' && *c != ' ').collect();
    casefold(&kept)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn arriva_q6_commas_spaces_and_case() {
        assert!(same_name(
            "Brno, Benešova tř.,hotel GRAND",
            "Brno,Benešova tř.hotel GRAND"
        ));
        assert!(same_name("Teplice, Celní", "Teplice,Celní"));
        assert!(!same_name("Teplice,Celní", "A"));
        assert_eq!(normal("Straße ČESKÁ"), "strassečeská");
        assert_eq!(normal("İ"), "i\u{307}");
        // Only U+0020 is dropped, as `str.replace(" ", "")`.
        assert_eq!(normal("A\u{a0}B"), "a\u{a0}b");
    }
}

//! Stop names as sources write them, compared the one way the keyless inference and its replay
//! report both use. Pure.

/// Stop names compared without commas, spaces and case (ARRIVA-Q6: `Brno, Benešova
/// tř.,hotel GRAND` is `Brno,Benešova tř.hotel GRAND`).
pub fn same_name(a: &str, b: &str) -> bool {
    normal(a) == normal(b)
}

/// The normalised names a source may give a stop: the whole name, and for a `townless` source
/// also the name from each comma on (MPVNET-Q10: `Křižíkova` and `Mor. Ostrava,Plynárny` for
/// `Ostrava,Mor.Ostrava,Křižíkova` and `Ostrava,Mor.Ostrava,Plynárny`).
pub fn spellings(stop: &str, townless: bool) -> Vec<String> {
    let mut found = vec![normal(stop)];
    if townless {
        for (at, _) in stop.match_indices(',') {
            let rest = normal(&stop[at + 1..]);
            if !rest.is_empty() && !found.contains(&rest) {
                found.push(rest);
            }
        }
    }
    found
}

/// Whether a source's name is the stop's (`same_name`, or a comma suffix of it when
/// `townless`).
pub fn same_stop(source: &str, stop: &str, townless: bool) -> bool {
    spellings(stop, townless).contains(&normal(source))
}

/// The name without commas and spaces (U+0020 only), lowercase.
pub fn normal(name: &str) -> String {
    let kept: String = name.chars().filter(|c| *c != ',' && *c != ' ').collect();
    kept.to_lowercase()
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
        assert_eq!(normal("Ústí ČESKÁ"), "ústíčeská");
        assert_eq!(normal("İ"), "i\u{307}");
        // Only U+0020 is dropped, as `str.replace(" ", "")`.
        assert_eq!(normal("A\u{a0}B"), "a\u{a0}b");
    }

    #[test]
    fn mpvnet_q10_townless_names_match_a_comma_suffix() {
        let stop = "Ostrava,Mor.Ostrava,Plynárny";
        assert!(same_stop("Mor. Ostrava,Plynárny", stop, true));
        assert!(same_stop("Plynárny", stop, true));
        assert!(same_stop("Ostrava, Mor.Ostrava,Plynárny", stop, false));
        assert!(!same_stop("Plynárny", stop, false));
        assert!(!same_stop("Ostrava", stop, true));
        assert!(same_stop(
            "Terminál HD",
            "Hradec Králové,,Terminál HD",
            true
        ));
        assert_eq!(
            spellings("Zlín,Kostelec,garáže", false),
            ["zlínkostelecgaráže"]
        );
    }
}

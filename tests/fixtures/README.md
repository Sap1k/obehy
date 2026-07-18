# Milestone 0 synthetic fixtures

Every place, schedule, identifier and observation in this directory is fictional. The files
exercise Czech data shapes and UTF-8 text; they are not factual transport data.

The native JDF `.txt` files are the required exception to the repository UTF-8 convention: JDF
uses Windows-1250 and CRLF record terminators. `.gitattributes` prevents Git from rewriting them.

`native/` contains deliberately tiny source-format files. `expected.json` is the normalized
projection consumed by Milestone 0 tests. Native-to-projection equivalence is intentionally
deferred to the JrUtil and connector golden tests in later milestones.

The JDF A/B continuity example uses the mock authoritative identifier `CIS-SYN-0001`. Real
national JDF-derived GTFS stop IDs are not stable between exports, and this fixture must not be
read as evidence that authoritative stop identity is generally available. Structural continuity
matching remains Milestone 1 work.

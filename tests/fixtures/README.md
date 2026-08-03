# Milestone 0 synthetic fixtures

Every place, schedule, identifier and observation in this directory is fictional. The files
exercise Czech data shapes and UTF-8 text; they are not factual transport data.

The native JDF `.txt` files are the required exception to the repository UTF-8 convention: JDF
uses Windows-1250 and CRLF record terminators. `.gitattributes` prevents Git from rewriting them.

`native/` contains deliberately tiny source-format files. `expected.json` is the normalized
projection consumed by Milestone 0 tests. Native-to-projection equivalence is intentionally
deferred to the JrUtil and connector golden tests in later milestones.

The historical JDF A/B example contains a synthetic shared identifier only to describe its source
projection. It is not an Oběhy public ID and must not be treated as stable national identity.
Current pre-registry compiler fixtures emit opaque `v0:` IDs, label the identity contract in the
manifest, and resolve realtime only through mappings belonging to the active static build.

`tests/serving_fixture.py` creates the executable serving-package v1 fixture: one national trip,
one PID posts-only overlay, one operational rail point, source/call/coverage mappings, dated CIS
and train keys, an explicit DÚK alias, and selected-field provenance.

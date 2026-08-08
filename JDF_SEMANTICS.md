# JDF 1.11 semantic preservation contract

This document is a blocking design input for JrUtil serving-package v1 and the Oběhy database-v1
baseline. It describes semantics that the current JDF-to-GTFS conversion either drops or can only
project approximately,
and the typed sidecars required before Oběhy may claim lossless JDF or NeTEx exportability.

The normative source is the Ministry of Transport's [JDF 1.11 specification][jdf-111], especially
the `Pevnykod`, `Caskody`, and `Navaznosti` sections. The immutable source archive remains the
byte-lossless replay boundary. The relations below are the semantic, queryable boundary: generic
JSON/XML payloads and free-text-only substitutions are forbidden.

[jdf-111]: https://md.gov.cz/getattachment/Dokumenty/Verejna-doprava/Jizdni-rady%2C-kalendare-pro-jizdni-rady%2C-metodi-%281%29/Jizdni-rady-verejne-dopravy/metodicky-pokyn-cis-5.pdf.aspx

## Current JrUtil shortfall

JrUtil parses the complete `Pevnykod` enum, but parsing is not preservation. Today it projects only
a subset into GTFS or the conversion bundle:

| JDF fact | Current behavior | Loss or overstatement |
| --- | --- | --- |
| `X`, `+`, `1`-`7` and typed `Caskody` calendar rules | Compiled into GTFS calendars | Calendar meaning is retained, but the source designation/provenance is not always retained when a note has no text. |
| `@`, `{` on a trip | Projected to `wheelchair_accessible` | The distinction between fully and partly accessible vehicles is collapsed. Absence is currently emitted as not accessible rather than unknown. |
| `O` on a trip | Correctly projected to `bikes_allowed=1`; absence becomes `bikes_allowed=2` | The Czech closed-world allowed/forbidden meaning survives in GTFS, but conditions from `Caskody`, exact source code, and provenance are not typed or linked. |
| `x`, `T`, `!`, `(`, `)` at a stop/call | Usefully approximated through GTFS pickup/drop-off values | `phone_before` makes on-demand service discoverable in GTFS, but cannot preserve the distinction between passenger order, an external operating condition, and their exact trip/call scope or instructions. |
| `§`, `A`, `B`, `C` | Existing restriction sidecar | The group is retained, but serving v1 must keep the original route-stop versus trip-call scope without expanding it. |
| `R`, `#`, `[`, `%` and the remaining trip facilities | Mostly dropped or left as text | Reservation availability/requirement, luggage, and refreshments are not machine-readable final facts. |
| Stop codes `@`, `%`, `W`, `w`, `x`, `~`, `$`, `}`, `t`, `v`, `b`, `U`, `S`, `J`, `P` | Mostly dropped | Accessibility, WC, request-stop, CLO, MHD/rail/line/metro interchange, port, airport, and P+R facts cannot be reconstructed. |
| `Navaznosti` `m`/`M` | Compact source sidecar | Direction and supplied columns/text survive, but no typed distinction exists between structured targets, targets parsed from the specification-defined note form, and final resolution. |

Consequently, current GTFS plus current sidecars are **not** sufficient for lossless JDF-to-NeTEx
export. Production serving-package v1 is blocked until the compiler emits the typed relations below.

## Required typed sidecars

All IDs are unrestricted text and all rows belong to one immutable build. Every national semantic
row carries `source_id`, `source_snapshot_sha256`, `source_object_id`, and the original one-character
`source_code`. A regional GTFS overlay may neither author these national facts nor delete them by
omission.

### `service_feature_assignment`

One selected route-, trip-, or call-scoped feature:

- identity and target: `feature_id`, `scope`, nullable `route_id`, `trip_id`, `call_sequence`;
- typed meaning: `kind`, one of `reservation_available`, `reservation_required`,
  `wheelchair_accessible_vehicle`, `partly_wheelchair_accessible_vehicle`,
  `refreshments_on_vehicle`, `luggage_transport`, `bicycle_transport`, `on_request`,
  `conditional`, `self_service_ticketing`, `integrated_transport`, `not_stopping`, `diversion`,
  `request_stop`, `exit_only`, or `boarding_only`;
- evidence: `source_code`, optional `note_id`, and source provenance.

`T` and `!` must retain whether they apply to the whole trip or only marked calls. `O`, `[`, `T`,
and `!` must link to their applicable `Caskody` text where present. GTFS projections are derived
views, never the authoritative representation. For `O`, both the positive marker and its absence
have the Czech meaning JrUtil currently projects; the sidecar adds conditions and provenance rather
than changing that boolean result.

### `location_feature`

One selected physical stop-place feature:

- `feature_id`, `location_id`, `kind`, `source_code`, and source provenance;
- `kind` is one of `wheelchair_accessible`, `refreshments`, `toilet`, `accessible_toilet`,
  `request_stop`, `urban_transport_interchange`, `border_control_only`,
  `visually_impaired_accessible`, `accessibility_terminal`, `rail_interchange`,
  `line_interchange`, `metro_interchange`, `ship_terminal`, `airport_nearby`, or
  `park_and_ride`.

Call-specific `x`, `(`, and `)` belong in `service_feature_assignment`, even when an identically
encoded feature also exists on the stop place. Context is part of the JDF meaning and must not be
flattened.

### `service_note` and `service_note_assignment`

Retain route information, all `Caskody`, and `Mistenky` as verbatim Unicode plus typed note kind,
designation, service-note type, validity, assignment scope, source identity, and provenance.
Calendar compilation does not justify discarding a text-free source designation; it is still needed
for audit and deterministic semantic round-trip. Feature rows link to applicable notes rather than
copying or interpreting their text.

### `connection_claim`

Retain every `Navaznosti` row even if it cannot identify a final target journey:

- source direction `m` as `waits_for` and `M` as `connects_to`;
- origin final trip/call, wait limit, verbatim note, and every supplied target route/stop/post/end
  field;
- optional target source-trip/public-line/destination fields and `target_derivation` of `none`,
  `structured`, or `spec_note`;
- resolution status `unresolved`, `pattern`, or `resolved`, with optional final route/trip/location.

The specification defines standard human-readable forms for both directions. A future JrUtil parser
should extract only fields licensed by those forms, retain the original note, label them
`target_derivation = 'spec_note'`, and report non-matches instead of guessing. This document does
not authorize or implement that parser. Only a uniquely resolved claim may generate a routable
`transfer`; unresolved claims still carry useful wait/connection information and remain exportable
as claims or constraints without invented target specificity.

### Other mandatory semantic relations

- `travel_restriction_assignment` retains `§`/`A`/`B`/`C` and the original route-stop or trip-call
  scope without national call expansion.
- `call_zone` retains each original zone token and `source_order`.
- `source_trip_coverage` retains coverage identity/type, IDS system, role, and inclusive call bounds.
- `operational_location`, `operational_journey`, and `operational_call` retain complete accepted
  CZPTT sequences, including timing-only and non-passenger points.
- `location` retains municipality, district name, actual JDF district code, nearby place, country,
  and coordinate precision.

## Compiler and database gate

JrUtil must materialize these relations from one finalized compiler model alongside GTFS. Oběhy
validates their Arrow schemas, ordering, counts, domains, targets, source provenance, and note links
before attaching any build partition. It does not reinterpret codes or parse connection text.

The PostgreSQL-to-NeTEx mapping ledger must cover every `kind`, scope, and field with either a
standard NeTEx v2.0.0 path or a versioned typed `cz-jdf` extension. A build fails the semantic gate
if a retained fact is omitted, reduced to untyped prose, or exported with stronger meaning than its
source. Fixture coverage must include every code above, trip- and call-scoped `T`/`!`, bike/luggage
conditions, both `m` and `M`, structured and note-derived target examples, and unresolved claims.

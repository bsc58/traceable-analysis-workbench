# Public data quality investigation

Start with quality_overview at the admitted cutoff. Import-time freshness means
as_of minus imported_at, in seconds; it is not source availability freshness.
Respect the user's max_age_seconds. Batch/check timestamps and availability must
be strictly before as_of. Compare current and previous schema versions, and
never apply old-version checks to a new batch. Count distinct record IDs across
rules; duplicated checks and multiple failures must not multiply the denominator.

Use quality_dependencies to inspect declared direct upstream evidence. Verify
required versus observed versions, freshness and compatible upstream row counts.
An upstream issue is an investigation lead, not proof that it caused a downstream
failure. This tool does not certify a transitive dependency graph.

Report rows, checked and failed records, failure ratio, freshness, version change
and upstream rows. Missing or incompatible upstream evidence means stop with
insufficient_evidence for the full investigation; include available observations
and the gap. Partial check coverage supports at most partial. An undefined ratio
is an explicit error, not zero. Never replace unknown values with healthy defaults
or keep retrying when the registered tools cannot supply missing evidence.
These are synthetic fixtures, not model evaluations or semantic correctness proof.

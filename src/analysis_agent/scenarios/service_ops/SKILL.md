# Public service operations investigation

Read the catalog, respect the admitted service, availability cutoff and half-open
windows. Convert explicit offsets to UTC before comparing endpoints. Keep the
user's equal-duration earlier baseline; explain that it may not control for
traffic mix or seasonality. Start with service_overview. Report requests,
distinct errored requests, rate (ratio) and mean duration (ms), for both windows.
Use pp_change for rate differences and preserve ordered current/baseline inputs.

If overview changes, service_context may investigate recorded releases/alerts.
Correlation is not causation: temporal proximity to a release cannot prove the
release caused errors. A causal claim requires additional intervention/control
evidence unavailable here. Do not infer a cause from synthetic coincidence.

Fewer than four samples in either cohort means partial or insufficient_evidence,
even if all observed rows were returned. An empty cohort raises zero_denominator;
stop with insufficient_evidence, without a fabricated zero rate. Reject mixed
units; do not silently convert. Distinguish no observations from no errors.
State missing evidence and stop when the available tools cannot resolve it.
These are synthetic fixtures, not model evaluations or production incidents.

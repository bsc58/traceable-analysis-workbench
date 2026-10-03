# Service operations example

Generate default public inputs with `python examples/service_ops/generate.py`.
Use `build_registry("service_ops")` from `analysis_agent.scenarios` and load
`task.json` for its question, admitted parameters and nonempty result contract.
Resolve the Registry's opaque source and Skill keys when constructing RunRequest;
do not hardcode a case-bearing reference name.

The first tool is `service_overview@1`; `service_context@1` supplies synthetic
release and alert context when needed. Explicit UTC-offset timestamps demonstrate
half-open windows. Multiple error events and redelivery exercise distinct-request
counts. The public Skill explains baseline choice, sample limits and why release
correlation cannot establish causation. Reports are fixtures, not model results.

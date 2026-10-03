# Data quality example

Generate default public inputs with `python examples/data_quality/generate.py`.
Use `build_registry("data_quality")` from `analysis_agent.scenarios` and load
`task.json` for its question, admitted cutoff, freshness limit and result contract.
Resolve the Registry's opaque source and Skill keys when constructing RunRequest.

The first tool is `quality_overview@1`; `quality_dependencies@1` supplies direct
upstream evidence. Import batches, versioned rule checks, duplicate delivery and
dependency edges are independently invented. Freshness is measured in seconds
since import. The Skill explains version compatibility and stopping when upstream
evidence is unavailable. Reports are fixtures, not model results.

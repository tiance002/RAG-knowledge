# M1-B1 independent selection diagnostics

This is an explicit analytics opt-in, `rsi-selection-metrics/m1b1`.
It consumes validated `rsi-metrics/m1a1` Selection projections; it does not
reinterpret historical Parent events or change the frozen S3/S6 metrics.

For each existing compatibility partition let C be the number of completed,
valid selection operations, S the number returning a parent, and n(p) the
number returning parent p. Then:

| Metric ID | Name | Numerator | Denominator |
| --- | --- | --- | --- |
| selection_success_rate | Selection Success Rate | S | C |
| selected_parent_diversity | Selected Parent Diversity | number of distinct returned parents | S |
| conditional_parent_hhi | Conditional Parent HHI | sum of n(p) squared | S squared |

Completed empty operations increment C and leave S unchanged. Thus one empty
and one parent-returning operation give success 1/2, diversity 1/1, conditional
HHI 1/1, while the existing m1a1 S3 and S6 remain 1/2 and 1/4.
When C is positive but S is zero, success is zero and both conditional metrics
are N/A with observed zero denominators. Absent observations or C=0 produce
UNKNOWN, never an inferred zero. Interrupted, invalid, missing-identity,
unknown-parent and conflicting operations are excluded from C and reported in
unknown_operations. Results describe observed completed operations, not an
unconditional estimate for interrupted operations. No selection-quality,
adoption, verification or causal claim follows from these diagnostics.

Operations are keyed by source identity and selection_id. Repeated matching
semantic snapshots count once; conflicting completed/valid/parent/returned
snapshots make the operation UNKNOWN. Contradictory identical projection IDs
still fail the existing projection-integrity check. Real projected records have
validated source_refs; direct callers without source_refs share an unscoped
selection_id namespace and should provide explicit source identities.

Python APIs:

```python
compute_metrics(records, selection_contract_version='rsi-selection-metrics/m1b1')
build_report(paths, selection_contract_version='rsi-selection-metrics/m1b1')
```

CLI: add `--selection-contract-version rsi-selection-metrics/m1b1` to
`python -m rsik.analytics.report`. The opt-in adds selection_metrics per
partition, a separate report selection_metrics_schema, and a versioned schema
section. Omission preserves the prior JSON/Markdown/schema content. Unknown
versions fail validation. All computation is read-only and invokes no model.

Partitions retain experiment, epoch, role profile version and manifest hash,
evidence mode, compatibility fingerprint, evaluator version, proposal protocol
profile and input metric contract. No comparisons pool across these axes.
Agent roles remain separately available within each partition. Portfolio exact
hash identity is not semantic equivalence or evidence of improved answers.

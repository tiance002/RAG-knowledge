# Portable test evidence

Run from the repository root:

```text
python -m pytest rsik/tests backend/tests/test_rsi_candidates.py backend/tests/test_rsi_feedback.py backend/tests/test_rsi_decision.py backend/tests/test_rsi_business_source.py -q
```

`portable_fixtures.py` builds small invented source documents, questions, required
clauses, source hashes and source-coordinate maps in pytest temporary directories.
It reuses the checked-in foundation builder, frozen evaluator, source provenance
assessor and decision reader. `SyntheticCapsule` emits deterministic capture doubles;
it does not execute the delivery worker, backend, a container or an external model.
The source/query stress variants are generated from those invented sources.

Synthetic execution receipts in `test_experiment_adapter.py` exercise raw versus
canonical configuration hashing and rejection of tampering. Their runtime/image
metadata is explicitly synthetic. They are not evidence that a container ran.
`test_query_preview.py` compares two readers over the same synthetic captures and
checks complete 80-frame coverage; this proves reader parity for the constructed
fixture, not independent delivery quality or runtime parity.

Curation tests construct attested public-context contracts and quarantine events
locally. These validate contract binding and state readers, not real model output
or verified production candidates. Robustness tests use explicitly invented paired
outcomes to validate hard gates and statistical denominators.

These tests require no ignored `var/` history, private QA/Gold, source corpus,
credentials, remote host or network/model calls. Historical real-run evidence is
separate and is not packaged as a public fixture. No test skips absent history.
Temporary files are managed under pytest's temporary-directory retention policy;
no corpus, database, index, dependency environment or container is copied/created.

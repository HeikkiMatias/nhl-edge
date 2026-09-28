---
name: add-data-source
description: Add a new data source end to end - docs entry, raw cache, parser, schema, fixture and tests.
  Use when a new API, file or endpoint is added to the pipeline.
argument-hint: "[source name]"
---
Add the data source: $ARGUMENTS. Use the data-engineer subagent for the build.

1. **Docs.** Add the source to docs/data-sources.md: what it gives, access and limits, terms of use,
   and its role in the model. List every endpoint used. Stop and ask if the terms forbid automated
   access or the endpoint is paid.
2. **Raw cache.** Write responses untouched as JSON under raw/, keyed so a rerun can replay them without
   calling the source. Throttle requests and record the fetch time.
3. **Parser.** Parse raw JSON into a Polars frame in src/nhl_edge/ingest/. Every row gets observed_utc,
   when the fact became public, not when it was fetched if the two differ.
4. **Schema.** Add a pandera schema to src/nhl_edge/lake/schemas.py and validate on every write.
5. **Fixture.** Save one small real response as a test fixture under tests/unit/fixtures/. Golden games
   in tests/golden/ are frozen by hand only; propose one if it is needed, never write it.
6. **Tests.** Parser unit tests on the fixture, a schema test, and a leakage test in tests/leakage/ that
   rejects rows observed at or after the prediction time.
7. Run `uv run ruff check . && uv run pyright src && uv run pytest -m "not slow" -q`.

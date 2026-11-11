## What and why

<!-- One or two sentences. -->

## Honesty checklist

Answer each point; "nothing" is a valid answer, a blank is not.

- **What is mocked, faked or stubbed in this change, and why?**
- **What was verified for real (engine, running stack, model), and how?** Name the
  database or model version and the command, or say it was not.
- **Which row of [`docs/verification-status.md`](../docs/verification-status.md) did you
  add or update?** Claim only the level you reached: tests with fakes are `FAKES`.
- **If this changes engine behaviour** (prompts, retrieval, candidate preparation,
  validation, the ontology): was it measured with the eval harness, on which
  questions, and are they **development** questions (the ones that motivated the
  change) or **held-out**? Attach both scores side by side, strict and tolerant,
  and say if the cases file or the index state differed between runs.

## Checks run locally

- [ ] `python -m dev_tools.integrity`
- [ ] `uv run ruff check auto_ontology/ dev_tools/ && uv run ruff format --check auto_ontology/ dev_tools/`
- [ ] `uv run pytest auto_ontology dev_tools -q`

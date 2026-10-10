## What changed

## Why

## Code checklist (delete for data-only PRs)

- [ ] Tests added or updated, and `uv run pytest` passes
- [ ] `uv run atlas validate` passes (and `uv run atlas schema export --check` after a schema change)
- [ ] No tokens, keys or other secrets in code, fixtures, logs or this description
- [ ] New workflow steps pin actions to a full SHA and keep `permissions: {}` at the top

## Data checklist (delete for code-only PRs; 07 §6.7)

- [ ] Each quote supports the claim it is attached to
- [ ] Each status fits its definition (07 §2.3)
- [ ] Each MW value has the right basis (`it_mw`, `facility_mw` or `utility_request_mw`)
- [ ] Bulk PRs (`bulk` label): every flagged item reviewed, plus a 10% random sample; a sample error
      rate above 5% blocks the merge. An error is a published fact the record's own cited sources do
      not support (07 §8); a stale source is corrected in `config/overrides/{source}.json` instead

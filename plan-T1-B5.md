# T1-B5: Ranking pipeline (no LLM yet) — plan

## Scope (5 bullets)

1. `oxi_core/v3/ranking.py` — four pipeline stages: `prefilter` (FTS5),
   `bm25_score`, `vector_rerank` (all-MiniLM-L6-v2), `rrf_combine` (k=60).
2. `build_corpus` — assembles docs from `roadmap.md`, `CHANGELOG.md`,
   and last-90-day merged PRs via `GitHubClient.list_merged_prs`.
3. `GitHubClient.list_merged_prs` — new method added to protocol +
   `GhCliClient` implementation + `FakeGitHubClient` stub.
4. `tests/test_ranking.py` — 81 tests over a fixed 50-item synthetic
   corpus with a `DeterministicModel` stub (no network, fast, deterministic).
5. `pyproject.toml` — confirm `sentence-transformers>=2.0` dep; no new dep
   required. sqlite-vec extension *not* needed (pure-NumPy cosine).

## File list

- `oxi-core/src/oxi_core/v3/ranking.py` (new)
- `oxi-core/tests/test_ranking.py` (new)
- `oxi-core/src/oxi_core/v3/github_client.py` (modified: `list_merged_prs`)
- `oxi-core/tests/fixtures/fake_github.py` (modified: `add_merged_pr`)
- `oxi-core/pyproject.toml` (modified: confirm sentence-transformers dep)

## Commit ordering

1. `T1-B5: plan` — this file
2. `feat(ranking): add list_merged_prs to GitHubClient protocol + GhCliClient`
3. `feat(ranking): add FakeGitHubClient.add_merged_pr + list_merged_prs stub`
4. `feat(ranking): implement ranking.py pipeline`
5. `test(ranking): add 81-test suite with 50-item deterministic corpus`
6. Final: push + open PR

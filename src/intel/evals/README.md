# Evals — hill-climbing harness

A small, focused regression + failure-mode suite for the `intel` package. Each task pairs a deterministic runner with one or more graders; results persist to `eval_runs` / `eval_task_results` in the live db so progress is trackable across commits.

## Run it

```bash
.venv/bin/intel evals run               # all tasks
.venv/bin/intel evals run --task R3,R6  # subset
.venv/bin/intel evals run --task regression
.venv/bin/intel evals list              # show discovered tasks
.venv/bin/intel evals history           # recent runs
.venv/bin/intel evals dashboard         # rebuild HTML without re-running
.venv/bin/intel evals build-seed        # refresh fixtures/seed.db from data/intel.db
```

Tasks needing the Anthropic API run only when `ANTHROPIC_API_KEY` is set in the env (or `.env`) — otherwise they're skipped, not failed. Skipped tasks don't count toward the headline score.

### Stub LLM mode (no API key needed)

Set `INTEL_LLM_STUB=1` to run the LLM-backed tasks against hand-authored canned responses ([_stub_llm.py](_stub_llm.py)) instead of skipping them:

```bash
INTEL_LLM_STUB=1 .venv/bin/intel evals run
```

In stub mode, the runner monkeypatches `anthropic.Anthropic` in the analyzer modules (`creative`, `offers`, `themes`, `whitespace`, `briefing`, `homepage_hero`) for the duration of the run; the stub matches on the system-prompt header and returns a fixture-aware response. This exercises the full schema/parsing/grader plumbing for the LLM-backed tasks without making a network call.

**What stub mode does NOT do:** it doesn't measure the actual model — the canned responses are written to pass the graders, by design. Use it for harness/plumbing regression detection (does the code path still parse the response correctly? do the graders still trigger on the right shapes?) but not for prompt-quality measurement. For prompt evaluation, run with a real key.

The canned responses live next to the routing logic in [`_stub_llm.py`](_stub_llm.py) — see the `_PROMPT → response` table near the top and the per-prompt builder functions below it. When a new LLM-backed task or a new prompt shape lands, add a router branch + canned response.

## How a run works

1. `fixtures/seed.db` is copied to a tmp file; `INTEL_DB_PATH` is repointed there so tasks under test never mutate the real db.
2. Bookkeeping (`eval_runs`, `eval_task_results`) writes to the **real** `data/intel.db`, so history persists across runs.
3. Each task's runner produces an output object; each grader inspects it and emits a `GraderOutcome`.
4. Status:
   - `PASS` — all correctness graders pass AND all speed graders pass
   - `PASS_SLOW` — all correctness graders pass, but at least one speed grader failed (partial credit 0.5)
   - `FAIL` — any correctness grader failed
   - `ERROR` — runner raised
   - `SKIP` — task requires an API key that isn't set
5. Headline score: `(pass + 0.5 * pass_slow) / (pass + pass_slow + fail)`. SKIPs aren't scored.
6. HTML dashboard is written to `reports/<date>/evals/index.html` after each run.

## Most recent runs (2026-06-04)

| Run | Mode | PASS | SLOW | FAIL | SKIP | Headline | Notes |
|---|---|---|---|---|---|---|---|
| #6 | `INTEL_LLM_STUB=1` | **10** | 0 | 0 | 0 | **100%** | Stub LLM — all 10 tasks exercised end-to-end against hand-authored canned responses. R5 citations pulled from real seed ad_archive_ids. |
| #3 | unset key       | 3  | 0 | 0 | 7 | 100%     | Baseline — only the 3 no-key tasks (R3, R6, R9) run; the 7 LLM-backed tasks skip cleanly. |

Dashboard: [reports/2026-06-04/evals/index.html](../../../reports/2026-06-04/evals/index.html).

## The 10 tasks

The numbering has a deliberate gap (R4 was removed when its assertion turned out to overlap with R3). New tasks should follow `<category-prefix><next-int>_<short_slug>.py` and live in `tasks/`.

### Regression — `tasks/R*.py`

| ID | Title | Module under test | Needs key | What it asserts |
|---|---|---|---|---|
| **R1** | Vision taxonomy on lifestyle image produces structured output | `analysis.creative.analyze_creative_image` | yes | Returns a dict with no `error`; `photography_style`, `hook_style`, `production_style` are valid enum values; `dominant_colors_hex` is non-empty; `confidence ≥ 0.5`; wall ≤ 30s. Fixture: `fixtures/images/lifestyle.jpg`. |
| **R2** | Offer extraction finds percent_off + free_shipping + code | `analysis.offers.extract_offers_from_text` | yes | On a hard-coded Memorial Day Sale string, returns a non-empty offers list containing at least one `percent_off` and one `free_shipping`; the first item's `value` contains a number; wall ≤ 15s. |
| **R3** | Cross-set comparison surfaces distinctiveness + whitespace | `synthesis.creative_readout.cross_set_comparison` | no | Returns a dict (`body_md`) referencing the words "Distinctiveness" and "whitespace" plus at least one real brand (bobs/wayfair/ashley); wall ≤ 5s. |
| **R5** | LLM briefing cites real ad_archive_ids | `synthesis.briefing.generate_briefing(days=30)` | yes | Briefing body contains ≥ 2 explicit `[#ad:<id>]` citations and every citation resolves to a real ad in the seed db; wall ≤ 60s. *Known gap*: current prompt doesn't enforce the citation grammar — this is the failing test that drives Phase 1's citation enforcement. |
| **R6** | Deterministic briefing covers active competitors | `synthesis.briefing.generate_briefing(use_llm=False, days=30)` | no | The no-LLM fallback persists a briefing whose body has "Competitive Briefing", "TL;DR", and "new ad"; wall ≤ 3s. |
| **R7** | Hook clustering produces ≥1 cluster on furniture ads | `analysis.themes.cluster_hooks` | yes | On 30 furniture ads pulled from seed, returns a dict with a non-empty `clusters` list; wall ≤ 45s. |
| **R8** | Whitespace detection returns testable hypotheses | `synthesis.whitespace.detect_whitespace` | yes | Returns a dict with a non-empty `whitespace` list; the first item's `testable_hypothesis` is non-empty; wall ≤ 45s. |
| **R9** | Per-brand creative readout (bobs) has expected sections | `synthesis.creative_readout.per_brand_readout("bobs")` | no | Returns a dict with a `body_md` containing "Bob" and at least one of `photography_style \| production_style \| hook_style`; wall ≤ 3s. |

### Failure mode — `tasks/F*.py`

Failure-mode tasks target known weak spots. The expected behavior is graceful degradation, not "produce something."

| ID | Title | Module under test | Needs key | What it asserts |
|---|---|---|---|---|
| **F1** | Vision on low-quality image signals uncertainty via confidence | `analysis.creative.analyze_creative_image` on `fixtures/images/low_quality.jpg` | yes | The model returns a dict and self-reports `confidence < 0.85` on an intentionally low-information image — proving the confidence field is calibrated rather than always-high. Phase 1 may upgrade this to gate downstream usage on the score. |
| **F2** | Zero-activity briefing degrades gracefully (no hallucinated ads) | `synthesis.briefing.generate_briefing(days=0)` | yes | With `days=0` (empty corpus), the briefing body contains no suspicious 14+ digit numbers (would-be `ad_archive_id` hallucinations) and explicitly states the window is quiet ("no material", "no activity", "quiet", etc.); wall ≤ 20s. |

## Grader vocabulary

Composable in any task's `graders=[...]` list. All live in `graders.py`.

**Correctness**
- `output_is_dict_no_error()` — runner returned a dict that lacks an `error` key.
- `exact_match(path, expected)` / `in_set(path, allowed)` — dotted-path lookup, value check.
- `at_least(path, minimum)` — numeric floor.
- `list_non_empty(path)` / `list_has_kind(path, kind)` — list shape checks.
- `regex_present(path, pattern)` / `regex_absent(path, pattern)` — text content.
- `schema_valid(validator)` — custom schema check; Phase 1 will swap in pydantic.
- `db_row_exists(table, where, params)` — side-effect: a row landed in the seed db.

**Speed** (failure → `PASS_SLOW`, not `FAIL`)
- `wall_budget_from_elapsed(budget_sec)` — reads `ctx._elapsed_sec` set by the runner.

**LLM judge** (stub in Phase 0)
- `llm_judge(rubric, min_score=0.7)` — returns pass-as-stub until `intel.agent.judge` ships in Phase 3.

## Where things live

| Path | Purpose |
|---|---|
| `src/intel/evals/runner.py` | Suite discovery + `run_task` + `_seeded_db` context |
| `src/intel/evals/base.py` | `Task`, `TaskContext`, `GraderOutcome`, `TaskRunResult` |
| `src/intel/evals/graders.py` | Reusable grader factories |
| `src/intel/evals/dashboard.py` | HTML dashboard renderer |
| `src/intel/evals/seed.py` | Build `fixtures/seed.db` from live `data/intel.db` |
| `src/intel/evals/tasks/` | One file per task; module exposes a `task = Task(...)` symbol |
| `src/intel/evals/fixtures/seed.db` | Frozen db copied per run |
| `src/intel/evals/fixtures/images/` | Pixel inputs for vision tasks |
| `data/intel.db` (tables `eval_runs`, `eval_task_results`, `llm_calls`) | Run history + per-task graders + per-LLM-call telemetry |
| `reports/<date>/evals/index.html` | Per-run dashboard |

## Adding a task

1. Drop a file at `tasks/<id>_<slug>.py` exposing a `task = Task(...)`.
2. Reuse graders from `graders.py` where possible; custom graders are just `(out, ctx) -> GraderOutcome` callables.
3. If the task mutates the seed db, expect the mutation to be discarded — the seed is copied per run.
4. If the task needs new fixture data, drop it under `fixtures/`.
5. Set `requires_anthropic_key=True` for any task that hits the model; the runner will SKIP it cleanly when no key is set.
6. Add a row to the table above so the doc stays accurate.

## Open work

- **R5 enforcement** — currently FAILs because the briefing prompt doesn't require `[#ad:<id>]` citations. Phase 1 of the bible-aligned plan adds citation enforcement; this task is the gate.
- **No homepage / brand-store coverage yet** — Phase H1–H3 shipped the website Playwright migration + Amazon brand-store adapter + dashboard lanes but no eval task pins those behaviors. Candidate additions: `R10_homepage_adapter_capture`, `R11_extended_taxonomy_fields_populate`, `R12_hero_extractor_cache_hit`. The harness is ready — the gap is task coverage.
- **LLM judge** — currently stubbed in `graders.llm_judge`; real implementation lands when `intel.agent.judge` is built (Phase 3).

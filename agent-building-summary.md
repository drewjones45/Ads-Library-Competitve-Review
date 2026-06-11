# Summary & Considerations for Building Agents

*Talk + workshop: **"Tool, skill, or subagent? Decomposing an agent that outgrew its prompt"** — Code with Claude (London), **Workshop W5**, 45 min, presented by **Will**, an engineer on Anthropic's **Applied AI** team (he splits his time between internal engineering and building agents with customers). In the workshop repo it's titled **"StockPilot — Compose multi-agent systems with Skills and MCP."***

*This version is **grounded in the actual workshop repository** (`cwc-workshops/agent-decomposition`), so it corrects several numbers the talk stated from memory and adds the real code-level detail. A "Corrections vs. the talk" section is at the end.*

## What it's about

The workshop dramatizes a situation most teams hit: you ship an agent, it works, and then you keep bolting on capability as new business requirements arrive. Each addition is individually reasonable. The aggregate is the problem — the system prompt swells, tools and subagents multiply, and the agent starts **regressing in the areas it used to be good at**. The cure is **decomposition**: for each capability decide whether it should be a **tool**, a **skill**, or a **subagent**, and let an **eval suite** gate every change.

## The case study: StockPilot

**StockPilot** is an inventory-management agent for a **mid-size outdoor-gear retailer** with three warehouses (WH-EAST/Carlisle PA, WH-WEST/Reno NV, WH-CENTRAL/Kansas City MO) and ~250 SKUs of seeded data. It flags low stock, forecasts demand, picks suppliers, files purchase orders, and writes weekly reports. None of that is individually hard; the architecture is the issue.

**The "before" architecture (the anti-pattern), exactly as it ships in `agents/before/`:**

- A single **orchestrator** running a **hand-rolled while-loop on the raw Messages API** (`stockpilot.py`: `client.messages.create(... tools=TOOL_DEFS ...)`, dispatch tool calls, loop until `end_turn`, `max_turns=25`).
- A **402-line system prompt** (`before/prompts.py`) grown by appending policy after policy — operating cadence, prioritization, transfer-vs-reorder, promo handling, reorder formulas, supplier quirks, a seasonal calendar, eight worked examples, escalation matrix, edge cases, a glossary.
- **12 tools**, **3 of which are thin wrappers around subagents** (`before/subagents.py`): a *forecasting* subagent ("Demand Forecasting Analyst"), a *procurement* subagent ("Procurement Specialist"), and a *writing* subagent ("Communications Writer"). Each is its own Messages API call that **returns prose**, and the orchestrator parses what it needs out of the text.

## The eval suite (the real spec)

`evals/tasks.yaml` defines **12 tasks**; `evals/graders.py` implements the graders. Score = **(PASS + ½·PASS-SLOW) / 12**.

- **R = regression** (R1–R9): realistic **single-turn** tasks — lookup, list-below-reorder, create PO, supplier lead times, ERP adjust, reorder rec, 14-day forecast, promo-month forecast, weekly report.
- **F = failure-mode** (F1–F3): harder **multi-turn** tasks that target specific architectural smells.

**Graders, deterministic and non-deterministic:**

- **Deterministic** — `exact_match`, `set_match`, `numeric_tolerance`, `action_taken` (checks the side-effect actually written to a sink), and the *efficiency* family: `wall_budget` (latency), `efficiency` (turn/token budgets), `ranked_mention`, `regex_present`, `composite`.
- **Non-deterministic** — `llm_judge`, which sends the response to a model (Sonnet 4.6) with a rubric and reads back PASS/FAIL. Used for R9 (is the weekly report actually usable?).

**The three highlighted failures and their real root causes (`evals/baseline_starter.json`):**

- **F1 — daily low-stock sweep — FAIL on wall budget.** Correct answer, but ~580s (budget 270s) because the agent makes **100+ serial tool calls** (`list_low_stock` dumps every below-reorder row into context, then per-SKU `get_stock_level`/`get_sales_velocity`). Tag: *context-bloat*. Fix: drop the data-dump tools; run a **Bash batch script** over the CSVs (`forecasting/batch_days_of_cover.py`) — the "What changed" table shows **488s · 102 tool calls → ~100s · 3 scripts**.
- **F2 — promo reorder w/ forecast — FAIL on a dropped number.** The forecasting subagent states confidence **qualitatively** ("moderate-low") instead of as a number, and the grader (`regex_present` for `confidence … 0.NN`) can't find it. This is the "subagent↔orchestrator communication breakdown" the talk described, made concrete: **the number is lost in the prose hand-off.** The forecasting skill's rule: *"anchor the number, not the narrative"* — require `{forecast_qty, confidence, method, flags}` JSON and parse it strictly.
- **R8 — promo-month forecast — FAIL on mean-anchoring.** The agent under-forecasts the promo because promo guidance is scattered across several sections of the 402-line prompt and it anchors on the rolling mean. The grader (`numeric_tolerance ±30%`, must mention "promo"; expected ≈ mean × 30 × **2.5** uplift) literally reports *"anchored on mean?"* on failure. (In the talk Will narrated this as the agent pulling the right multiplier then "hallucinating" a smaller one — same lesson: a long, internally-contradictory prompt is a **context** failure, not a model failure.)

## The method: hill climbing, triaged in Claude Code

The loop Anthropic calls **hill climbing**: baseline → change one architectural thing → re-run the affected tasks → keep what moves the score up. Will drove it **inside Claude Code** (running **Opus at extra-high effort** as the *developer's* assistant — distinct from the agent itself, which runs on Sonnet 4.6) and used Claude Code's **Bash** tool to run the evals and **triage the failures into themes**: the model doing work it should have a tool for; weak output-structure enforcement; and policy confusion from the long prompt. The repo's three cycles:

| Cycle | Observe / diagnose | Decide | Verify |
|---|---|---|---|
| 1 | "Where does `LEGACY_PROMPT` duplicate `notify-templates/SKILL.md`?" | Swap `LEGACY_PROMPT → SHORT_PROMPT`; enable `notify-templates` + `weekly-report` | `--task F3` |
| 2 | "Count tool calls in the F1 transcript by name" | Drop the data-dump tools; agent uses Bash over the CSVs | `--task F1` |
| 3 | "Where does the number get lost?" (F2) | Enable `forecasting`, drop `forecast_demand`; decide *how* (or whether) a second agent gets involved | `--task F2,R7` |

## Fix 1 — long system prompt → skills (progressive disclosure)

**Skills** are packaged, composable information (a `SKILL.md` with YAML front-matter plus optional bundled scripts) that the agent pulls into context **only when its trigger matches**. The rule: the **system prompt holds only what's true regardless of the task**; everything "sometimes" becomes a skill.

The repo ships **5 domain skills** — `reorder-policy`, `supplier-selection`, `forecasting`, `notify-templates`, `weekly-report` (plus a `submit-solution` workshop-plumbing skill). Each front-matter `description` is written as a **trigger** ("Load this whenever a task involves reorder recommendations, purchase orders, or 'should we restock'"). `forecasting/` bundles real scripts (`rolling_mean.py`, `batch_days_of_cover.py`).

Result: the **402-line prompt becomes a 15-line `SHORT_PROMPT`**, with ~400 lines moved into skills that load on demand. As the README puts it: *"The knowledge didn't shrink — 402 prompt lines became 400 skill lines. The difference is when they're in context."*

## Fix 2 — 12 bespoke tools → human-like primitives

Anthropic's tooling principle: **lean into the same primitives a human has at a desk** — code execution, a filesystem, a to-do list, web search — and add custom tools only for real gaps. On **Claude Managed Agents** these come built in as the `agent_toolset` (Bash, file read/write, Task), so you don't reinvent them.

The canonical move, straight from the skills: stop paging data through tool calls and instead **write a Python script via Bash that reads the CSVs and prints compact JSON**. `supplier-selection` says it outright — *"Ranking suppliers is arithmetic, not judgment. Compute it in Python via code execution — do not reason about it in prose"* — and ships a scoring formula (`0.5·(1−price) + 0.3·(1−lead) + 0.2·reliability`). Most of StockPilot's 12 tools collapse into "read the CSV, compute, write a JSONL sink." The `SHORT_PROMPT` instructs: *"For any operation touching >5 SKUs, write a Python script via Bash … don't page through tool calls."*

## Fix 3 — subagents: make delegation a runtime decision

The `before` agent hid 3 subagents inside Python tools. On CMA there's no nested API call inside a tool, so cycle 3 asks you to choose **how** to delegate the one genuinely isolation-worthy task (a full-history promo forecast). The repo gives **three real options**, with explicit security trade-offs (`agents/cma.py`):

- **(a) `callable_agents`** — attach a deployed `forecaster` agent (its own fixed system prompt, Bash+Read only) as a callable. The safest option; multi-agent went GA 2026-05-06.
- **(b) `spawn_subagent`** — a custom tool that spins up a generic bash-capable `worker` session per task. Most flexible *and* most exposed; the worker's system prompt is **fixed server-side** so injected instructions in upstream data can't redefine its role, and inputs are sanitized.
- **(c) inline** — just compute the rolling mean yourself and set `confidence ≤ 0.55` so the reorder-policy skill escalates to a human instead of auto-ordering on an unvalidated number.

The forecaster returns strict `{forecast_qty, confidence, method, flags}` JSON; `confidence < 0.6` triggers human-review escalation. Net result in the "What changed" table: **hardcoded subagents 3 → 0** — delegation is now a *runtime* decision, not baked-in wiring.

## Claude Managed Agents (CMA)

The workshop **migrates from the raw Messages API to CMA**. CMA provides the harness and managed infrastructure — scaling, security, memory, and a **per-session isolated sandbox** — so you only design the agent. Concretely:

- The agent config is just `{name, model, system, tools, skills}` in `agents/starter/agent.py`; `agent_toolset_20260401` gives Bash/Read/Task by default.
- The CMA sandbox mounts the data CSVs at `/mnt/session/uploads/data/`; the agent writes side-effects (`purchase_orders.jsonl`, `outbox.jsonl`, `erp_writes.jsonl`) to `/mnt/user/sinks/`.
- Real commands: `uv sync` → `cp .env.example .env` (+ `ANTHROPIC_API_KEY`) → `uv run seed` (250 SKUs) → `uv run evals --agent before` (~9 min, local) → `uv run deploy starter` (~20s) → `uv run stockpilot --agent starter "What's the stock for SKU-0042 at WH-EAST?"` → `uv run evals --agent starter --task F1`. The Console shows each session's full transcript (every tool call, every Bash command, every skill loaded) — the debugging surface.
- *Note from the README:* in production the data-access pattern is **MCP**; the workshop uses uploaded CSVs as simpler plumbing for the same lesson.

## What changed (measured, from the repo)

| | Before | After |
|---|---|---|
| Score | **71%** (reference band 63–75%) | **92%** |
| F1 daily sweep | 488s · **102 tool calls** | ~100s · **3 scripts** |
| System prompt | **402 lines** | **15 lines** (+400 lines of skills, on demand) |
| Hardcoded subagents | **3** | **0** (delegation is a runtime decision) |

## What the workshop covered after the ~30-min cutoff

The transcript ended mid-sentence on "you're always gonna need some custom tools." The repo fills in the rest: you keep a **few** custom tools for genuine gaps (system-of-record writes, side-effects), but cycle 3 is about **choosing a delegation primitive** for the forecaster (the three options above), re-running `F2,R7` to confirm the confidence number now survives, and landing the score at ~92%. The workshop closes with the **`submit-solution`** skill, which walks attendees through `git diff`-ing their `starter/agent.py`, capturing their final eval score, and opening a PR whose body doubles as the workshop feedback form (which subagent approach they chose, hardest part, one thing they'd change).

## Corrections vs. what the talk said

- **Baseline score.** The talk said the suite "passes up front at about **83%**" and then a live run came back **62% (7/12)**. The repo's reference band is **63–75%** (`reference_scores.json`), the README says "expect ~71%", and the measured **after** is **92%** (not "83–100%"). So treat 83% as a misremembered figure; ~71% → 92% is the real arc. The 62% live run is a bad-luck run a touch below the band — which is exactly why the talk's own lesson ("don't trust one number; read the failures") applies.
- **System-prompt shrink.** Talk: ~400 → ~50 lines. Repo: **402 → 15** lines.
- **"One well-placed subagent."** The intro implies keeping a subagent, but the measured end-state is **0 hardcoded subagents**; the one forecast delegation is a *runtime* choice via `callable_agents`/`spawn_subagent`/inline.
- **Which model.** "Opus at extra-high effort" was the **Claude Code developer assistant** used to triage evals. The **StockPilot agent itself runs on Sonnet 4.6** (`STOCKPILOT_MODEL` default), and the LLM-judge grader also uses Sonnet 4.6.
- **R8 specifics.** The "pulled 3.1× then used 1.35×" numbers were illustrative narration; the graded R8 uses SKU-0116, a 30-day horizon, and a 2.5× promo uplift with ±30% tolerance. The lesson (mean-anchoring from a contradictory long prompt) is exactly as described.
- **Year.** The StockPilot agent is framed as "2025-era," but the **event/workshop is Code with Claude 2026 (W5).**
- **"5 grader types."** There are really 10 grader *functions* in `graders.py`; they group into ~5 conceptual types (string/set match, numeric tolerance, action/side-effect, efficiency/budget, and LLM-judge). The split the talk drew — deterministic (turns, latency, tokens) vs. non-deterministic (tone, quality) — is accurate.

---

*Everything above is grounded in `cwc-workshops/agent-decomposition`. Beta/feature names (CMA `agent_toolset_20260401`, `callable_agents`, the `managed-agents-2026-04-01` header) reflect the repo as of the workshop and may change — check current Anthropic docs.*

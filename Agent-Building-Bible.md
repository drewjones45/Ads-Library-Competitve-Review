# The Agent-Building Bible

*A practical, reusable reference for designing and maintaining world-class agents with Claude (Claude Code and Claude Managed Agents).*

This is a **principles** document. The running examples come from a real Anthropic workshop agent ("StockPilot," an inventory assistant) that was deliberately built as an anti-pattern and then decomposed — but the goal here is **not** to reproduce that agent. It's to extract the design principles, and to use the real before-state and the real fixes as concrete illustration. Wherever you see StockPilot code, read it as *"here's the principle in the wild,"* not *"build this."*

---

## 0. The philosophy in one paragraph

A great agent is **small, legible, and measured**. Give the model a few powerful, human-like primitives; keep its context window clean; load specialized knowledge only when the task needs it; split work into isolated reasoning units only when one context can't hold it; and let an **eval suite** — not taste — tell you whether each change helped. Capability is added *deliberately*, revisiting the architecture each time, never bolted on.

**The failure you're always fighting** is accreted complexity. The canonical case: an agent ships, works, and then grows — six months later it has a 400-line system prompt, a dozen tools, a few hardcoded subagents, and it now *regresses* in the areas it used to be good at. Every individual decision was defensible; the aggregate is the problem. The cure is **decomposition**.

---

## 1. The core decision: tool vs. skill vs. subagent

Every new capability is a fork. Pick the cheapest primitive that does the job. The spectrum runs from cheap/weak to powerful/expensive:

```
   TOOL CALL              SKILL                      SUBAGENT
   one function,          instructions + assets      a separate agent with its
   stateless,             the agent loads on         own context window and
   deterministic          demand                     its own goal
   ───────────────────────────────────────────────────────────▶
   cheaper, weaker                         more power, more cost
```

| Primitive | What it is | Reach for it when… | Cost / risk |
|-----------|-----------|--------------------|-------------|
| **Tool** | A discrete action or I/O the model invokes. | The capability is an *action* or data access the model can't do by reasoning (write to a system of record, call an API, run code). | Surface area: a schema to learn + output that lands in context. Many tools → decision paralysis + context bloat. |
| **Skill** | Packaged *knowledge/procedure* (a `SKILL.md` + optional scripts) pulled into context on demand when its trigger matches. | The capability is *information or a workflow* needed only **some** of the time (policies, formats, domain procedures, brand specs). | Nearly free when idle. Main risk: a vague trigger so it never loads — write a sharp description. |
| **Subagent** | A separate agent with its **own context window** and goal, invoked by an orchestrator. | A sub-task needs **real isolation** — large data in context, a long/noisy intermediate process, or parallel independent work. | The most expensive: extra round-trips + a **communication seam** that is a top source of bugs. |

**Decision heuristics**

1. **Default to a tool or skill.** Reach for a subagent only when a single context genuinely can't do the job.
2. *Action ⇒ tool. Knowledge ⇒ skill. Isolation ⇒ subagent.*
3. **Don't fake subagents.** A "subagent" that's a thin wrapper around hardcoded logic gives you all the cost and none of the benefit. If it doesn't need its own reasoning and context, it's a tool — or just code.

**Smell tests (fast, field-tested):**

- A tool returns **>2k tokens**? → it should be **code execution** over the data, not a tool that dumps rows into context.
- You're writing **"always do X before Y"** in the system prompt? → that's a **skill**.
- A subagent's **output is one number**? → it shouldn't be a subagent.

---

## 2. Human-like primitives first

When you build an agent, start by giving Claude the same primitives a capable human has at a desk, and remove what you don't need:

- **Code execution** (a Bash/code tool)
- **A filesystem** to read and write
- **A to-do list** / scratch space for planning
- **Web search**

**Why this wins:** these primitives compose to cover an enormous range of tasks, and they get *better for free* as stronger models drop in — same tools, used more skillfully. (Claude Code is essentially "Claude + a computer.") On **Claude Managed Agents** these are the built-in `agent_toolset` (Bash, file read/write, Task), so you don't write your own code-execution or filesystem tool.

**The canonical move — compute, don't dump.** For anything touching more than a handful of records, have the agent **write a short script via Bash, run it, and reason over the result** — instead of paging through tool calls or loading raw data into context.

> **In the wild:** StockPilot's `supplier-selection` skill says it directly — *"Ranking suppliers is arithmetic, not judgment. Compute it in Python via code execution — do not reason about it in prose"* — and ships a scoring formula the agent runs over a CSV. Its `SHORT_PROMPT` adds a blanket rule: *"For any operation touching >5 SKUs, write a Python script via Bash that reads the CSVs and prints compact JSON — don't page through tool calls."* That single principle is what turns a 100-tool-call sweep into 3 script runs.

**Rules of thumb**

- Start every agent from the primitive set; make each *additional* custom tool justify itself.
- Prefer "write and run code over the data" to "load the data into context."
- You'll still need *some* custom tools — for proprietary APIs, system-of-record writes, or guardrailed side-effects. Keep them few and narrow.
- **Audit tools periodically.** "We have a tool for everything" is a smell; consolidate the ones a primitive now covers and delete them.

---

## 3. System prompt vs. skills (progressive disclosure)

The system prompt is **always-on context**: every token is paid on every turn and competes for attention. Treat it as scarce.

**The rule:** the system prompt holds only what the agent must know **regardless of the task**. Everything needed only *sometimes* becomes a **skill** that loads on demand.

- **Stays in the prompt:** role and objective, durable behavioral rules, safety constraints, the top-level operating loop.
- **Moves to a skill:** domain policies, calculation/formatting procedures, per-workflow instructions, reference data, brand/UI specs, testing/release processes.

**Why long prompts rot**

- **Context pollution** — task-irrelevant text dilutes attention and burns budget.
- **Internal contradiction** — a prompt grown by accretion eventually disagrees with itself; the model gets confused and produces confidently wrong output. This is a *context* failure, not a *model* failure.

**Anatomy of a skill (the real structure):** a directory with a `SKILL.md` whose YAML front-matter is a **name + a trigger-shaped description**, optionally bundling helper scripts:

```
forecasting/
  SKILL.md            # ---  name: forecasting
                      #      description: How to produce a demand forecast…
                      #      Load this for any task involving "forecast",
                      #      "how much will we sell", "next month", promos…  ---
  rolling_mean.py     # bundled script the agent can just run
  batch_days_of_cover.py
```

The description is written so the model knows *exactly when* to pull it in. Put the heavy detail inside the body — that's the whole point of progressive disclosure. Prefer several focused, composable skills over one mega-skill.

> **In the wild:** StockPilot moved from a **402-line system prompt to a 15-line one**, with ~400 lines redistributed into 5 trigger-scoped skills. As the repo notes, *"the knowledge didn't shrink — 402 prompt lines became 400 skill lines. The difference is when they're in context."* The promo policy that had been duplicated and self-contradictory across multiple prompt sections (causing a forecast to anchor on the wrong number) became one `forecasting` skill loaded only for forecast tasks.

---

## 4. Subagents: isolation and the communication seam

Subagents exist to **contain context** — give a long, noisy, or parallel sub-task its own window so it doesn't flood the orchestrator. Use them for that reason, not as a default way to "organize" work.

**The number-one subagent bug is the hand-off.** A subagent can do its job perfectly and the system still fails because the **orchestrator ↔ subagent contract** is wrong — the subagent returns prose where the orchestrator expects a value, omits a field, or the orchestrator misreads it.

> **In the wild — the seam failing:** StockPilot's original forecasting subagent ended its reply with *"…roughly 2,100 units over the next 30 days, **medium** confidence."* The orchestrator needed a numeric confidence to decide whether to auto-order or escalate, and the number wasn't there. The eval for that task literally checks for a numeric `confidence 0.NN` and fails on the qualitative word. The fix wasn't a smarter model — it was a **contract**: the forecaster must return `{forecast_qty, confidence, method, flags}` JSON, parsed strictly, with `confidence < 0.6` routing to human review. The skill's one-liner captures it: *"anchor the number, not the narrative."*

**Design the contract explicitly**

- Define **typed/structured I/O** for each subagent — exactly what it receives and exactly what it returns. Prefer a JSON object over free prose; **parse it strictly** and treat malformed output as an error, not something to guess around.
- Give the orchestrator the subagent's **distilled result**, not its entire transcript.
- Keep the orchestrator small: route, delegate, assemble. Don't also make it do the heavy reasoning the subagent owns.
- **Make delegation a runtime decision, not baked-in wiring.** The end-state of a good decomposition often has *zero hardcoded subagents* — the agent decides *at runtime* whether a task needs one.

**Before adding a subagent, ask:** can a skill + a primitive do this in the main context? If yes, don't add a subagent.

**Three ways to actually wire delegation (with security trade-offs)** — from the workshop's CMA helpers, generally applicable:

- **(a) A declared callable agent** — a separate deployed agent with its **own fixed system prompt** and a minimal toolset (e.g. Bash + Read only). Safest: its role can't be redefined by input.
- **(b) A generic "spawn a worker" tool** — most flexible, most exposed. If you do this, **fix the worker's system prompt server-side** so injected instructions in upstream data can't redefine its role, **sanitize the input** (strip control chars, cap length), and run it in an isolated sandbox with no egress and no write/web tools.
- **(c) Inline fallback** — if a delegation primitive isn't available, do the work inline and **lower the confidence** so downstream policy escalates to a human rather than acting on an unvalidated result.

---

## 5. Context-window hygiene (cross-cutting)

Most agent regressions trace back to context problems. Treat the window as a scarce, shared resource.

- **Don't dump raw tool output into context.** Summarize, filter, or route it. One "data-dump" tool can quietly poison every downstream decision. *(StockPilot's `list_low_stock` returned every below-reorder row, then the agent fanned out per-SKU — 100+ calls — to do what one script does.)*
- **Reason over results, not raw data** (§2): run code, return the answer.
- **Load knowledge on demand** via skills, not always-on prompt text (§3).
- **Isolate noisy work** in subagents so intermediate chatter never reaches the orchestrator (§4).
- **Watch the cheap signals:** rising **turn count, latency, and token usage** are the earliest warnings that context is bloating — and they're directly gradable (§6).

---

## 6. Eval-driven development & hill climbing

**If you can't measure it, you can't improve it.** Build the eval suite *before* you optimize, and let it gate every change.

### Design the suite across two axes

**Task types**

- **Regression tasks** — realistic **single-turn** tasks (a request → tool calls → a response you grade). These guard capabilities you already have.
- **Failure-mode tasks** — harder **multi-turn** tasks aimed at known weak spots: efficiency, hand-offs, contradictory policy, long-horizon flows.

**Grader types** — combine **deterministic** and **non-deterministic**:

| Type | Grades | Deterministic? |
|---|---|---|
| Exact / set match | a value or a set of IDs is present and correct | ✅ |
| Numeric tolerance | a quantity is within ±X% of ground truth (catches mean-anchoring, etc.) | ✅ |
| Action / side-effect | the agent *actually* wrote the PO / sent the alert (check the sink, not the prose) | ✅ |
| Efficiency / budget | **turn count, latency, token usage** under budget; ranking/format checks | ✅ |
| **LLM-as-judge** | tone, style, structure, **output quality** against a rubric | ❌ (model-graded) |

Use **partial credit for "correct but slow."** A common scoring shape: `score = (PASS + ½·PASS_SLOW) / N` — full credit for correct *and* efficient, half for correct-but-over-budget. Correctness without efficiency is still a problem.

> **In the wild:** the StockPilot suite is 12 tasks (R1–R9 regression, F1–F3 failure-mode). Deterministic graders include `action_taken` (did a PO actually land in the sink?), `wall_budget` (≤270s?), `efficiency` (turn/token budgets), and `numeric_tolerance` (whose failure message is literally *"anchored on mean?"*). One task uses an `llm_judge` with a rubric for whether the weekly report is actually usable. Each failure carries a **"why"** — that's the line you read.

### The hill-climbing loop

1. **Baseline.** Run the full suite, record the score, and **read the actual failures** — don't trust the headline number. *(In the workshop the same suite reported ~71% one run and 62% another; the score band matters more than any single run, and the failures matter more than the score.)*
2. **Triage into themes.** Use Claude Code (a capable model at high/extra-high effort) to run the evals via Bash and cluster failures into root causes — e.g. "doing work it should have a tool for," "output-structure drift," "policy confusion."
3. **Change one thing.** Apply a single architectural fix (prompt → skills; tools → primitives; fix a subagent contract).
4. **Re-run the affected tasks and compare.** Keep the change if the score climbs; revert if not.
5. **Repeat** to the bar your domain demands. (In high-cost domains a 17% failure rate is unacceptable — set the bar by the cost of being wrong.)

**Make evals reproducible.** Freeze inputs so a run today equals a run next month. *(StockPilot anchors a fixed "business date" in every prompt so forecasts don't drift with the wall clock, and isolates each run's side-effects in its own sink directory so parallel tasks never see each other's writes.)*

### Map failures → fixes (triage cheat-sheet)

| Symptom in evals | Likely root cause | Fix |
|---|---|---|
| Correct answer, too many turns / high latency | Missing primitive; model reasoning instead of running code | Add the right primitive; compute over the data via Bash |
| Subagent right, system wrong | Orchestrator↔subagent contract broken (number lost in prose) | Define structured JSON I/O; parse strictly |
| Confident but wrong values | Contradictory / bloated system prompt | Extract to skills; remove the contradiction |
| Output won't parse / inconsistent shape | Weak output-structure enforcement | Specify + validate a structured result |
| Quality / tone off | No qualitative grading | Add an LLM-as-judge grader; iterate the prompt/skill |
| Raw data flooding context | A tool dumps rows | Replace the tool with code execution over the source |

---

## 7. Run it on a managed harness (Claude Managed Agents)

Hand-rolling an agent loop on the raw Messages API is fine for building and running **locally**. It stops being fine the moment you must **host it and serve many concurrent users** — now you own infrastructure, scaling, memory, security, and sandboxing.

**Claude Managed Agents** provides the harness and managed infrastructure so you focus only on agent design. It separates the agent from the session from the **per-session isolated sandbox** where tool calls run. What you offload: scaling, secure multi-tenant isolation, the execution sandbox. What you keep: the architecture — tools, skills, subagents.

**What the config actually looks like** (the whole agent is a small dict):

```python
{
  "name":   "your-agent",
  "model":  MODEL,
  "system": SHORT_PROMPT,                       # lean; knowledge lives in skills
  "tools":  [{"type": "agent_toolset_…"}],      # Bash + file R/W + Task, built in
  "skills": [{"type": "custom", "skill_id": …, "version": "latest"}, …],
}
```

- **Primitives are built in** — you don't write code-execution or filesystem tools; you enable the toolset.
- **Side-effects go to the sandbox filesystem** (e.g. append JSONL "sinks"); the agent has no access to your machine.
- **Deploy is config push.** A typical loop: edit the agent file → `deploy` (~15–20s) → run an eval subset → open the session in the Console and **read what the agent actually did** (every tool call, every Bash command, every skill loaded). The CLI tells you PASS/FAIL; the Console tells you *why*.
- **Migrate when** you need managed scale, security, or sandboxing — or simply want to stop maintaining harness plumbing.
- **Data access in production is typically MCP**; uploaded files/CSVs are a fine simplification for prototypes and workshops.

---

## 8. Build & rescue checklists

**Starting a new agent**

- [ ] Write a one-line objective + the *always-true* rules → that's your (short) system prompt.
- [ ] Give it the human-like primitives (code, filesystem, to-do, web); justify any custom tool.
- [ ] Put "sometimes" knowledge in skills with sharp, trigger-shaped descriptions.
- [ ] Add a subagent only where context must truly be isolated; define its structured I/O contract.
- [ ] Write an eval suite (regression + failure-mode; deterministic + LLM-judge) **before** optimizing. Freeze inputs for reproducibility.

**Rescuing an agent that "outgrew its prompt"**

- [ ] Run the suite, record a baseline band, and **read every failure's "why."**
- [ ] Triage failures into themes with Claude Code (high effort).
- [ ] Move the prompt's "sometimes" content into skills (expect a dramatic shrink).
- [ ] Replace data-dump tools with code execution over the source; delete now-redundant tools.
- [ ] Replace thin-wrapper subagents with either a real isolated subagent (structured contract) or plain code; make delegation a runtime decision.
- [ ] Re-run the affected tasks, keep what climbs, repeat. Move to a managed harness when you need scale.

---

## 9. Anti-patterns to avoid

- **The 400-line system prompt.** Accretion without extraction → contradiction and context pollution.
- **A tool for everything.** Decision paralysis and raw-output dumping; prefer primitives.
- **Raw context dumping.** Feeding whole files or unfiltered tool output into the window.
- **Decorative subagents.** Thin wrappers that add round-trips and a fragile seam for no isolation benefit.
- **Prose where a value belongs.** A subagent (or tool) that says "medium confidence" when the caller needs `0.62`.
- **Trusting one eval number.** Headline scores hide expensive failures; read them, and judge against a band.
- **Optimizing by vibes.** Changing many things at once with no baseline to attribute improvement.
- **Non-reproducible evals.** Inputs that drift with the wall clock or share mutable state across runs.
- **Hand-rolling harness plumbing** you could offload to a managed runtime.

---

## 10. One-line reminders

- *Action ⇒ tool. Knowledge ⇒ skill. Isolation ⇒ subagent.*
- *The system prompt is for what's true every time; everything else is a skill.*
- *Give the model a computer, not a thousand tools.*
- *Reason over results, not raw data.*
- *If a tool returns >2k tokens, it should be code execution.*
- *Anchor the number, not the narrative — give subagents a strict contract.*
- *If it doesn't need its own context, it isn't a subagent.*
- *Correct-but-slow is still a failure.*
- *Baseline → one change → re-run. That's the whole game.*
- *Read the "why," not just the score.*

---

*Principles synthesized from Anthropic's Applied AI guidance and grounded in the "agent-decomposition" workshop (Code with Claude 2026, W5). The StockPilot code is used as illustration of the before-state and the fixes, not as a template to copy. Beta/feature names (the CMA toolset, callable agents) reflect the workshop snapshot and may change — check current Anthropic docs.*

# sentrux — CLI Reference, `rules.toml` Schema, and Authoring Guide

## 1. CLI Reference

### Invocation

```
sentrux check .
```

`sentrux check` must be run from, or given the path to, a directory that contains a `.sentrux/rules.toml` file.

```
sentrux check /path/to/project
```

### Exit Codes

| Code | Meaning |
|------|---------|
| `0`  | All rules pass — no violations found |
| `1`  | Rule violations found, `.sentrux/rules.toml` is missing, or the scan itself failed |

### Output Format

When all rules pass, `sentrux check` prints exactly:

```
sentrux check — {N} rules checked

Quality: {score}

✓ All rules pass
```

- `{N}` is the total number of rules evaluated.
- `{score}` is an integer on the **0–10000 scale** (the geometric mean of the five root-cause dimension scores, multiplied by 10000).

Example output for a project with 12 rules and a quality score of 7842:

```
sentrux check — 12 rules checked

Quality: 7842

✓ All rules pass
```

### Missing `rules.toml` Behaviour

devloop treats a missing `.sentrux/rules.toml` as a **scan abort**, not a quality failure. When the file is absent, devloop posts an error comment on the parent tracking issue and exits with code `1`. No quality score is computed and no violations are reported.

---

## 2. Score Scale and Root-Cause Dimensions

### Score Scale

The quality score is expressed on a **0–10000 integer scale**:

- **10000** — perfect score across all dimensions.
- **7000–8500** — typical range for real-world, well-structured projects.
- Below **7000** — structural concerns worth addressing; consider tightening thresholds.

The score is the geometric mean of the five dimension scores, multiplied by 10000. Internally, each dimension produces a value on the 0.0–1.0 scale; the display score is simply that value × 10000.

### Five Root-Cause Dimensions

| Dimension | What it measures |
|-----------|-----------------|
| **modularity** | How well the codebase is partitioned into cohesive, loosely-coupled modules. High cohesion within modules and low coupling between them raises this score. |
| **acyclicity** | Absence of dependency cycles. A fully acyclic dependency graph scores 1.0 (10000); each cycle reduces the score. |
| **depth** | Appropriate nesting and call-stack depth. Excessively deep call chains and highly nested structures lower this score. |
| **equality** | Balance of size and responsibility across modules. Highly skewed file/module sizes (god files, empty stubs) reduce this score. |
| **redundancy** | Absence of duplicate logic, copy-pasted blocks, and near-identical structures. Lower duplication yields a higher score. |

---

## 3. `.sentrux/rules.toml` Schema

The configuration file lives at `.sentrux/rules.toml` in the project root.

### Constraint Thresholds

These fields use the **internal 0.0–1.0 scale**. The mapping to the display score is direct: `min_quality = 0.70` corresponds to display score **7000**.

| Field | Type | Description |
|-------|------|-------------|
| `min_quality` | float | Minimum acceptable overall quality score (0.0–1.0). Failing this threshold fails the entire check. |
| `min_modularity` | float | Minimum acceptable modularity dimension score. |
| `min_acyclicity` | float | Minimum acceptable acyclicity dimension score. |
| `min_depth` | float | Minimum acceptable depth dimension score. |
| `min_equality` | float | Minimum acceptable equality dimension score. |
| `min_redundancy` | float | Minimum acceptable redundancy dimension score. |

> **Scale note**: All `min_*` threshold values are on the internal 0.0–1.0 scale. The display score shown in CLI output is these values × 10000. For example, `min_quality = 0.70` will fail if the displayed score drops below 7000.

### Hard Engineering Limits

| Field | Type | Description |
|-------|------|-------------|
| `max_coupling_score` | float | Maximum allowed inter-module coupling score. Exceeding this value is a hard violation regardless of the overall quality score. |
| `max_cycles` | int | Maximum number of dependency cycles permitted across the entire project. Set to `0` to require a fully acyclic graph. |
| `max_cc` | int | Maximum cyclomatic complexity for any single function or method. |
| `max_file_lines` | int | Maximum number of lines allowed in any single source file. |
| `max_fn_lines` | int | Maximum number of lines allowed in any single function or method body. |
| `no_god_files` | bool | When `true`, any file that is disproportionately large or highly coupled is flagged as a violation. |
| `max_upward_violations` | int | Maximum number of allowed upward-layer dependency violations (calls from a lower architectural layer up to a higher one). |

### Structural Definitions

#### `[[layers]]`

Defines the architectural layers of the project. Each entry describes one layer.

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Human-readable name for the layer (e.g. `"presentation"`, `"domain"`, `"infrastructure"`). |
| `paths` | list of glob strings | yes | Glob patterns that identify which files or directories belong to this layer. |
| `order` | int | no | Optional integer that establishes the canonical dependency direction. Lower numbers are "higher" layers that may depend on higher-numbered layers but not vice versa. |

#### `[[boundaries]]`

Declares explicit cross-layer boundaries that must not be crossed.

| Field | Type | Description |
|-------|------|-------------|
| `from` | glob string | The source side of the forbidden dependency. |
| `to` | glob string | The target side of the forbidden dependency. |
| `reason` | string | Human-readable explanation of why this boundary exists, surfaced in violation output. |

#### `[language.<name>.constraints]`

Per-language overrides for any of the hard engineering limit fields. `<name>` is the lowercase language identifier (e.g. `python`, `go`, `typescript`). Any field valid at the top level can be overridden here for a specific language.

---

### Annotated Example `.sentrux/rules.toml`

```toml
# .sentrux/rules.toml
# Project quality gates enforced by sentrux.
# All min_* values are on the internal 0.0–1.0 scale.
# Display score = internal value × 10000.

# ---------------------------------------------------------------------------
# Overall quality gate
# A score below 0.70 (display: 7000) indicates systemic structural problems.
min_quality     = 0.70

# Dimension-level gates — useful for catching regressions in specific areas
# without necessarily failing the overall score yet.
min_modularity  = 0.65   # Allow slightly looser coupling while the team refactors
min_acyclicity  = 0.90   # Cycles are serious; keep this high
min_depth       = 0.60   # Deeper call chains are acceptable in some domains
min_equality    = 0.65   # Some size imbalance is tolerable
min_redundancy  = 0.70   # Duplicate logic should be caught early

# ---------------------------------------------------------------------------
# Hard engineering limits — these trigger violations regardless of score.

# No single module should become a hub for all cross-module dependencies.
max_coupling_score = 0.40

# Zero cycles required — any cycle is a hard failure.
max_cycles = 0

# Cyclomatic complexity above 15 makes functions hard to test and reason about.
max_cc = 15

# Files longer than 500 lines become hard to navigate and review.
max_file_lines = 500

# Functions longer than 60 lines should be broken up.
max_fn_lines = 60

# Flag any file that dominates the coupling graph.
no_god_files = true

# Allow at most 2 upward-layer violations during an active migration;
# set to 0 once the migration is complete.
max_upward_violations = 2

# ---------------------------------------------------------------------------
# Architectural layers — establish the allowed dependency direction.
# Lower `order` values depend on higher `order` values, not vice versa.

[[layers]]
name  = "presentation"
paths = ["src/api/**", "src/handlers/**", "src/routes/**"]
order = 1   # Top of the stack; calls into domain

[[layers]]
name  = "domain"
paths = ["src/domain/**", "src/services/**", "src/usecases/**"]
order = 2   # Business logic; calls into infrastructure

[[layers]]
name  = "infrastructure"
paths = ["src/db/**", "src/clients/**", "src/adapters/**"]
order = 3   # I/O and external integrations; no upward calls

# ---------------------------------------------------------------------------
# Explicit boundaries — dependencies that must never exist.

[[boundaries]]
from   = "src/domain/**"
to     = "src/api/**"
reason = "Domain logic must not import presentation-layer symbols; keep business rules UI-agnostic."

[[boundaries]]
from   = "src/db/**"
to     = "src/services/**"
reason = "Database adapters must not call into service layer; dependency must flow outward only."

# ---------------------------------------------------------------------------
# Per-language overrides — tighten or relax limits for specific languages.

[language.python.constraints]
# Python tends toward larger files; relax the file-line limit slightly.
max_file_lines = 600
max_cc         = 12   # Stricter for Python where complexity hides easily

[language.typescript.constraints]
# TypeScript frontends often have larger components; allow a bit more.
max_file_lines = 700
max_fn_lines   = 80
```

---

## 4. LLM Prompt for Drafting a `rules.toml`

Copy and paste the following prompt into your LLM session to generate a project-specific `.sentrux/rules.toml`.

---

```
You are a software architecture assistant. I need you to draft a .sentrux/rules.toml
file for my project. Follow these steps in order and show your reasoning briefly
before outputting the file.

Step 1 — Survey the directory structure.
List the top-level directories and any obvious sub-packages. Identify the primary
programming language(s) in use (check file extensions, build files such as
pyproject.toml, package.json, go.mod, Cargo.toml, pom.xml, etc.).

Step 2 — Identify architectural layers.
Based on directory names and typical conventions for the detected language(s), name
two to four natural architectural layers (e.g. presentation / domain / infrastructure,
or cmd / internal / pkg / vendor for Go projects). For each layer, write the glob
patterns that match its files or directories.

Step 3 — Identify cross-layer boundaries to enforce.
List any obvious or suspected dependency rules that must never be violated (e.g.
"domain must not import from the HTTP handler layer"). For each boundary, write a
brief reason string that will appear in violation output.

Step 4 — Choose conservative thresholds.
Set initial values for the following fields using the internal 0.0–1.0 scale
(display score = value × 10000):
  - min_quality: start at 0.70 (display 7000) unless the project is clearly
    well-structured, in which case use 0.75.
  - max_cycles: set to 0 (require fully acyclic graph) for new or greenfield
    projects; set to a small positive integer only if cycles already exist and
    cannot be resolved immediately.
  - max_cc: use 15 for Python/Ruby/JavaScript/TypeScript; use 10 for Go/Java/C#;
    use 20 for legacy codebases that are known to have complex methods.
  - no_god_files: set to true unless the project has a known, intentional
    monolithic entry point.
  Add per-language overrides under [language.<name>.constraints] wherever a
  language-specific relaxation or tightening makes sense.

Step 5 — Output the complete .sentrux/rules.toml.
Produce the full TOML file with an inline comment on every constraint explaining
the rationale in one sentence. Do not omit any field you set. Group fields with
blank lines and a comment header for: thresholds, hard limits, layers, boundaries,
and per-language overrides.

Step 6 — Placement reminder.
After the file, add a one-line note: "Save this file as .sentrux/rules.toml at the
project root (the same directory that contains your primary build or manifest file)."
```

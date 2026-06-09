# deid — SDC verbs → microdata.no

Translates statistical-disclosure-control verbs (a small `protect`-inspired call
DSL) into valid [microdata.no](https://microdata.no) script. deid is its own SDC
dialect tuned for microdata, inspired by the
[`protect`](https://github.com/hmelberg/protect) package. Pure Python standard
library — no runtime dependencies.

```python
from deid import transform
res = transform("round(income, to=1000)\nshorten(icd, keep=3)")
print(res.script())     # microdata lines, each prefixed with a // audit comment
print(res.warnings)     # honest list of anything dropped, approximated, or pending
```

Install (editable, for development) and run the tests:

```bash
pip install -e .
pytest tests/        # 86 tests
```

**Try it in a browser:** open `index.html` — a single self-contained page
(no server, no dependencies) that translates the DSL → microdata live, with
worked examples. It is a JavaScript port of `commands.py`; the **Python is the
source of truth** — keep them in sync when adding or changing verbs.

## Verbs (16)

| verb | what it does |
|---|---|
| `round(x, to=N)` | round to nearest multiple of N (numeric) |
| `round_date(d, to="year")` | truncate a date to a period (year/month/quarter/halfyear/week/day/dow/doy) |
| `bin(x, …)` | bin codes: `bins=` (quantile), `width=`/`start=`/`stop=`/`edges=` (recode bands), `labels=True` |
| `recode(x, rules)` | remap by the variable's **own** value — microdata-string `"(1/7 = 0)"` or dict `{1: 0}`; `labels=` |
| `change(x, value, where=…)` | set value (None=missing) where a condition holds (esp. on **other** variables) |
| `reduce_categories(x, min=N, to="Other")` | merge **rare** categories (min int=count, 0–1=proportion, to=None→missing) |
| `function(x, func, *args)` | apply any microdata function — `function(lonn, round, 1000)` (escape hatch) |
| `shorten(code, keep=3)` | keep the first N characters |
| `eliminate(where= \| col, min=)` | drop ROWS: conditional, or rare records |
| `microaggregate(x, by=…)` | replace with a group statistic (`stat="mean"`) |
| `winsorize(x, method="iqr", …)` | cap extremes with data-driven bounds (iqr/gaussian/mad/percentile) |
| `diff(x, ref=…)` | value − reference (centering / date-interval); see below |
| `diff_date(x, …)` | parse a `YYYY-MM-DD` string to a date-value, then `diff` |
| `noise(x, …)` / `jitter(x, …)` / `draw(z, …)` | seeded noise / deterministic hash-noise / a reusable random column |
| `risk(quasi_ids=[…])` | k-anonymity measurement |

Cross-cutting: **multi-var** (list as 1st arg → per column), **optional prefix**
(`round` / `apply_round` / `apply-round`), **`into="new"`** (new var vs in-place),
**session seed** (`transform(src, seed=N)`). Unsupported (explicit warning):
`swap`, `pseudonymize`, `insert`, `suppress`. Pending: `profile`.

## The two hard limits of microdata that bound everything

1. **No value-level RNG.** Every probability function is a deterministic
   CDF/PDF/inverse; there is no `rnormal`/`runiform`. `sample fraction seed`
   randomizes *which rows*, never *a value*. → additive noise is impossible.
2. **Pseudonym IDs (`_FNR`) are write-protected** — usable only as `collapse(by)`
   / `merge(on)` keys (microdata commands), never in `generate`/`replace`.

## Notes by area

### Near-direct value transforms
| verb | microdata |
|---|---|
| `round(x, to=50)` | `replace x = round(x, 50)` |
| `round_date(d, to="year")` | `replace d = year(d)` (or month/quarter/…) |
| `bin(x, bins=10)` | `replace x = quantile(x, 10)` (quantile method) |
| `shorten(code, keep=3)` | `replace code = substr(code, 1, 3)` |
| `function(x, round, 1000)` | `replace x = round(x, 1000)` (any function) |

`into="newvar"` switches any value transform from in-place `replace` to a new
`generate`.

### Conditional set / drop (the 2×2)
`change` and `eliminate` are a parallel pair (set a value vs drop the row), over
the same two condition kinds:

| | set value — `change` | drop row — `eliminate` |
|---|---|---|
| **frequency** | `reduce_categories(d, min=5)` | `eliminate(d, min=5)` |
| **condition** | `change(d, "X", where="alder>90")` | `eliminate(where="alder>90")` |

`min` int = count, `0<min<1` = proportion of total. Cell-masking to missing =
`change(col, None, where=…)`. Random row-drop = microdata's own `sample`.

**Session seed.** `transform(src, seed=N)` sets a base seed; each seed-less random
op (noise/draw/diff-random) gets a distinct block (`N + k·1000`), so independent
draws don't accidentally correlate. Pass `seed=` on a call to pin it.

### Noise — implemented, but read this first
microdata has no value-level RNG, so noise is faked two different ways. **Know
which you are using — they are not equally safe.**

| verb | how | strength |
|---|---|---|
| `jitter(x, scale=50[, dist='normal'][, key=[...]])` | deterministic `fract(sin(k))` hash (Box–Muller for `dist='normal'`) added in place | **display-only.** The noise is a pure function of the key columns + seed, so anyone who knows the recipe and inputs can recompute and *subtract it back out*. Use it to break exact ties / fuzz precision, never as anonymization. By default `key` is the target column itself, which is reversible **and** correlated — pass `key=[other,cols]` to decorrelate. |
| `noise(x[, unit_id=…][, scale=…][, pct=…][, source=…])` | genuine seeded randomness applied to `x`, where `z` is a per-unit draw (see the random primitive below) | **real**, seed-controlled, not reconstructable. |

**Self-scaling by default.** With no `scale`, noise is **relative**: `x * (1 +
0.05·z)` (±5 %) — so `noise(inntekt)` works without knowing the mean/SD (uses
`unit_id` from the session if set). Pass `scale=` for absolute additive noise
(`x + scale·z`), or `pct=` for a different percentage. NB: relative leaves exact
zeros unchanged. `using=` reuse stays additive (for date offsets) unless `pct=` is
given.

**Why noise defaults to `dist='uniform_sym'`.** A `binomial`/`normal` draw has its
mode at `z=0`, so a fraction ≈ √(2/πk) of units (e.g. ~23% at `rounds=12`) get
**zero** noise — their value stays exactly original. The default centered-uniform
(`uniform_sym ∈ [-1,1)`) has no mass point at 0, so virtually nothing is left
unchanged; there `scale` is the half-range. With `dist='normal'`, `scale` is the
SD but expect the 0-spike (raise `rounds`, or use `source='hybrid'`).

**`source=`** picks the entropy:
| source | how | trade-off |
|---|---|---|
| `sample` (default) | seeded `sample`+`merge` per unit | truly random, per-unit; costs `rounds` merges; needs `unit_id` |
| `digits` | `from_col − floor(from_col/mod)*mod` (low digits) | one line, free, changes ~every row — but **reversible** and only as uniform as the source digits (avoid round-number columns) |
| `hybrid` | sample draw + `digit_weight`·digit term | sample randomness, with digits filling `normal`'s 0-spike |

Caveats unchanged: microdata already noises *output* (Tiltak 3), so input noise is
often redundant; the `clone-dataset`/`merge` flow is **unverified** (VERIFY.md).

### Winsorize — implemented
`winsorize(x[, method=…][, limits=…][, by=…])` builds bounds with `aggregate`
(written back as columns), then clips with `replace … if`. One-sided bounds
(`None`) and `by=` grouping are supported.

| method | maps to | note |
|---|---|---|
| `iqr` (**default**, limits (1.5,1.5)) | `aggregate (p25)(p75)` → `q3-q1` fences | full support |
| `gaussian` (default 3·sd) | `aggregate (mean)(sd)` → `mean ± k·sd` | full support |
| `mad` (default 3·mad) | two-pass `aggregate (median)` of `abs(x-median)` | full support |
| `percentile` (default 0.25/0.75) | `aggregate (p25/median/p75)` | **only 0.25/0.5/0.75** — arbitrary percentiles refused with a warning pointing at `iqr` |

Default method is `iqr`, limits default per method, so a bare `winsorize(col)` is
valid microdata. `winsorize` does only **data-driven** bounds; for manual exact
top/bottom-coding use `change(x, bound, where="x > bound")`.

### Diff — implemented (a general "value − reference" / centering op)
`diff` is **not date-specific**. It computes `col - reference(col)` for any
numeric variable (income, age, dates…) via `aggregate` + subtraction. The
reference is chosen by `ref`:

| `ref=` | scope | how / use |
|---|---|---|
| `random_global` | **default** | hidden global shift: a population stat + secret `let` offset — one unknown anchor for all (works without `unit_id`) |
| `mean` `median` `min` `max` `p25` `p75` | global | `aggregate (stat) col -> a` → subtract — **mean-centering / de-meaning** |
| `<stat>_per_unit` (e.g. `mean_per_unit`) | per-unit | `aggregate (stat) col -> a, by(unit_id)` → subtract — **within-person demeaning** (fixed-effects style) |
| `first_per_unit` | per-unit | alias for `min_per_unit` (protect compatibility) |
| `random_per_unit` | per-unit | tilfeldig anchor per unit — the secret key below |
| `'YYYY-MM-DD'` | — | fixed date literal: `col - date(y,m,d)` |
| another column | — | pairwise: `col - other` |

`unit='years'|'months'` divides the result (date-only). Per-unit refs require
`unit_id`. **deid's default ref (`random_global`) differs from protect's
(`first_per_unit`)** so a bare `diff(x)` is useful without `unit_id`.

**`diff_date` — for ISO-string date variables.** microdata's diffable date is the
integer date-value (days since 1970); `year()`/`date()` operate on that, and
`isoformatdate()` produces the `YYYY-MM-DD` *string*. If a variable is stored as
that string, it must be parsed before subtraction. `diff_date(col, …)` prepends
`replace col = date(to_int(substr(col,1,4)), to_int(substr(col,6,2)),
to_int(substr(col,9,2)))` and then delegates to `diff` (same args). Use `diff`
when the column is already a date-value, `diff_date` when it's an ISO string.

**`random_global` — a fixed but hidden anchor.** `generate one=1` →
`aggregate (stat) col -> med, by(one)` → `let shift = <secret>` →
`replace col = col - (med + shift)`. One anchor for all rows; the secret `shift`
makes it unpredictable. Weaker than per-unit (between-row differences survive),
but hides absolute level/timing while preserving all relative differences. Note:
sampling a *subset* and taking its min/max/median is **not** cleanly expressible
(microdata can't broadcast a subset statistic back to all rows — no `aggregate …
if`, no stored results), so the secrecy comes from `shift`, not from sampling.

**The per-unit secret key (`random_per_unit`).** A random anchor per person needs
per-unit randomness keyed to the unit id — but `_FNR` ids are write-protected, so
you *cannot* hash them in `generate`. The **only** path is `sample` + `merge`:
`clone-units` (one row per unit) → `sample 0.5 seed` (picks whole units) → merge
a flag back (same bit on every row of a unit). `bits` independent draws form a
binary fraction `u ∈ [0,1)`; `anchor = min + floor(u * span)`. The seeds are the
key. Shares the unverified multi-dataset flow with `noise` (open Q3).

### Categorical re-labelling (recode / reduce_categories / change)
Three non-overlapping verbs (the old `collapse` was removed — it duplicated all
three):

- **`recode(col, rules)`** — remap by the variable's **own value**. `rules` is a
  microdata recode string (`"(1/7 = 0) (* = 9)"`) or a dict (`{1: 0, 2: 0}`).
  `labels={code: text}` adds `define-labels` / `assign-labels`.
- **`reduce_categories(col, min=N, to="Other")`** — merge **rare** categories:
  `aggregate (count) by(col)` → `replace col = to if cnt < N` (int N = count,
  `0<N<1` = fraction of total; `to=None` → missing).
- **`change(col, value, where="cond")`** — set `value` (None=missing) where a
  condition holds, **including conditions on other variables**
  (`change(diagnose, "X", where="alder > 90")`) — which `recode` cannot express.

`keep_top` was never expressible (needs ranking levels by frequency).

### Risk — implemented (k-anonymity measurement)
`risk(quasi_ids=[…], sensitive=[…], unit_id=…)` is non-destructive: it builds a
composite quasi-ID key (`string(a) ++ "_" ++ string(b) …`), counts the
equivalence-class size per row, and `summarize`s it — **min = k-anonymity** —
plus shares of rows in cells <5 and uniquely-identifiable rows. Limitations
(warned): per-unit projection needs count-distinct (unavailable → row-level
cells); l-diversity needs entropy (not expressible).

### Tier B — feasible, deferred
| verb | pattern |
|---|---|
| `profile` | ordered macro expansion of its constituent verbs |

### Random primitive — `draw` and `_random_series`
microdata has no RNG, but each seeded `sample` round is a **Bernoulli(p)** bit;
weighting and summing repeated rounds builds whole families of distributions.
This is factored into one helper (`_random_series`) that `noise` and
`diff(random_*)` both reuse, and exposed as a verb:

```
draw(z, dist="normal", rounds=12, unit_id="PERSONID_1", seed=7)   # builds column z
```

| `dist` | bit weights | result |
|---|---|---|
| `bernoulli` | one bit | 0/1 with prob `p` |
| `binomial` | all 1 | integer 0…rounds (≈ normal, CLT) |
| `normal` | all 1, then standardise | ≈ N(0,1) |
| `uniform_int` | 2⁰,2¹,… | uniform integer in [0, 2^rounds) |
| `uniform` | 2⁻¹,2⁻²,… | binary fraction in [0,1) |

Construction: `clone-units` yields an **empty** dataset (no variables) you can't
`sample`/`generate` on — so we `clone-dataset` the source once, `keep` just the
merge key (a small reusable copy), then per round `clone-dataset` that small copy
(sample is destructive), `sample p seed` → flag → `merge … on key` →
`recode bit (missing = 0)`. Assumes **one row per unit** in the source (typical
for a population dataset); panel data must be collapsed to unit level first.
`level='row'` needs a row-unique merge key. This whole flow is **unverified** —
`VERIFY.md` Blokk 3 tests it.

**Reuse a draw with `using=`.** `draw` keeps the named column, and `noise` accepts
`using=<that column>` to add it without rebuilding — apply one draw to several
variables (correlated noise) or shift several date columns by the same per-unit
offset (preserving intervals):
```
draw(z, dist="normal", rounds=12, unit_id="PERSONID_1", seed=7)
noise(inntekt, scale=50, using=z)      # replace inntekt = inntekt + 50*z
noise(formue,  scale=30, using=z)      # same z → correlated; no rebuild
noise(visit,             using=skift)  # scale defaults to 1 (raw offset shift)
```
Caveats: assumes independent draws across seeds; costs `rounds` × clone/merge;
the multi-dataset flow and `recode (missing=0)` token are unverified (open Q2/Q3).

### Tier C — not expressible (emit `// UNSUPPORTED` + warning, never silent)
`swap` (no shuffle/sort-pairing) · `pseudonymize` (IDs write-protected, already
pseudonymized at source) · `insert` (no row-append) · `suppress` (platform
applies output SDC automatically — the 10 *Tiltak*).

## Open questions to verify against live microdata (block Tier B)
1. Does `aggregate` accept an **empty `by()`** (population-level), or must we
   synthesize a constant key (`generate protect2micro_one = 1`)?
2. ~~Exact **`recode` interval-rule syntax**~~ — **confirmed:** `recode v1 v2
   (1/7 = 0) (nonmissing = 1) (missing = 99 "vet ikke" missing)`. So ranges
   `(a/b = x)`, `nonmissing`, and `missing = x` all work (deid uses `(missing = 0)`).
3. Can `sample`'s seeded selection become an **in-place flag** (perturb a share,
   keep the rest)? If not, `share` only works for pure-selection verbs.
4. Are full-date variables stored as **date-values or ISO strings**, and can
   `replace` change a string column's type to a date-value in place (what
   `diff_date` assumes), or must the parse go into a **new** variable?
5. Does `recode` accept **`min`/`max`** open intervals, and do `define-labels` /
   `assign-labels` work as assumed? (Used by `bin(start=, stop=, width=,
   labels=True)` for open-ended labelled bands — VERIFY.md Blokk 6.)

`VERIFY.md` holds a block-by-block script to settle these against live microdata.

## Architecture
- `__init__.py` — `transform()` → `TranslationResult{.script(), .warnings}`;
  `ast`-based parse of `verb(column, **kwargs)` statements + dispatch.
- `commands.py` — `Ctx`, the `REGISTRY` of Tier-A emitters, and the
  `TIER_B_PENDING` / `TIER_C_REASONS` tables.
- `tests/test_deid.py` — `pytest tests/`.

**Temp-name rule:** microdata forbids variable (and dataset) names that start
with `_`. All generated temporaries go through `Ctx.tmp()`, which prefixes
`protect2micro_` (e.g. `protect2micro_q1`, `protect2micro_anchor1`) — a valid, collision-unlikely namespace.
Never hand-write a leading-underscore name in an emitter.

## Next steps
1. Run `VERIFY.md` against live microdata to settle the open questions, then drop
   the "unverified" caveats (and switch globals to a constant key if empty `by()`
   fails).
2. Implement `profile` (named compositions — the only remaining verb).
3. Wire `deid` into the editor as a 4th source mode beside Python/R.

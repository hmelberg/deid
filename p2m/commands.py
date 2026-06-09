"""
Verb emitters for p2m (protect-DSL → microdata.no).

Each Tier-A emitter has the signature:

    emit_<verb>(col: str, kwargs: dict, ctx: Ctx) -> list[str]

and returns the microdata script lines for that verb (the dispatcher in
__init__.py prepends the ``// source`` comment). Emitters may attach
warnings via ``ctx.warn(...)``.

Design notes
------------
- microdata mutates in place via ``replace``; pass ``into="newvar"`` to emit a
  ``generate`` of a new variable instead.
- microdata has NO value-level RNG, so the universal protect args ``share`` and
  ``random_state`` cannot perturb a *value*. When present on a value-changing
  verb we warn rather than silently ignore.
"""
import re
from dataclasses import dataclass, field

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UNIT_DIV = {"days": None, "months": 30.44, "years": 365.25}
# Reference statistics diff can subtract — global (`<stat>`) or per-unit
# (`<stat>_per_unit`). diff is a general "value minus reference" / centering op,
# not date-specific: works on income etc. too.
_DIFF_STATS = ("min", "max", "mean", "median", "p25", "p75")


@dataclass
class Ctx:
    """Shared translation state."""
    warnings: list = field(default_factory=list)
    dataset: str = "main"      # active microdata dataset (merge target for noise)
    unit_id: str = None        # default unit key for merge-back draws
    seed: int = 0              # base seed; each random op gets a distinct block
    _tmp: int = 0
    _seed_n: int = 0

    def warn(self, msg: str):
        self.warnings.append(msg)

    def tmp(self, stem: str) -> str:
        """Allocate a unique temp name. Must NOT start with '_' — microdata
        forbids leading-underscore variable (and dataset) names."""
        self._tmp += 1
        return f"protect2micro_{stem}{self._tmp}"

    def next_seed(self) -> int:
        """A fresh seed block (base + n·1000) so independent random ops don't
        collide — a single shared seed would give correlated draws. rounds < 1000
        so blocks never overlap."""
        s = self.seed + self._seed_n * 1000
        self._seed_n += 1
        return s


# ── helpers ───────────────────────────────────────────────────────────────────

def _target(col: str, kwargs: dict):
    """Return (verb_keyword, target_var). ``into=`` ⇒ generate a new var."""
    dest = kwargs.get("into")
    if dest:
        return "generate", dest
    return "replace", col


def _warn_value_rng(verb: str, kwargs: dict, ctx: Ctx):
    """Warn that share/random_state can't drive a value transform in microdata."""
    for arg in ("share", "random_state"):
        if arg in kwargs:
            ctx.warn(
                f"{verb}: '{arg}' ignored — microdata cannot apply a partial/"
                f"seeded *value* transform (no value-level RNG). The whole "
                f"column is transformed.")


# ── Tier A emitters ─────────────────────────────────────────────────────────────

_DATE_PERIODS = {"year", "month", "quarter", "halfyear", "week", "day", "dow", "doy"}


def emit_round(col, kwargs, ctx):
    """round(x, to=N) → round to nearest multiple of N (numeric).
    For dates use round_date. (This is protect's `coarsen`, renamed/numeric-only.)"""
    _warn_value_rng("round", kwargs, ctx)
    kw, tgt = _target(col, kwargs)
    to = kwargs.get("to")
    if to is None:
        ctx.warn("round: 'to' required (the multiple to round to, e.g. to=1000).")
        return ["// round: missing 'to'"]
    if isinstance(to, str):
        ctx.warn("round: 'to' must be numeric; for dates use round_date(col, to='year').")
        return ["// round: 'to' must be numeric (use round_date for dates)"]
    return [f"{kw} {tgt} = round({col}, {to})"]


def emit_round_date(col, kwargs, ctx):
    """round_date(d, to='year') → truncate a date to a period
    (year/month/quarter/halfyear/week/day/dow/doy)."""
    kw, tgt = _target(col, kwargs)
    to = kwargs.get("to", "year")
    if to not in _DATE_PERIODS:
        ctx.warn(f"round_date: to={to!r} is not a period; using 'year'. "
                 f"Options: {', '.join(sorted(_DATE_PERIODS))}.")
        to = "year"
    return [f"{kw} {tgt} = {to}({col})"]


def _emit_labels(ctx, var, labels):
    """define-labels + assign-labels so binned output reads '10-14' not '1'."""
    name = ctx.tmp("lab")
    pairs = " ".join(f'{_num(k)} "{v}"' for k, v in labels.items())
    return [f"define-labels {name} {pairs}", f"assign-labels {var} {name}"]


def _cuts_recode(col, kwargs, ctx, cuts):
    """Integer bands from cut points → static `recode`, with open ends."""
    tgt = kwargs.get("into") or col
    n = len(cuts)
    rules = [f"(min/{_num(cuts[0] - 1)} = 0)"]
    labels = {0: f"<{_num(cuts[0])}"}
    for i in range(n - 1):
        rules.append(f"({_num(cuts[i])}/{_num(cuts[i + 1] - 1)} = {i + 1})")
        labels[i + 1] = f"{_num(cuts[i])}-{_num(cuts[i + 1] - 1)}"
    rules.append(f"({_num(cuts[-1])}/max = {n})")
    labels[n] = f"{_num(cuts[-1])}+"
    lines = []
    if kwargs.get("into"):
        lines.append(f"clone-variables {col} -> {tgt}")
    lines.append(f"recode {tgt} " + " ".join(rules))
    want = kwargs.get("labels")
    if want is True:
        lines += _emit_labels(ctx, tgt, labels)
    elif isinstance(want, dict):
        lines += _emit_labels(ctx, tgt, want)
    return lines


def emit_bin(col, kwargs, ctx):
    """bin(col, …) → bin codes. Ways to specify (first match wins):

      edges=[10, 18, 67]            explicit cut points → static recode, integer
      start=, stop=, width=        cut points = start..stop by width → recode
      width= (no bounds)           aggregate min → floor((x-min)/width)
      method='equal_width', bins=  aggregate min/max → floor, N equal bins
      bins=10 (default)            equal-frequency via quantile()

    For the recode forms: integer bands (assumes integer values; for continuous
    use recode()). `labels=True` adds readable '10-14'/'<10'/'80+' labels.
    """
    _warn_value_rng("bin", kwargs, ctx)
    edges = kwargs.get("edges")
    start, stop, width = kwargs.get("start"), kwargs.get("stop"), kwargs.get("width")

    cuts = None
    if edges is not None:
        cuts = list(edges)
    elif None not in (start, stop, width):
        cuts, c = [], start
        while c < stop:
            cuts.append(c)
            c += width
        cuts.append(stop)
    if cuts is not None:
        if len(cuts) < 2:
            ctx.warn("bin: need at least two cut points.")
            return ["// bin: need at least two cut points"]
        return _cuts_recode(col, kwargs, ctx, cuts)

    kw, tgt = _target(col, kwargs)
    if width is not None:                       # fixed width, auto range
        lo = ctx.tmp("lo")
        return [_agg("min", col, lo, None),
                f"{kw} {tgt} = floor(({col} - {lo}) / {width})", f"drop {lo}"]

    method = kwargs.get("method", "quantile")
    bins = kwargs.get("bins", 10)
    if method in ("equal_width", "width"):
        lo, hi, w = ctx.tmp("lo"), ctx.tmp("hi"), ctx.tmp("w")
        return [_agg("min", col, lo, None), _agg("max", col, hi, None),
                f"generate {w} = ({hi} - {lo}) / {bins}",
                f"{kw} {tgt} = floor(({col} - {lo}) / {w})", f"drop {lo} {hi} {w}"]
    if method != "quantile":
        ctx.warn(f"bin: method={method!r} unknown; using quantile.")
    return [f"{kw} {tgt} = quantile({col}, {bins})"]


def _freq_cond(col, kwargs, ctx, minv):
    """Build (lines, condition, drops) for a frequency test on col: count < minv
    (int minv) or proportion < minv (0<minv<1). Shared by reduce_categories /
    eliminate(min=)."""
    one, cnt = ctx.tmp("one"), ctx.tmp("cnt")
    lines = [f"generate {one} = 1", _agg("count", one, cnt, col)]
    drops = [one, cnt]
    if 0 < minv < 1:                            # proportion of total
        tot = ctx.tmp("tot")
        lines.append(_agg("count", one, tot, one))
        drops.append(tot)
        return lines, f"{cnt} < {minv} * {tot}", drops
    return lines, f"{cnt} < {minv}", drops       # absolute count


def emit_reduce_categories(col, kwargs, ctx):
    """reduce_categories(col, min=N, to="Other") — merge RARE categories into one.
    min: int → count < N (absolute); 0<N<1 → proportion of total (0.01 = 1%).
    to: the catch-all value (default "Other"); to=None → missing."""
    minv = kwargs.get("min")
    if minv is None:
        ctx.warn("reduce_categories: needs min= (int = count, 0<x<1 = proportion).")
        return ["// reduce_categories: missing min="]
    to = kwargs.get("to", "Other")
    vexpr = "." if to is None else _mval(to)
    lines, cond, drops = _freq_cond(col, kwargs, ctx, minv)
    lines += [f"replace {col} = {vexpr} if {cond}", "drop " + " ".join(drops)]
    return lines


def emit_change(col, kwargs, ctx):
    """change(col, value, where="cond") — set value where a condition holds.
    value=None → missing; the condition may reference OTHER variables. For
    frequency-based category merging use reduce_categories; for value/interval
    remapping by the variable's own value use recode."""
    pos = kwargs.get("_pos") or []
    has_value = "value" in kwargs or "to" in kwargs or len(pos) >= 1
    value = kwargs.get("value", kwargs.get("to", pos[0] if pos else None))
    if not has_value:
        ctx.warn('change: needs a value, e.g. change(x, "Y", where="…"). None = missing.')
        return ["// change: missing value"]
    where = kwargs.get("where")
    if not where:
        ctx.warn("change: needs where= (a condition). For rare categories use "
                 "reduce_categories.")
        return ["// change: needs where="]
    vexpr = "." if value is None else _mval(value)
    return [f"replace {col} = {vexpr} if {where}"]


def emit_recode(col, kwargs, ctx):
    """recode(col, rules) — remap a variable by its OWN value. `rules` is either
      a microdata recode string:  recode(x, "(1/7 = 0) (* = 9)")  — full control
      or a dict:                   recode(x, {1: 0, 2: 0, 3: 1})  — friendly
    Optional labels={code: text} adds define-labels/assign-labels.
    (Frequency / cross-variable conditions → use change instead.)"""
    rules = kwargs.get("rules")
    if rules is None:
        pos = kwargs.get("_pos") or []
        rules = pos[0] if pos else None
    if not rules:
        ctx.warn('recode: needs rules, e.g. recode(x, "(1/7 = 0)") or '
                 'recode(x, {1: 0, 2: 0}).')
        return ["// recode: missing rules"]
    if isinstance(rules, dict):                 # dict → recode rules
        rules = " ".join(f"({_mval(k)} = {_mval(v)})" for k, v in rules.items())
    tgt = kwargs.get("into") or col
    lines = []
    if kwargs.get("into"):
        lines.append(f"clone-variables {col} -> {tgt}")
    lines.append(f"recode {tgt} {rules}")
    if isinstance(kwargs.get("labels"), dict):
        lines += _emit_labels(ctx, tgt, kwargs["labels"])
    return lines


def emit_shorten(col, kwargs, ctx):
    """shorten(code, keep=3) → keep the first `keep` characters."""
    keep = kwargs.get("keep", 3)
    if "min_count" in kwargs:
        ctx.warn("shorten: rarity-cascade ('min_count') needs counts (Tier B) — "
                 "emitting fixed-length truncation only.")
    kw, tgt = _target(col, kwargs)
    return [f"{kw} {tgt} = substr({col}, 1, {keep})"]


def emit_eliminate(col, kwargs, ctx):
    """eliminate — drop ROWS (the row-dropping parallel to change).

      eliminate(where="cond")   → drop if cond            (conditional drop)
      eliminate(col, min=5)     → drop if count(col) < 5  (drop rare records)

    min: int → count; 0<min<1 → proportion. Cell-masking to missing →
    change(col, None, where=…). Random subset → microdata's own `sample`.
    """
    minv = kwargs.get("min")
    if minv is not None:                       # drop rare records of `col`
        if not col:
            ctx.warn("eliminate(min=): needs a column (the category to count).")
            return ["// eliminate: min needs a column"]
        lines, cond, drops = _freq_cond(col, kwargs, ctx, minv)
        lines += [f"drop if {cond}", "drop " + " ".join(drops)]
        return lines

    where = kwargs.get("where")
    if not where:
        ctx.warn("eliminate: needs where= (drop rows) or min= (drop rare records).")
        return ["// eliminate: needs where= or min="]
    return [f"drop if {where}"]


def emit_function(col, kwargs, ctx):
    """function(col, func, *args) → replace col = func(col, args…).
    A pass-through to any microdata function (escape hatch). Accessible as
    apply_function(...) via the prefix. Multi-var: pass a list as the first arg.
      function(lonn, round, 10000)  →  replace lonn = round(lonn, 10000)
      function(kode, substr, 1, 3)  →  replace kode = substr(kode, 1, 3)
    """
    pos = kwargs.get("_pos") or []
    func = kwargs.get("func", pos[0] if pos else None)
    if not func:
        ctx.warn("function: needs a function name, e.g. function(x, round, 1000).")
        return ["// function: missing function name"]
    extra = pos[1:] if len(pos) > 1 else []
    kw, tgt = _target(col, kwargs)
    inner = ", ".join([col] + [_mval(a) for a in extra])
    return [f"{kw} {tgt} = {func}({inner})"]


def _seed_of(kwargs, ctx):
    """Explicit seed/random_state wins; otherwise a fresh ctx block (so two
    seed-less random ops don't share a seed → correlated draws)."""
    if "seed" in kwargs:
        return kwargs["seed"]
    if "random_state" in kwargs:
        return kwargs["random_state"]
    return ctx.next_seed()


_KEY_PRIMES = [1, 31, 97, 131, 197, 241, 313, 397]


def emit_jitter(col, kwargs, ctx):
    """jitter — deterministic 'hash noise' added in place (Strategy 1).

    Builds a per-row pseudo-uniform from a key (other columns + seed) via the
    fract(sin(k*C)) trick, then adds it. dist='normal' uses Box-Muller. The
    key defaults to the target column; pass key=[...] to decorrelate.
    NB: this is reversible by anyone who knows the inputs — display-only.
    """
    seed = _seed_of(kwargs, ctx)
    dist = kwargs.get("dist", "uniform")
    key = kwargs.get("key", col)
    cols = [key] if isinstance(key, str) else list(key)
    kexpr = " + ".join(c if p == 1 else f"{c}*{p}"
                       for c, p in zip(cols, _KEY_PRIMES))
    if seed:
        kexpr = f"{kexpr} + {seed}"

    k = ctx.tmp("k")
    ua = ctx.tmp("ua")
    lines, drops = [], [k, ua]

    # scale: numeric literal, or 'auto' = 0.01 * range (via aggregate min/max)
    scale = kwargs.get("scale", "auto")
    if isinstance(scale, (int, float)):
        scale_expr = repr(scale)
        pre = []
    else:
        lo, hi, sc = ctx.tmp("lo"), ctx.tmp("hi"), ctx.tmp("sc")
        pre = [f"aggregate (min) {col} -> {lo}",
               f"aggregate (max) {col} -> {hi}",
               f"generate {sc} = ({hi} - {lo}) * 0.01"]
        scale_expr = sc
        drops += [lo, hi, sc]

    lines += [f"generate {k} = {kexpr}",
              f"generate {ua} = sin({k} * 12.9898) * 43758.5453",
              f"replace {ua} = {ua} - floor({ua})"]

    if dist == "normal":
        ub, z = ctx.tmp("ub"), ctx.tmp("z")
        lines += [f"generate {ub} = sin({k} * 78.233) * 43758.5453",
                  f"replace {ub} = {ub} - floor({ub})",
                  f"generate {z} = sqrt(-2*ln({ua})) * cos(2*pi()*{ub})",
                  f"replace {col} = {col} + {z} * {scale_expr}"]
        drops += [ub, z]
    else:
        lines.append(f"replace {col} = {col} + ({ua} - 0.5) * 2 * {scale_expr}")

    return pre + lines + ["drop " + " ".join(drops)]


def _num(x):
    """Render a number without a trailing '.0' (cleaner microdata)."""
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x)


def _mval(v):
    """Render a value for recode/replace: strings in single quotes, numbers bare."""
    return f"'{v}'" if isinstance(v, str) else _num(v)


def _agg(stat, col, dest, by):
    """One aggregate line; empty by() = population-level (see README open Q1)."""
    return f"aggregate ({stat}) {col} -> {dest}" + (f", by({by})" if by else "")


# microdata has no RNG. This is the one constructible source: each seeded
# `sample` round is a Bernoulli(p) bit; weighting and summing repeated rounds
# yields whole families of distributions. Per-unit (`clone-units`, merge on the
# unit id) is robust; per-row needs a row-unique merge key. `_FNR` unit ids are
# write-protected, so merge — never hash — is the only way to key on them.
_RAND_DISTS = ("bernoulli", "binomial", "normal", "uniform_int",
               "uniform", "uniform_sym")


def _random_series(ctx, *, dist, rounds, level, key, ds, seed, p=0.5, dest=None):
    """Build a pseudo-random column via repeated seeded sample+merge+recode.

    dist → how the per-round Bernoulli bits are combined:
      bernoulli  one bit (0/1, prob p)
      binomial   sum of `rounds` bits → integer 0..rounds (≈ normal, CLT)
      normal     binomial, standardised to ≈ N(0,1)
      uniform_int weights 2^i → uniform integer in [0, 2^rounds)
      uniform    weights 2^-(i+1) → binary fraction in [0,1)

    Construction (NB: `clone-units` yields an EMPTY dataset you cannot `sample`/
    `generate` on; and you cannot `keep` the id directly — it is a key variable,
    auto-retained). So: `generate` a normal marker (=1) in `ds`, `clone-dataset`,
    `keep` the marker (the id follows automatically) → a small reusable source.
    Then per round clone that small source (sample is destructive → fresh copy
    each round), `sample`, flag, `merge` back onto `ds`, `recode` missing→0.
    Finally delete the source and drop the marker from `ds`.
    Assumes one row per unit in `ds`; for panel data collapse to unit level
    first. UNVERIFIED against live microdata — see VERIFY.md.

    Returns (lines, dest_var, temps_to_drop). dest_var holds the result and is
    NOT in the drop list — the caller decides whether to keep or drop it.
    """
    if dist == "bernoulli":
        rounds = 1
    rounds = max(1, int(rounds))
    mark, src = ctx.tmp("mark"), ctx.tmp("src")
    lines = [
        f"generate {mark} = 1",               # a normal var (the id key can't be kept alone)
        f"clone-dataset {ds} {src}",
        f"use {src}",
        f"keep {mark}",                        # keeps marker; the id key is auto-retained
    ]
    bits = []
    for i in range(rounds):
        rnd, bit = ctx.tmp("round"), ctx.tmp("bit")
        lines += [
            f"clone-dataset {src} {rnd}",      # copy the small marker-only source
            f"use {rnd}",
            f"sample {p} {seed + i}",
            f"generate {bit} = 1",
            f"merge {bit} into {ds} on {key}",
            f"use {ds}",
            f"recode {bit} (missing = 0)",
            f"delete-dataset {rnd}",
        ]
        bits.append(bit)
    lines.append(f"delete-dataset {src}")

    if dist == "uniform_int":
        weights = [2 ** i for i in range(rounds)]
    elif dist in ("uniform", "uniform_sym"):
        weights = [0.5 ** (i + 1) for i in range(rounds)]
    else:                                   # bernoulli / binomial / normal
        weights = [1] * rounds
    terms = [b if w == 1 else f"{b}*{w}" for b, w in zip(bits, weights)]
    total = " + ".join(terms)

    out = dest or ctx.tmp("rand")
    if dist == "normal":
        mean = rounds * p
        sd = (rounds * p * (1 - p)) ** 0.5
        lines.append(f"generate {out} = ({total} - {_num(mean)}) / {_num(round(sd, 6))}")
    elif dist == "uniform_sym":             # centered uniform in [-1, 1) — no spike at 0
        lines.append(f"generate {out} = ({total}) * 2 - 1")
    else:
        lines.append(f"generate {out} = {total}")
    return lines, out, bits + [mark]      # mark dropped from ds by the caller


def _digit_random(ctx, from_col, mod, dist, dest=None):
    """Cheap data-derived 'random' column from the low digits of `from_col`.

    Deterministic and REVERSIBLE (an attacker who knows from_col can recompute
    it), and only as uniform as the source digits (real data heaps on round
    numbers — pick a column with noisy low digits). Use as a cheap supplement,
    not as anonymisation. dist: 'uniform' [0,1) or 'uniform_sym' [-1,1).
    """
    if dist not in ("uniform", "uniform_sym"):
        ctx.warn(f"digits source: dist={dist!r} not meaningful from one column; "
                 f"using 'uniform_sym'.")
        dist = "uniform_sym"
    mv = ctx.tmp("dig")
    out = dest or ctx.tmp("rand")
    lines = [f"generate {mv} = {from_col} - floor({from_col} / {mod}) * {mod}"]
    if dist == "uniform":
        lines.append(f"generate {out} = {mv} / {mod}")
    else:
        lines.append(f"generate {out} = ({mv} / {mod}) * 2 - 1")
    return lines, out, [mv]


def _build_random(ctx, *, source, dist, rounds, level, key, ds, seed, p,
                  from_col, mod, digit_weight, dest=None):
    """Dispatch a random column over `source`: 'sample' | 'digits' | 'hybrid'.

    sample  truly random (seeded sample+merge), per-unit, costly
    digits  data-derived (mod of from_col), cheap, reversible
    hybrid  sample (chosen dist) + a small digit term to guarantee coverage even
            when the sample draw lands on its mode (relevant for dist='normal')
    """
    if source == "digits":
        if not from_col:
            ctx.warn("source='digits' needs from_col=<column>.")
            return ["// random: from_col required for source='digits'"], dest, []
        return _digit_random(ctx, from_col, mod, dist, dest)

    if source == "hybrid":
        if not from_col:
            ctx.warn("source='hybrid' needs from_col=<column>; using sample only.")
            return _random_series(ctx, dist=dist, rounds=rounds, level=level,
                                  key=key, ds=ds, seed=seed, p=p, dest=dest)
        s_lines, s_var, s_drops = _random_series(ctx, dist=dist, rounds=rounds,
                                                 level=level, key=key, ds=ds, seed=seed, p=p)
        d_lines, d_var, d_drops = _digit_random(ctx, from_col, mod, "uniform_sym")
        out = dest or ctx.tmp("rand")
        lines = s_lines + d_lines + [f"generate {out} = {s_var} + {digit_weight}*{d_var}"]
        return lines, out, s_drops + d_drops + [s_var, d_var]

    return _random_series(ctx, dist=dist, rounds=rounds, level=level,
                          key=key, ds=ds, seed=seed, p=p, dest=dest)


def _noise_apply(col, z, scale, pct, default_pct):
    """How the draw `z` is applied to `col`. Precedence:
    scale → additive (col + scale·z); else pct → relative (col·(1+pct·z));
    else default_pct (None ⇒ additive offset col+1·z, for using=; else relative).
    """
    if scale is not None:
        return f"replace {col} = {col} + {scale}*{z}"
    if pct is not None:
        return f"replace {col} = {col} * (1 + {pct}*{z})"
    if default_pct is None:
        return f"replace {col} = {col} + 1*{z}"          # additive offset (reuse)
    return f"replace {col} = {col} * (1 + {default_pct}*{z})"


def emit_noise(col, kwargs, ctx):
    """noise — genuine seeded noise. Self-scaling by default.

    With no `scale`, applies RELATIVE noise `col * (1 + pct*z)` (pct default 0.05
    → ±5 %): self-scaling, so `noise(inntekt)` works without knowing the mean/SD.
    Pass `scale=` for absolute additive noise (`col + scale*z`). NB: relative
    leaves exact zeros unchanged (0·anything = 0).

    Default dist `uniform_sym` (centered uniform [-1,1)) has no mass point at 0,
    so virtually no value is left unchanged (unlike `normal`/`binomial`).

    source: 'sample' (default, per-unit; uses unit_id, from ctx if set) |
            'digits' (cheap, from low digits of from_col=<col>; reversible) |
            'hybrid' (sample + small digit term).

    `using=<column>` reuses a pre-built `draw(...)` column (additive by default —
    e.g. a date offset; pass pct= for relative).
    """
    scale = kwargs.get("scale")
    pct = kwargs.get("pct")
    using = kwargs.get("using")
    if scale is not None and not isinstance(scale, (int, float)):
        ctx.warn("noise: 'scale' must be numeric.")
        return ["// noise: 'scale' must be numeric"]

    if using:                                   # reuse: additive offset unless scale/pct given
        return [_noise_apply(col, using, scale, pct, default_pct=None)]

    source = kwargs.get("source", "sample")
    dist = kwargs.get("dist", "uniform_sym")
    seed = _seed_of(kwargs, ctx)
    rounds = int(kwargs.get("rounds", 12))
    unit = kwargs.get("unit_id") or ctx.unit_id
    ds = kwargs.get("dataset") or ctx.dataset
    from_col = kwargs.get("from_col")
    mod = kwargs.get("mod", 100)
    digit_weight = kwargs.get("digit_weight", 0.3)

    if source in ("sample", "hybrid") and not unit:
        ctx.warn("noise: 'unit_id' required (merge key, or set it on transform), "
                 "or pass using=<draw column> / source='digits'.")
        return ["// noise: 'unit_id' required (or using= / source='digits')"]

    lines, z, drops = _build_random(
        ctx, source=source, dist=dist, rounds=rounds, level="unit", key=unit,
        ds=ds, seed=seed, p=0.5, from_col=from_col, mod=mod,
        digit_weight=digit_weight)
    if z is None:                       # build failed (e.g. digits without from_col)
        return lines
    lines.append(_noise_apply(col, z, scale, pct, default_pct=0.05))   # relative ±5% default
    lines.append("drop " + " ".join(drops + [z]))
    return lines


def _per_unit_uniform(ctx, unit_id, ds, seed, bits):
    """Per-unit pseudo-uniform in [0,1) — thin wrapper over _random_series."""
    lines, u, drops = _random_series(ctx, dist="uniform", rounds=bits,
                                     level="unit", key=unit_id, ds=ds, seed=seed)
    drops = list(drops) + [u]
    return lines, u, drops


def emit_diff(col, kwargs, ctx):
    """diff — subtract a reference value: a general centering / relativizing op.

    diff is NOT date-specific. It computes `col - reference(col)` for any numeric
    variable (income, age, dates, …). The reference is set by `ref`:

      <stat>            global anchor       e.g. 'mean', 'median', 'min', 'p25'
                                            (mean-centering, de-meaning, …)
      <stat>_per_unit   per-unit anchor     e.g. 'mean_per_unit' (within-person
                                            demeaning), 'first_per_unit' (=min)
      'random_global'   (DEFAULT) hidden global shift: a stat + secret `let`
                                            offset — one unknown anchor for all
      'random_per_unit' tilfeldig per-unit anchor (the per-unit secret key)
      'YYYY-MM-DD'      fixed date literal   (date variables)
      <column name>     pairwise anchor      (col - other)

    Stats: min/max/mean/median/p25/p75. Per-unit forms require unit_id.
    NB: p2m's default ref (random_global) differs from protect's (first_per_unit)
    so a bare diff(x) is useful without unit_id. `unit='years'|'months'` divides
    the result (date-only).
    """
    ref = kwargs.get("ref", "random_global")
    unit = kwargs.get("unit", "days")
    unit_id = kwargs.get("unit_id") or ctx.unit_id
    ds = kwargs.get("dataset") or ctx.dataset
    seed = _seed_of(kwargs, ctx)
    bits = int(kwargs.get("bits", 8))

    per_unit = isinstance(ref, str) and ref.endswith("_per_unit") and ref != "random_per_unit"
    needs_unit = per_unit or ref == "random_per_unit"
    if needs_unit and not unit_id:
        ctx.warn(f"diff: ref={ref!r} requires unit_id.")
        return [f"// diff: ref={ref} requires unit_id"]

    lines, drops, anchor = [], [], None
    if per_unit:                                    # <stat>_per_unit (incl. first_per_unit)
        stat = ref[:-len("_per_unit")]
        if stat == "first":
            stat = "min"
        if stat not in _DIFF_STATS:
            ctx.warn(f"diff: per-unit stat {stat!r} not in {_DIFF_STATS}; using min.")
            stat = "min"
        anchor = ctx.tmp("anchor")
        lines.append(_agg(stat, col, anchor, unit_id))
        drops = [anchor]
    elif ref in _DIFF_STATS:                        # global <stat> anchor
        anchor = ctx.tmp("anchor")
        lines.append(_agg(ref, col, anchor, None))
        drops = [anchor]
    elif ref == "random_per_unit":
        klines, u, kdrops = _per_unit_uniform(ctx, unit_id, ds, seed, bits)
        lo, hi, span, anchor = (ctx.tmp("mind"), ctx.tmp("maxd"),
                                ctx.tmp("span"), ctx.tmp("anchor"))
        lines += klines + [
            _agg("min", col, lo, None), _agg("max", col, hi, None),
            f"generate {span} = {hi} - {lo}",
            f"generate {anchor} = {lo} + floor({u} * {span})",
        ]
        drops = kdrops + [lo, hi, span, anchor]
    elif ref == "random_global":
        stat = kwargs.get("stat", "median")
        if stat not in ("min", "max", "median"):
            ctx.warn(f"diff(random_global): stat={stat!r} not in min/max/median; using median.")
            stat = "median"
        shift = kwargs.get("shift", 0)
        one, med, sh = ctx.tmp("one"), ctx.tmp("med"), ctx.tmp("shift")
        lines += [
            f"generate {one} = 1",
            _agg(stat, col, med, one),
            f"let {sh} = {shift}   // secret offset — set to a number only you know",
        ]
        anchor = f"({med} + {sh})"   # the let binding is the key; not dropped
        drops = [one, med]
    elif isinstance(ref, str) and _DATE_RE.match(ref):
        y, m, d = (int(p) for p in ref.split("-"))
        anchor = f"date({y}, {m}, {d})"
    else:
        anchor = str(ref)        # another date column — pairwise anchor

    div = _UNIT_DIV.get(unit)
    if div:
        lines.append(f"replace {col} = ({col} - {anchor}) / {div}   // {unit}, approx")
    else:
        lines.append(f"replace {col} = {col} - {anchor}")
    if drops:
        lines.append("drop " + " ".join(drops))
    return lines


def emit_diff_date(col, kwargs, ctx):
    """diff_date — like diff, but the column is a 'YYYY-MM-DD' STRING.

    microdata's diffable date is the integer date-value (days since 1970). A
    string date must be parsed first: date(to_int(substr(...))). Use diff_date
    when the variable is stored as an ISO string; use diff when it is already a
    date-value. Takes the same args as diff (ref/unit/unit_id/…).

    NB: this replaces the string column in place with its date-value. If
    microdata rejects changing a string variable's type via replace, the parse
    must instead go into a new variable — verify on live microdata (open Q4).
    """
    parse = (f"replace {col} = date(to_int(substr({col}, 1, 4)), "
             f"to_int(substr({col}, 6, 2)), to_int(substr({col}, 9, 2)))")
    return [parse] + emit_diff(col, kwargs, ctx)


_PCTL_STAT = {0.25: "p25", 0.5: "median", 0.75: "p75"}
# Per-method default limits so a bare winsorize(col) is valid in microdata.
# (protect's percentile default (0.01,0.99) is impossible here — see percentile mode.)
_WINS_DEFLIM = {"iqr": (1.5, 1.5), "gaussian": (3, 3), "mad": (3, 3),
                "percentile": (0.25, 0.75)}


def emit_winsorize(col, kwargs, ctx):
    """winsorize — cap extremes. methods: iqr (default), gaussian, mad, percentile, value.

    Bounds come from `aggregate` (written back as columns), then `replace` clips.
    Default method is `iqr` with limits (1.5, 1.5) so `winsorize(col)` just works;
    limits default per method (gaussian/mad → 3, percentile → 0.25/0.75). microdata
    exposes only p25/median/p75, so percentile mode is limited to those cut points.
    """
    method = kwargs.get("method", "iqr")
    limits = kwargs.get("limits", _WINS_DEFLIM.get(method, (1.5, 1.5)))
    lo_arg, hi_arg = limits if isinstance(limits, (tuple, list)) else (limits, limits)
    by = kwargs.get("by")
    if kwargs.get("share", 1.0) != 1.0:
        ctx.warn("winsorize: 'share' ignored (no partial value transform in microdata).")

    # value: limits are exact bounds — no statistic needed.
    if method == "value":                       # removed — that's just change()
        ctx.warn("winsorize: method='value' removed — use "
                 "change(col, bound, where='col > bound') for manual top/bottom-coding.")
        return ["// winsorize: method='value' removed; use change(...)"]

    # percentile: the bound IS the percentile value (p25/median/p75 only).
    if method == "percentile":
        out, drops = [], []
        for arg, cmp_, side in ((lo_arg, "<", "lo"), (hi_arg, ">", "hi")):
            if arg is None:
                continue
            stat = _PCTL_STAT.get(arg)
            if stat is None:
                ctx.warn(f"winsorize: percentile {arg} unavailable in microdata "
                         f"(only 0.25/0.5/0.75). Use method='iqr'.")
                out.append(f"// winsorize: percentile {arg} unavailable ({side} skipped)")
                continue
            d = ctx.tmp(side)
            out += [_agg(stat, col, d, by), f"replace {col} = {d} if {col} {cmp_} {d}"]
            drops.append(d)
        if drops:
            out.append("drop " + " ".join(drops))
        return out

    # gaussian / iqr / mad: center +/- multiplier * spread.
    lines, drops = [], []
    if method == "gaussian":
        center, spread = ctx.tmp("m"), ctx.tmp("s")
        lines += [_agg("mean", col, center, by), _agg("sd", col, spread, by)]
        lo_base = hi_base = center
        drops += [center, spread]
    elif method == "iqr":
        q1, q3, spread = ctx.tmp("q1"), ctx.tmp("q3"), ctx.tmp("iqr")
        lines += [_agg("p25", col, q1, by), _agg("p75", col, q3, by),
                  f"generate {spread} = {q3} - {q1}"]
        lo_base, hi_base = q1, q3
        drops += [q1, q3, spread]
    elif method == "mad":
        med, ad, spread = ctx.tmp("med"), ctx.tmp("ad"), ctx.tmp("mad")
        lines += [_agg("median", col, med, by),
                  f"generate {ad} = abs({col} - {med})",
                  _agg("median", ad, spread, by)]
        lo_base = hi_base = med
        drops += [med, ad, spread]
    else:
        ctx.warn(f"winsorize: unknown method {method!r}")
        return [f"// winsorize: unknown method {method!r}"]

    if lo_arg is not None:
        lo_v = ctx.tmp("lo")
        lines += [f"generate {lo_v} = {lo_base} - {lo_arg}*{spread}",
                  f"replace {col} = {lo_v} if {col} < {lo_v}"]
        drops.append(lo_v)
    if hi_arg is not None:
        hi_v = ctx.tmp("hi")
        lines += [f"generate {hi_v} = {hi_base} + {hi_arg}*{spread}",
                  f"replace {col} = {hi_v} if {col} > {hi_v}"]
        drops.append(hi_v)
    lines.append("drop " + " ".join(drops))
    return lines


def emit_microaggregate(col, kwargs, ctx):
    """microaggregate(x, by=…, stat='mean') → replace each value with its group
    statistic (a standard SDC method; microdata Tiltak 10). `by` defaults to the
    session unit_id (within-person aggregation)."""
    stat = kwargs.get("stat", "mean")
    by = kwargs.get("by") or kwargs.get("unit_id") or ctx.unit_id
    if not by:
        ctx.warn("microaggregate: 'by' (the grouping) required.")
        return ["// microaggregate: 'by' required"]
    if stat not in ("mean", "median", "min", "max", "p25", "p75", "sum"):
        ctx.warn(f"microaggregate: stat={stat!r} unsupported; using mean.")
        stat = "mean"
    g = ctx.tmp("ma")
    return [_agg(stat, col, g, by), f"replace {col} = {g}", f"drop {g}"]


def emit_risk(col, kwargs, ctx):
    """risk — measure disclosure risk (k-anonymity) for a set of quasi-IDs.

    Builds a composite quasi-ID key, counts the equivalence-class size per row,
    and `summarize`s it (min = k-anonymity). Also flags rows in cells < 5 and
    uniquely-identifiable rows. Non-destructive: temps are dropped, the
    `summarize` output IS the report.

    Limitations (warned): per-unit projection needs count-distinct (unavailable,
    so reports ROW-level cells); l-diversity needs entropy (not expressible).
    """
    quasi = kwargs.get("quasi_ids")
    if not quasi:
        ctx.warn("risk: quasi_ids required (the columns an attacker could link on).")
        return ["// risk: quasi_ids required"]
    if isinstance(quasi, str):
        quasi = [quasi]
    if kwargs.get("unit_id") or ctx.unit_id:
        ctx.warn("risk: per-unit k-anonymity needs count-distinct (unavailable in "
                 "microdata); reporting ROW-level equivalence-class sizes instead.")
    if kwargs.get("sensitive"):
        ctx.warn("risk: l-diversity (entropy over sensitive values) isn't "
                 "expressible in microdata; reporting k-anonymity only.")

    one, qkey, cell = ctx.tmp("one"), ctx.tmp("qkey"), ctx.tmp("cell")
    below, uniq = ctx.tmp("below"), ctx.tmp("uniq")
    keyexpr = ' ++ "_" ++ '.join(f"string({q})" for q in quasi)
    return [
        f"generate {one} = 1",
        f"generate {qkey} = {keyexpr}",
        _agg("count", one, cell, qkey),
        f"summarize {cell}   // min = k-anonymity (smallest equivalence class)",
        f"generate {below} = 1 * ({cell} < 5)",
        f"generate {uniq} = 1 * ({cell} == 1)",
        f"summarize {below} {uniq}   // means = share of rows in cells <5 / uniquely identifiable",
        "drop " + " ".join([one, qkey, cell, below, uniq]),
    ]


def emit_draw(col, kwargs, ctx):
    """draw(name, dist=…, rounds=…, unit_id=…, seed=…, source=…, …)

    Materialise a REUSABLE pseudo-random column named `name` (the positional arg).
    Make it once, then reference it from later lines (e.g. noise(x, using=name)).

    source: 'sample' (default) | 'digits' (from_col=<col>) | 'hybrid'.
    dist:   uniform | uniform_sym | normal | binomial | uniform_int | bernoulli.
    """
    dist = kwargs.get("dist", "uniform")
    if dist not in _RAND_DISTS:
        ctx.warn(f"draw: dist={dist!r} not in {_RAND_DISTS}; using 'uniform'.")
        dist = "uniform"
    source = kwargs.get("source", "sample")
    rounds = int(kwargs.get("rounds", 8))
    level = kwargs.get("level", "unit")
    seed = _seed_of(kwargs, ctx)
    ds = kwargs.get("dataset") or ctx.dataset
    p = kwargs.get("p", 0.5)
    from_col = kwargs.get("from_col")
    mod = kwargs.get("mod", 100)
    digit_weight = kwargs.get("digit_weight", 0.3)
    key = kwargs.get("key") or kwargs.get("unit_id") or ctx.unit_id
    if source in ("sample", "hybrid") and not key:
        ctx.warn("draw: a merge key is required (unit_id for level='unit', "
                 "or key= for level='row').")
        return ["// draw: missing key (unit_id / key)"]

    lines, z, drops = _build_random(
        ctx, source=source, dist=dist, rounds=rounds, level=level, key=key,
        ds=ds, seed=seed, p=p, from_col=from_col, mod=mod,
        digit_weight=digit_weight, dest=col)
    if z is None:                       # build failed (e.g. digits without from_col)
        return lines
    lines.append("drop " + " ".join(drops))   # keep `col`; drop only the temps
    return lines


REGISTRY = {
    "round":          emit_round,
    "round_date":     emit_round_date,
    "bin":            emit_bin,
    "recode":         emit_recode,
    "change":         emit_change,
    "reduce_categories": emit_reduce_categories,
    "function":       emit_function,
    "shorten":        emit_shorten,
    "eliminate":      emit_eliminate,
    "microaggregate": emit_microaggregate,
    "jitter":         emit_jitter,
    "noise":          emit_noise,
    "winsorize":      emit_winsorize,
    "diff":           emit_diff,
    "diff_date":      emit_diff_date,
    "draw":           emit_draw,
    "risk":           emit_risk,
}

# Tier B — feasible but multi-line; deferred until open questions are verified.
TIER_B_PENDING = {
    "profile":   "macro expansion of constituent verbs",
}

# Tier C — not expressible in microdata.
TIER_C_REASONS = {
    "swap":         "no shuffle / sort-pairing / RNG for value exchange",
    "pseudonymize": "IDs are write-protected pseudonyms; data is already pseudonymized at source",
    "insert":       "no row-append command (decoy rows cannot be created)",
    "suppress":     "output protection is applied automatically by the platform (Tiltak 2–10)",
}

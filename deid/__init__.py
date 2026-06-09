"""
deid — protect-inspired SDC verbs → microdata.no script translator.

Write statistical-disclosure-control verbs in a small call DSL
(e.g. ``round(income, to=1000)``) and get valid microdata.no script.

Quick start:
    from deid import transform

    result = transform("round(income, to=1000)\\nshorten(icd, keep=3)")
    print(result.script())
    for w in result.warnings:
        print("WARNING:", w)

Conveniences:
  - Multiple columns: pass a list as the first arg — round([a, b, c], to=1000).
  - Optional namespacing prefix: round / apply_round / apply-round all work.

See README.md for the full verb reference and feasibility analysis.
"""
import ast
import re
from dataclasses import dataclass, field

from .commands import REGISTRY, Ctx, TIER_B_PENDING, TIER_C_REASONS

# Optional verb prefixes (namespacing). All map to the bare verb for dispatch.
_PREFIXES = ("apply_", "scrub_", "sdc_")
# A leading hyphenated verb (microdata style, e.g. apply-round) → underscore,
# so Python's parser accepts it. Only touches the verb token at line start.
_HYPHEN_VERB = re.compile(r"(?m)^(\s*)([A-Za-z][\w]*)-([A-Za-z][\w]*)(\s*\()")


@dataclass
class TranslationResult:
    microdata_lines: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def script(self) -> str:
        return "\n".join(self.microdata_lines)


def _column_of(node: ast.AST):
    """A column reference → its name. Accepts a bare name or a string."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _columns_of(node: ast.AST):
    """First positional arg → list of columns. A list literal applies the verb
    to each column; a single name/string → one column."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return [c for c in (_column_of(e) for e in node.elts) if c is not None]
    c = _column_of(node)
    return [c] if c is not None else [None]


def _kwargs_of(call: ast.Call) -> dict:
    """Evaluate keyword arguments to plain Python values via literal_eval."""
    out = {}
    for kw in call.keywords:
        try:
            out[kw.arg] = ast.literal_eval(kw.value)
        except (ValueError, SyntaxError):
            out[kw.arg] = ast.unparse(kw.value)   # non-literal → keep source text
    return out


def _canonical(verb: str):
    """Strip an optional namespacing prefix (apply_/scrub_/sdc_)."""
    for pre in _PREFIXES:
        if verb.startswith(pre):
            return verb[len(pre):]
    return verb


def transform(source: str, dataset: str = "main",
              unit_id: str = None, seed: int = 0) -> TranslationResult:
    """Translate the SDC-verb DSL into a microdata.no script.

    Each statement is one verb call: ``verb(column[, **kwargs])``. The column is
    the first positional arg — a bare name, a string, or a **list** of columns
    (the verb is then applied to each). An optional prefix is allowed:
    ``round`` / ``apply_round`` / ``apply-round`` are equivalent.

    dataset / unit_id set the active dataset and default unit key for the random
    verbs. seed is the session base seed: each seed-less random op gets a distinct
    block (base + n·1000) so independent draws don't correlate.
    """
    result = TranslationResult()
    ctx = Ctx(warnings=result.warnings, dataset=dataset, unit_id=unit_id, seed=seed)

    source = _HYPHEN_VERB.sub(r"\1\2_\3\4", source)   # apply-round( → apply_round(
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        result.warnings.append(f"Could not parse input: {e}")
        return result

    for stmt in tree.body:
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
            src = ast.get_source_segment(source, stmt) or "<stmt>"
            result.warnings.append(f"Skipped non-call statement: {src!r}")
            continue

        call = stmt.value
        raw_verb = call.func.id if isinstance(call.func, ast.Name) else None
        verb = _canonical(raw_verb) if raw_verb else None
        kwargs = _kwargs_of(call)
        if len(call.args) > 1:                 # extra positionals (e.g. recode rules)
            extra = []
            for a in call.args[1:]:
                try:
                    extra.append(ast.literal_eval(a))
                except (ValueError, SyntaxError):
                    extra.append(ast.unparse(a))
            kwargs["_pos"] = extra
        cols = _columns_of(call.args[0]) if call.args else [None]

        for col in cols:                          # one block per column (multi-var)
            label = f"{raw_verb}({col}" + (", …)" if kwargs or len(cols) > 1 else ")")
            if len(cols) == 1:
                label = ast.get_source_segment(source, call) or label
            result.microdata_lines.append(f"// {label}")

            if verb in REGISTRY:
                result.microdata_lines.extend(REGISTRY[verb](col, kwargs, ctx))
            elif verb in TIER_B_PENDING:
                ctx.warn(f"{verb}: not yet implemented (Tier B) — {TIER_B_PENDING[verb]}")
                result.microdata_lines.append(
                    f"// PENDING (Tier B): {verb} — {TIER_B_PENDING[verb]}")
            elif verb in TIER_C_REASONS:
                ctx.warn(f"{verb}: not expressible in microdata — {TIER_C_REASONS[verb]}")
                result.microdata_lines.append(
                    f"// UNSUPPORTED: {verb} — {TIER_C_REASONS[verb]}")
            else:
                ctx.warn(f"Unknown verb: {raw_verb}")
                result.microdata_lines.append(f"// UNKNOWN verb: {raw_verb}")

    return result


__all__ = ["transform", "TranslationResult"]

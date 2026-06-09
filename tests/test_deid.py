"""Tests for the deid Tier-A skeleton (protect-DSL → microdata.no)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from deid import transform


def _lines(src):
    return transform(src).script().splitlines()


# ── Tier A: near-direct translations ───────────────────────────────────────────

def test_round_numeric():
    assert _lines("round(income, to=50)") == [
        "// round(income, to=50)",
        "replace income = round(income, 50)",
    ]


def test_round_rejects_string_to():
    res = transform("round(dob, to='year')")     # dates must use round_date
    assert any("round_date" in w for w in res.warnings)


def test_round_date_period():
    assert "replace dob = quarter(dob)" in _lines("round_date(dob, to='quarter')")
    assert "replace dob = year(dob)" in _lines("round_date(dob)")   # default year


def test_round_into_generates_new_var():
    assert "generate inc_c = round(income, 100)" in _lines(
        "round(income, to=100, into='inc_c')")


def test_prefix_is_stripped():
    assert "replace income = round(income, 50)" in _lines("apply_round(income, to=50)")
    assert "replace income = round(income, 50)" in _lines("apply-round(income, to=50)")


def test_multi_variable_list_applies_to_each():
    out = _lines("round([inntekt, formue, kostnad], to=1000)")
    assert "replace inntekt = round(inntekt, 1000)" in out
    assert "replace formue = round(formue, 1000)" in out
    assert "replace kostnad = round(kostnad, 1000)" in out


def test_function_applies_microdata_function():
    assert "replace lonn = round(lonn, 10000)" in _lines("function(lonn, round, 10000)")
    assert "replace lonn = int(lonn)" in _lines("function(lonn, int)")
    # accessible via the apply_ prefix too
    assert "replace dato = year(dato)" in _lines("apply_function(dato, year)")


def test_bin_quantile_default():
    assert "replace income = quantile(income, 10)" in _lines("bin(income)")
    assert "replace age = quantile(age, 4)" in _lines("bin(age, bins=4)")


def test_shorten():
    assert "replace icd = substr(icd, 1, 3)" in _lines("shorten(icd, keep=3)")


def test_eliminate_drops_rows():
    assert "drop if alder > 89" in _lines('eliminate(where="alder > 89")')
    # masking a cell to missing is now change(col, None, where=…), not eliminate
    assert "replace cost = . if cost > 1000000" in _lines(
        'change(cost, None, where="cost > 1000000")')


def test_seed_auto_offset_independent_per_op():
    # two seed-less random ops must NOT share a seed (else correlated draws)
    out = transform("noise(a, unit_id='pid', rounds=1)\n"
                    "noise(b, unit_id='pid', rounds=1)").script()
    assert "sample 0.5 0" in out and "sample 0.5 1000" in out


def test_session_seed_base():
    out = transform("noise(cost, scale=10, rounds=1, unit_id='pid')", seed=999).script()
    assert "sample 0.5 999" in out


def test_string_column_arg_accepted():
    assert "replace income = round(income, 50)" in _lines('round("income", to=50)')


def test_bin_equal_width():
    out = _lines("bin(alder, bins=5, method='equal_width')")
    assert any(l.startswith("aggregate (min) alder ->") for l in out)
    assert any("floor((alder -" in l for l in out)


def test_bin_start_stop_width_integer_bands():
    out = _lines("bin(alder, start=10, stop=80, width=5)")
    line = next(l for l in out if l.startswith("recode alder"))
    assert "(min/9 = 0)" in line          # bottom-open <10
    assert "(10/14 = 1)" in line          # integer band 10-14
    assert "(75/79 = 14)" in line
    assert "(80/max = 15)" in line        # top-open 80+
    assert not any("aggregate" in l for l in out)   # static, no runtime aggregate


def test_bin_labels_define_and_assign():
    out = _lines("bin(alder, start=10, stop=80, width=5, labels=True)")
    assert any(l.startswith("define-labels ") and '"10-14"' in l and '"<10"' in l
               and '"80+"' in l for l in out)
    assert any(l.startswith("assign-labels alder ") for l in out)


def test_bin_edges_uneven():
    out = _lines("bin(alder, edges=[0, 18, 67])")
    line = next(l for l in out if l.startswith("recode alder"))
    assert "(0/17 = 1)" in line and "(18/66 = 2)" in line and "(67/max = 3)" in line


def test_bin_width_only_aggregate_floor():
    out = _lines("bin(alder, width=5)")
    assert any(l.startswith("aggregate (min) alder ->") for l in out)
    assert any("floor((alder -" in l and "/ 5)" in l for l in out)


# ── recode: explicit microdata-notation pass-through ────────────────────────────

def test_recode_passthrough():
    out = _lines('recode(alder, "(min/9 = 0) (10/14 = 1) (* = 9)")')
    assert "recode alder (min/9 = 0) (10/14 = 1) (* = 9)" in out


def test_recode_with_labels():
    out = _lines('recode(kjonn, "(2 = 0)", labels={0: "kvinne", 1: "mann"})')
    assert "recode kjonn (2 = 0)" in out
    assert any(l.startswith("define-labels ") and '"kvinne"' in l for l in out)
    assert any(l.startswith("assign-labels kjonn ") for l in out)


def test_recode_missing_rules_warns():
    res = transform("recode(alder)")
    assert any("rules" in w for w in res.warnings)


# ── change: frequency-based & cross-variable conditional set ─────────────────────

def test_reduce_categories_absolute_count():
    out = _lines('reduce_categories(diagnose, min=5)')
    assert any(l.startswith("aggregate (count)") and "by(diagnose)" in l for l in out)
    assert any("replace diagnose = 'Other' if " in l and "< 5" in l for l in out)


def test_reduce_categories_decimal_is_proportion():
    out = _lines('reduce_categories(diagnose, min=0.01, to="Annen")')
    assert any("replace diagnose = 'Annen' if" in l and "< 0.01 *" in l for l in out)


def test_reduce_categories_to_missing():
    out = _lines("reduce_categories(diagnose, min=5, to=None)")
    assert any(l.startswith("replace diagnose = . if") for l in out)


def test_change_value_none_is_missing():
    out = _lines("change(inntekt, None, where=\"inntekt > 10000000\")")
    assert "replace inntekt = . if inntekt > 10000000" in out


def test_change_where_cross_variable():
    out = _lines('change(diagnose, "X", where="alder > 80")')
    assert out == ['// change(diagnose, "X", where="alder > 80")',
                   "replace diagnose = 'X' if alder > 80"]


def test_change_needs_where():
    res = transform('change(diagnose, "X")')
    assert any("where=" in w for w in res.warnings)


def test_microaggregate_replaces_with_group_stat():
    out = _lines("microaggregate(inntekt, by=kommune)")
    assert any(l.startswith("aggregate (mean) inntekt ->") and "by(kommune)" in l for l in out)
    assert any(l.startswith("replace inntekt =") for l in out)


def test_eliminate_min_drops_rare_records():
    out = _lines("eliminate(yrke, min=5)")
    assert any(l.startswith("aggregate (count)") and "by(yrke)" in l for l in out)
    assert any(l.startswith("drop if") and "< 5" in l for l in out)


# ── jitter: deterministic hash noise (Strategy 1) ───────────────────────────────

def test_jitter_uniform_adds_and_cleans_up():
    out = _lines("jitter(income, scale=50)")
    assert any("sin(" in l for l in out)                      # builds pseudo-uniform
    assert any(l == "replace income = income + (protect2micro_ua2 - 0.5) * 2 * 50"
               for l in out)
    assert out[-1].startswith("drop ")                        # temps cleaned


def test_jitter_normal_uses_box_muller():
    out = _lines("jitter(income, scale=50, dist='normal')")
    assert any("sqrt(-2*ln(" in l and "cos(2*pi()*" in l for l in out)


def test_jitter_auto_scale_uses_aggregate_range():
    out = _lines("jitter(age, key=['kjonn','bosted'])")       # scale defaults to 'auto'
    assert any(l.startswith("aggregate (min) age ->") for l in out)
    assert any(l.startswith("aggregate (max) age ->") for l in out)


# ── noise: genuine seeded noise via sample + merge (Strategy 2) ──────────────────

def test_noise_emits_sample_merge_rounds():
    out = _lines("noise(cost, scale=100, rounds=2, unit_id='PERSONID_1')")
    assert not any("clone-units" in l for l in out)            # clone-units is empty
    assert sum(1 for l in out if l.startswith("clone-dataset ")) == 3  # 1 source + 2 rounds
    assert any(l.startswith("generate protect2micro_mark") and l.endswith("= 1") for l in out)
    assert any(l.startswith("keep protect2micro_mark") for l in out)   # not the key var
    assert sum(1 for l in out if l.startswith("sample 0.5 ")) == 2
    assert sum(1 for l in out if "into main on PERSONID_1" in l) == 2
    assert any(l.startswith("recode ") and "(missing = 0)" in l for l in out)
    # adds scale * standardised normal draw, then cleans up
    assert any("replace cost = cost + 100*protect2micro_rand" in l for l in out)
    assert out[-1].startswith("drop ")


def test_noise_requires_unit_id():
    res = transform("noise(cost, scale=100)")     # no unit_id given
    assert any("unit_id" in w for w in res.warnings)


def test_noise_default_is_relative_5pct():
    # no scale → self-scaling relative noise: col * (1 + 0.05*z)
    out = _lines("noise(inntekt, unit_id='pid', rounds=4)")
    assert any(l.startswith("replace inntekt = inntekt * (1 + 0.05*") for l in out)


def test_noise_scale_gives_absolute_additive():
    out = _lines("noise(inntekt, scale=8000, unit_id='pid', rounds=4)")
    assert any(l.startswith("replace inntekt = inntekt + 8000*") for l in out)


def test_noise_pct_custom_relative():
    out = _lines("noise(inntekt, pct=0.1, unit_id='pid', rounds=4)")
    assert any("(1 + 0.1*" in l for l in out)


def test_noise_using_stays_additive_for_offsets():
    assert "replace visit = visit + 1*offset" in _lines("noise(visit, using=offset)")


def test_noise_default_is_centered_uniform_no_spike():
    # default dist uniform_sym → combine ends with '* 2 - 1' (centered, no 0-spike)
    out = _lines("noise(cost, scale=100, rounds=4, unit_id='pid')")
    assert any(l.startswith("generate protect2micro_rand") and l.endswith(") * 2 - 1")
               for l in out)


def test_noise_source_digits_is_cheap_one_liner():
    out = _lines("noise(inntekt, scale=8000, source='digits', from_col=kostnad, mod=100)")
    assert not any("clone-dataset" in l for l in out)          # no sample machinery
    assert "generate protect2micro_dig1 = kostnad - floor(kostnad / 100) * 100" in out
    assert any(l.endswith("(protect2micro_dig1 / 100) * 2 - 1") for l in out)
    assert "replace inntekt = inntekt + 8000*protect2micro_rand2" in out


def test_noise_source_digits_requires_from_col():
    res = transform("noise(inntekt, scale=100, source='digits')")
    assert any("from_col" in w for w in res.warnings)


def test_noise_source_hybrid_blends_sample_and_digits():
    out = _lines("noise(inntekt, scale=8000, source='hybrid', dist='normal', "
                 "rounds=4, unit_id='pid', from_col=kostnad)")
    assert any("clone-dataset" in l for l in out)              # has the sample part
    assert any("floor(kostnad / 100)" in l for l in out)       # has the digit part
    assert any("+ 0.3*protect2micro_rand" in l for l in out)   # blended with digit_weight


def test_draw_source_digits():
    out = _lines("draw(r, source='digits', from_col=maaling, mod=1000)")
    assert any("maaling - floor(maaling / 1000) * 1000" in l for l in out)
    assert any(l.startswith("generate r = ") for l in out)     # kept under the user's name


def test_noise_using_reuses_drawn_column():
    # one drawn column applied to two variables → correlated noise, no rebuild
    out = _lines("noise(inntekt, scale=50, using=z)")
    assert out == ["// noise(inntekt, scale=50, using=z)",
                   "replace inntekt = inntekt + 50*z"]


def test_noise_using_default_scale_is_one_for_date_shift():
    out = _lines("noise(visitdate, using=offset)")     # shift a date by a drawn offset
    assert "replace visitdate = visitdate + 1*offset" in out
    assert not any("clone-units" in l for l in out)     # reuses, doesn't build


def test_noise_unit_id_from_context():
    out = transform("noise(cost, scale=100)", unit_id="PERSONID_1").script()
    assert "into main on PERSONID_1" in out


# ── recode dict-form (absorbed the old collapse(mapping=)) ──────────────────────

def test_recode_dict_builds_rules():
    out = _lines("recode(sivilstand, {1: 0, 2: 0, 3: 1})")
    assert "recode sivilstand (1 = 0) (2 = 0) (3 = 1)" in out


def test_recode_dict_strings_quoted():
    assert "recode yrke ('a' = 'x')" in _lines("recode(yrke, {'a': 'x'})")


def test_collapse_verb_removed():
    res = transform("collapse(yrke, rare_below=5)")   # collapse no longer exists
    assert any("UNKNOWN verb: collapse" in l for l in res.microdata_lines)


# ── risk: k-anonymity measurement (Tier B, now implemented) ─────────────────────

def test_risk_builds_composite_key_and_summarizes():
    out = _lines("risk(quasi_ids=['sex', 'zip', 'age'])")
    assert any('= string(sex) ++ "_" ++ string(zip) ++ "_" ++ string(age)' in l for l in out)
    assert any(l.startswith("aggregate (count)") for l in out)
    assert sum(1 for l in out if l.startswith("summarize ")) == 2   # k + risk shares
    assert out[-1].startswith("drop ")


def test_risk_single_quasi_id():
    assert any("= string(kommune)" in l for l in _lines("risk(quasi_ids=kommune)"))


def test_risk_requires_quasi_ids():
    res = transform("risk()")
    assert any("quasi_ids required" in w for w in res.warnings)


def test_risk_sensitive_warns_l_diversity():
    res = transform("risk(quasi_ids=['sex', 'zip'], sensitive=['diag'])")
    assert any("l-diversity" in w for w in res.warnings)


def test_rng_uses_clone_dataset_not_clone_units_and_no_arrow():
    # clone-units yields an empty dataset (unusable); use clone-dataset, space-separated
    out = _lines("noise(cost, scale=10, rounds=1, unit_id='pid')")
    assert not any("clone-units" in l for l in out)
    assert any(l.startswith("clone-dataset main protect2micro_src") for l in out)
    assert not any(l.startswith("keep pid") for l in out)      # can't keep the key var
    assert any(l.startswith("keep protect2micro_mark") for l in out)
    assert not any("clone-dataset" in l and "->" in l for l in out)


# ── Tier C: still unsupported, explicit (never silent) ──────────────────────────

def test_pseudonymize_unsupported():
    res = transform("pseudonymize(pid)")
    assert any("UNSUPPORTED: pseudonymize" in l for l in res.microdata_lines)


def test_swap_unsupported():
    res = transform("swap(income)")
    assert any("UNSUPPORTED: swap" in l for l in res.microdata_lines)


# ── winsorize (Tier B, now implemented) ─────────────────────────────────────────

def test_winsorize_value_mode_removed():
    res = transform("winsorize(income, limits=(0, 1000000), method='value')")
    assert any("method='value' removed" in w for w in res.warnings)


def test_winsorize_percentile_p25_p75():
    out = _lines("winsorize(income, limits=(0.25, 0.75))")
    assert any(l.startswith("aggregate (p25) income ->") for l in out)
    assert any(l.startswith("aggregate (p75) income ->") for l in out)
    assert out[-1].startswith("drop ")


def test_winsorize_default_is_iqr():
    out = _lines("winsorize(inntekt)")        # bare call now valid: iqr (1.5,1.5)
    assert any(l.startswith("aggregate (p25) inntekt ->") for l in out)
    assert any(l.startswith("aggregate (p75) inntekt ->") for l in out)


def test_winsorize_arbitrary_percentile_warns_and_skips():
    res = transform("winsorize(income, method='percentile', limits=(0.01, 0.99))")
    assert any("0.01" in w for w in res.warnings)
    assert any("unavailable" in l for l in res.microdata_lines)


def test_winsorize_iqr_builds_fences():
    out = _lines("winsorize(income, limits=(1.5, 1.5), method='iqr')")
    assert any("= protect2micro_q3" in l and "- protect2micro_q1" in l for l in out)   # iqr = q3 - q1
    assert any("income < protect2micro_lo" in l for l in out)
    assert any("income > protect2micro_hi" in l for l in out)


def test_winsorize_gaussian_one_sided_top_code():
    out = _lines("winsorize(income, limits=(None, 3), method='gaussian')")
    assert any(l.startswith("aggregate (mean) income ->") for l in out)
    assert any(l.startswith("aggregate (sd) income ->") for l in out)
    assert not any("income <" in l for l in out)                 # no lower bound
    assert any("income >" in l for l in out)


def test_winsorize_mad_two_pass_median():
    out = _lines("winsorize(income, limits=(3, 3), method='mad')")
    assert sum(1 for l in out if l.startswith("aggregate (median)")) == 2
    assert any("abs(income -" in l for l in out)


def test_winsorize_by_group():
    out = _lines("winsorize(income, limits=(0.25, 0.75), by=kommune)")
    assert any(l.endswith(", by(kommune)") for l in out)


# ── diff (Tier B, now implemented) ──────────────────────────────────────────────

def test_diff_first_per_unit():
    out = transform("diff(visit, ref='first_per_unit', unit_id='pid')").script().splitlines()
    assert "aggregate (min) visit -> protect2micro_anchor1, by(pid)" in out
    assert "replace visit = visit - protect2micro_anchor1" in out
    assert out[-1].startswith("drop ")


def test_diff_min_population():
    out = _lines("diff(visit, ref='min')")
    assert any(l == "aggregate (min) visit -> protect2micro_anchor1" for l in out)  # no by()


def test_diff_fixed_date_literal():
    out = _lines("diff(visit, ref='2020-01-01')")
    assert "replace visit = visit - date(2020, 1, 1)" in out


def test_diff_pairwise_column_anchor():
    out = _lines("diff(discharge, ref='admission')")
    assert "replace discharge = discharge - admission" in out


def test_diff_years_unit_approximate():
    out = _lines("diff(visit, ref='min', unit='years')")
    assert any("/ 365.25" in l for l in out)


def test_diff_first_per_unit_requires_unit_id():
    res = transform("diff(visit, ref='first_per_unit')")   # explicit ref needs unit_id
    assert any("requires unit_id" in w for w in res.warnings)


def test_diff_default_ref_is_random_global():
    # A bare diff(x) — no ref, no unit_id — defaults to random_global.
    out = _lines("diff(utskrivning)")
    assert any(l.startswith("generate protect2micro_one") for l in out)
    assert any(l.startswith("aggregate (median) utskrivning -> protect2micro_med")
               and "by(protect2micro_one" in l for l in out)
    assert any(l.startswith("let protect2micro_shift") for l in out)
    assert any("utskrivning = utskrivning - (protect2micro_med" in l for l in out)
    assert not any(l.startswith("drop") and "shift" in l for l in out)   # key not dropped


def test_diff_random_global_stat_and_shift():
    out = _lines("diff(besoek, ref='random_global', stat='max', shift=137)")
    assert any(l.startswith("aggregate (max) besoek ->") for l in out)
    assert any(l.startswith("let ") and l.split("//")[0].rstrip().endswith("= 137") for l in out)


# ── diff generalized beyond dates: centering any numeric variable ───────────────

def test_diff_global_mean_centering_income():
    out = _lines("diff(inntekt, ref='mean')")
    assert any(l == "aggregate (mean) inntekt -> protect2micro_anchor1" for l in out)  # global, no by()
    assert "replace inntekt = inntekt - protect2micro_anchor1" in out


def test_diff_within_unit_demeaning():
    out = _lines("diff(inntekt, ref='mean_per_unit', unit_id='pid')")
    assert any(l == "aggregate (mean) inntekt -> protect2micro_anchor1, by(pid)" for l in out)


def test_diff_first_per_unit_is_min_per_unit_alias():
    out = _lines("diff(visit, ref='first_per_unit', unit_id='pid')")
    assert any(l.startswith("aggregate (min) visit ->") and "by(pid)" in l for l in out)


def test_diff_per_unit_requires_unit_id():
    res = transform("diff(inntekt, ref='median_per_unit')")
    assert any("requires unit_id" in w for w in res.warnings)


# ── draw: reusable random column (the general RNG primitive) ────────────────────

def test_draw_uniform_keeps_named_column():
    out = _lines("draw(r1, dist='uniform', rounds=3, unit_id='pid')")
    assert not any("clone-units" in l for l in out)
    assert sum(1 for l in out if l.startswith("clone-dataset ")) == 4  # 1 source + 3 rounds
    assert all("(missing = 0)" in l for l in out if l.startswith("recode "))
    # binary fraction lands in the user's column r1, which is NOT dropped
    assert any(l.startswith("generate r1 = ") and "*0.5" in l for l in out)
    assert out[-1].startswith("drop ") and "r1" not in out[-1]


def test_draw_normal_standardised():
    out = _lines("draw(z, dist='normal', rounds=4, unit_id='pid')")
    # mean=4*0.5=2, sd=sqrt(4*0.25)=1 (rendered without trailing .0)
    assert any(l.startswith("generate z = (") and "- 2) / 1" in l for l in out)


def test_draw_binomial_is_plain_sum():
    out = _lines("draw(b, dist='binomial', rounds=3, unit_id='pid')")
    assert any(l.startswith("generate b = protect2micro_bit") and "*" not in l.split("=")[1] for l in out)


def test_draw_requires_key():
    res = transform("draw(r, dist='uniform')")     # no unit_id / key
    assert any("merge key is required" in w for w in res.warnings)


def test_draw_row_level_merges_on_rowkey():
    out = _lines("draw(r, dist='uniform', rounds=2, level='row', key='rowkey')")
    assert any(l.startswith("clone-dataset main protect2micro_src") for l in out)
    assert any(l.startswith("keep protect2micro_mark") for l in out)   # marker, not the key
    assert sum(1 for l in out if "into main on rowkey" in l) == 2


def test_diff_date_parses_iso_string_then_diffs():
    out = _lines("diff_date(innleggelse, ref='min')")
    # first line parses the YYYY-MM-DD string into a date-value
    assert out[1] == ("replace innleggelse = date(to_int(substr(innleggelse, 1, 4)), "
                      "to_int(substr(innleggelse, 6, 2)), to_int(substr(innleggelse, 9, 2)))")
    # then the ordinary diff follows
    assert any(l.startswith("aggregate (min) innleggelse ->") for l in out)
    assert any(l == "replace innleggelse = innleggelse - protect2micro_anchor1" for l in out)


def test_diff_random_per_unit_builds_secret_key():
    out = transform("diff(visit, ref='random_per_unit', unit_id='pid', bits=3)").script().splitlines()
    # 3 bit-draws via clone-dataset (clone-units is empty), each merged on pid
    assert not any("clone-units" in l for l in out)
    assert sum(1 for l in out if "into main on pid" in l) == 3
    # recode-based bits, binary fraction, span, anchor = min + floor(u*span)
    assert any(l.startswith("recode protect2micro_bit") and "(missing = 0)" in l for l in out)
    assert any(l.startswith("generate protect2micro_rand") and "*0.5" in l for l in out)
    assert any("floor(protect2micro_rand" in l and "protect2micro_span" in l for l in out)


# ── Universal-arg honesty ───────────────────────────────────────────────────────

def test_share_on_value_transform_warns():
    res = transform("round(income, to=50, share=0.5)")
    assert any("share" in w for w in res.warnings)


# ── Multi-statement + comment trail ─────────────────────────────────────────────

def test_multi_statement_audit_trail():
    out = transform("coarsen(income, to=50)\nshorten(icd, keep=3)").script()
    assert "// coarsen(income, to=50)" in out
    assert "// shorten(icd, keep=3)" in out


def test_unknown_verb():
    res = transform("frobnicate(x)")
    assert any("UNKNOWN verb: frobnicate" in l for l in res.microdata_lines)


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))

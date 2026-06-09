# deid — verifiseringsskript for microdata.no

deid genererer microdata-kode basert på noen **uverifiserte antakelser**. Dette
dokumentet tester dem mot live microdata.

**Slik kjører du:** lim inn **én blokk om gangen** i microdata-editoren og kjør.
microdata stopper ved første feil, så blokkene er bevisst uavhengige (hver har
egen `require`/`import`). Noter for hver blokk om den **kjørte** og hva
`summarize`/`tabulate` viste.

**Juster ved behov:** databank-versjon (`no.ssb.fdb:54`), variabelnavn, og
person-ID (`PERSONID_1`) til det du har tilgang til.

Resultatene avgjør disse deid-funksjonene:

| Blokk | Tester | Gjelder deid-funksjon |
|---|---|---|
| 1 | `round`, `quantile`, `recode (missing/interval)` | `coarsen`, `bin`, kategorisk `collapse` |
| 2 | `aggregate` uten `by()` | global `diff`/`winsorize` (ellers konstant-nøkkel) |
| 3 | `clone-units`+`sample`+`merge`+`recode` | `draw`, `noise`, `diff(random_*)` — RNG-kjernen |
| 4 | `aggregate ... if` | sample-delmengde-statistikk (ekte `random_global` fra sample) |
| 5 | datovariabel: datoverdi vs streng | `diff` vs `diff_date`, `year`/`month` |
| 6 | `recode min/max` + `define-labels`/`assign-labels` | `bin(labels=…)` åpne ender + etiketter |

---

## Blokk 1 — byggeklosser (forventet OK)

```
require no.ssb.fdb:54 as fd
create-dataset deid_b1
import fd/INNTEKT_WLONN 2022-01-01 as inntekt

generate b1_round = round(inntekt, 50000)        // coarsen
generate b1_bin   = quantile(inntekt, 10)        // bin (desiler 0..9)
generate b1_rec   = inntekt
recode b1_rec (0/300000 = 0) (nonmissing = 1) (missing = 0)
summarize b1_round b1_bin b1_rec
```

**Forventet:** kjører; `b1_bin` i 0–9, `b1_rec` i {0,1}. → `coarsen`/`bin`/`recode` OK.

---

## Blokk 2 — `aggregate` uten `by()` (populasjonsnivå)

Avgjør om vi kan skrive `aggregate (mean) x -> m` direkte, eller må bruke
konstant-nøkkel-trikset.

```
require no.ssb.fdb:54 as fd
create-dataset deid_b2
import fd/INNTEKT_WLONN 2022-01-01 as inntekt

aggregate (mean) inntekt -> b2_pop_mean
summarize b2_pop_mean
```

**Forventet OK:** `b2_pop_mean` lik på alle rader (populasjonsgjennomsnittet).
**Hvis FEIL:** tom `by()` støttes ikke → deid må bruke fallback (test Blokk 2b).

### Blokk 2b — fallback med konstant nøkkel (hvis 2 feilet)

```
require no.ssb.fdb:54 as fd
create-dataset deid_b2b
import fd/INNTEKT_WLONN 2022-01-01 as inntekt

generate deid_one = 1
aggregate (mean) inntekt -> b2b_mean, by(deid_one)
summarize b2b_mean
```

**Forventet:** kjører. Hvis ja, bytter deid globale aggregater til dette mønsteret.

---

## Blokk 3 — RNG-kjernen: `clone-dataset` + `keep` + `sample` + `merge` + `recode`

Den viktigste antakelsen. Bygger ett tilfeldig bit per enhet. **Merk:**
`clone-units` gir et *tomt* datasett (kan ikke `sample`/`generate`), og man kan
ikke `keep` id-en direkte (nøkkelvariabel, beholdes automatisk). Derfor: generér
en markør (`=1`), `clone-dataset`, `keep` markøren (id-en følger med).

```
require no.ssb.fdb:54 as fd
create-dataset deid_b3
import fd/BEFOLKNING_KJOENN as kjonn

generate deid_marker = 1
clone-dataset deid_b3 deid_src
use deid_src
keep deid_marker
clone-dataset deid_src deid_round
use deid_round
sample 0.5 12345
generate deid_bit = 1
merge deid_bit into deid_b3 on PERSONID_1
use deid_b3
recode deid_bit (missing = 0)
summarize deid_bit
delete-dataset deid_round
delete-dataset deid_src
drop deid_marker
```

**Forventet OK:** `deid_bit` i {0,1} med gjennomsnitt **~0.5**. → hele `draw`/`noise`/
`diff(random_*)`-maskineriet virker.
**Sjekk særlig:** (a) at `keep deid_marker` beholder markøren + id-en automatisk,
(b) at `sample` virker på det krympede datasettet, (c) at `merge ... on PERSONID_1`
finner nøkkelen og at `recode (missing = 0)` setter ikke-trukne til 0,
(d) at `delete-dataset` og `drop deid_marker` virker.
**Hvis FEIL:** noter på hvilken linje — det forteller oss hvilket ledd som svikter.

### Blokk 3b — er `main` én rad per enhet? (avgjør per-enhet vs per-rad)

```
require no.ssb.fdb:54 as fd
create-dataset deid_b3b
import fd/BEFOLKNING_KJOENN as kjonn
generate deid_one = 1
aggregate (count) deid_one -> deid_rows_per_unit, by(PERSONID_1)
summarize deid_rows_per_unit
```

**Forventet:** maks = 1 → datasettet er én rad per person, og sample-på-rad =
sample-på-enhet (RNG-konstruksjonen er korrekt per enhet). Hvis maks > 1
(paneldata) må kilden kollapses til enhetsnivå før sample.

---

## Blokk 4 — `aggregate ... if` (bonus: ekte sample-delmengde-statistikk)

Hvis dette virker, kan `random_global` hente min/max/median fra en *tilfeldig
delmengde* (ikke bare hele populasjonen + hemmelig offset).

```
require no.ssb.fdb:54 as fd
create-dataset deid_b4
import fd/INNTEKT_WLONN 2022-01-01 as inntekt

generate b4_sub = 1 * (inntekt > 300000)
aggregate (median) inntekt -> b4_med if b4_sub == 1
summarize b4_med
```

**Forventet:** usikkert — `aggregate`-syntaksen har ingen dokumentert `if`.
**Hvis OK:** åpner for sample-delmengde-anker. **Hvis FEIL:** bekrefter at
hemmeligheten i `random_global` må komme fra `let shift`, ikke fra sampling.

---

## Blokk 5 — datovariabel: datoverdi eller `YYYY-MM-DD`-streng?

Avgjør når man trenger `diff` (datoverdi) vs `diff_date` (streng), og om
`year()`/subtraksjon virker direkte.

```
require no.ssb.fdb:54 as fd
create-dataset deid_b5
import fd/BEFOLKNING_FOEDEAAR as foedeaar
import fd/BEFOLKNING_FOEDEDATO as foededato

generate b5_aar  = year(foededato)        // virker dato-funksjoner direkte?
generate b5_diff = foededato - 0          // er verdien et heltall (dager)?
summarize b5_aar foedeaar b5_diff
```

**Forventet hvis datoverdi:** `b5_aar` ≈ `foedeaar` (fornuftige årstall), og
`b5_diff` = store negative/positive heltall (dager siden 1970). → bruk `diff`.
**Hvis FEIL/rart:** `foededato` er trolig en streng → bruk `diff_date`; test parsing:

### Blokk 5b — `diff_date` sin parse-vei (kun hvis 5 feilet)

```
require no.ssb.fdb:54 as fd
create-dataset deid_b5b
import fd/BEFOLKNING_FOEDEDATO as foededato

generate b5b_val = date(to_int(substr(foededato, 1, 4)), to_int(substr(foededato, 6, 2)), to_int(substr(foededato, 9, 2)))
summarize b5b_val
```

**Forventet:** `b5b_val` = heltall (dager siden 1970). → `diff_date`-parsingen virker.
**Ekstra (type-endring in-place):** prøv `replace foededato = date(to_int(substr(foededato,1,4)), to_int(substr(foededato,6,2)), to_int(substr(foededato,9,2)))`.
Hvis microdata nekter å endre en streng-variabel til datoverdi via `replace`, må
`diff_date` skrive til en **ny** variabel i stedet.

---

## Etterpå

Send meg for hver blokk: **kjørte den?** og hva `summarize`/`tabulate` viste
(særlig gjennomsnitt for Blokk 3, og om `b5_aar ≈ foedeaar` i Blokk 5). Da
oppdaterer jeg deid: fjerner «uverifisert»-forbeholdene, bytter eventuelt globale
aggregater til konstant-nøkkel, og fikser `diff_date` hvis type-endring ikke går.
```

---

## Blokk 6 — `recode min/max` + etiketter (åpne bånd i `bin(labels=…)`)

`bin(start=, stop=, width=, labels=True)` antar at `recode` godtar `min`/`max` for
de åpne endene, og bruker `define-labels`/`assign-labels` for lesbare bånd.

```
require no.ssb.fdb:54 as fd
create-dataset deid_b6
import fd/BEFOLKNING_FOEDEAAR as foedeaar
generate alder = 2024 - foedeaar

recode alder (min/9 = 0) (10/19 = 1) (20/64 = 2) (65/max = 3)
define-labels alder_band 0 "<10" 1 "10-19" 2 "20-64" 3 "65+"
assign-labels alder alder_band
tabulate alder
```

**Forventet OK:** `tabulate` viser båndene med etikettene «<10 / 10-19 / 20-64 / 65+».
**Sjekk særlig:** (a) at `recode` godtar `min/…` og `…/max` (åpne intervaller),
(b) at `define-labels`/`assign-labels` virker og vises i `tabulate`.
**Hvis `min`/`max` FEILER:** vi faller tilbake til clip-varianten (de åpne endene
smelter inn i ytterbåndene). **Hvis etiketter feiler:** vi gir bare tallkoder.

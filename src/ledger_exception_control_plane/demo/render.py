"""Turn a :class:`~.snapshot.PipelineSnapshot` into one standalone HTML page.

Presentation only. Every number here is read off the snapshot; nothing is recomputed, and no rule
from matching, classification or the calculator is restated. A guard test walks this package's AST
to keep it that way, because a report that decides anything is a second implementation of the thing
it claims to be reporting on.

**Two sections, kept apart on the page as they are in the data.** *What the pipeline did* is
everything a running system would know about itself. *Fixture evaluation* is a comparison with what
each synthetic case was constructed to be, which only a generated corpus can know — labelled as such
so nobody reads a demo as a production metric.

**Deterministic output.** No clock, no paths, no environment. The same profile and seed render
byte-identical HTML, which is what lets a committed copy be checked for drift.

Zero dependencies: one file, embedded CSS, no JavaScript, no images. It opens from disk.
"""

from __future__ import annotations

import html
from typing import Final

from ledger_exception_control_plane.demo.snapshot import PipelineSnapshot

#: The command that regenerates this page. Shown on the page so a reader can reproduce it.
REGENERATE_COMMAND: Final = "make m2-demo"

_STAGES: Final = (
    ("Settlement", "a PSP file arrives"),
    ("Normalise", "typed, or quarantined"),
    ("Match", "exact, then tolerance"),
    ("Residual", "what did not match"),
    ("Classify", "what can be proved"),
    ("Price", "or safely refuse"),
)

#: Statements about the system, each one a property something in the repository enforces.
_CONTROLS: Final = (
    (
        "Money is <code>Decimal</code>, never <code>float</code>",
        "asserted across every money path",
    ),
    (
        "Over-precise input is rejected, not rounded",
        "at ingestion and again in the calculator",
    ),
    ("Ambiguity is refused, never guessed", "a contested match settles nothing"),
    (
        "A class needs evidence that distinguishes it",
        "direction alone is not evidence",
    ),
    ("Unsupported currency is refused", "no conversion exists anywhere"),
    ("No AI is involved", "there is no model in this codebase yet"),
    ("Nothing is posted to a ledger", "and no adjustment row is written"),
)

_CSS: Final = """
:root {
  --bg: #0f1115; --panel: #171a21; --panel-2: #1d212a; --line: #2a2f3a;
  --ink: #e6e9ef; --muted: #98a1b3; --accent: #6ea8fe;
  --good: #4ec9a5;
  --warn: #e0a458; --stop: #d1707a; --truth: #b892e8;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 40px 24px 64px; background: var(--bg); color: var(--ink);
  font: 15px/1.6 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 1080px; margin: 0 auto; }
h1 { font-size: 28px; margin: 0 0 4px; letter-spacing: -0.02em; }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing: 0.08em;
     color: var(--muted); margin: 40px 0 14px; font-weight: 600; }
h3 { font-size: 14px; margin: 0 0 10px; color: var(--ink); font-weight: 600; }
p  { margin: 0 0 12px; color: var(--muted); }
code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.92em; }
.sub { color: var(--muted); font-size: 14px; margin-bottom: 18px; }
.meta { display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0 8px; }
.chip { background: var(--panel-2); border: 1px solid var(--line); border-radius: 999px;
        padding: 4px 12px; font-size: 12.5px; color: var(--muted); }
.chip b { color: var(--ink); font-weight: 600; }
.flow { display: flex; flex-wrap: wrap; gap: 8px; align-items: stretch; margin: 8px 0 4px; }
.step { flex: 1 1 150px; background: var(--panel); border: 1px solid var(--line);
        border-radius: 10px; padding: 12px 14px; position: relative; }
.step .n { font-size: 11px; color: var(--accent); font-weight: 700; letter-spacing: 0.06em; }
.step .t { font-weight: 600; margin-top: 2px; }
.step .d { font-size: 12.5px; color: var(--muted); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px;
        padding: 16px 18px; }
.card.truth { border-color: #3b2f52; background: #191526; }
.stat { display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
        padding: 5px 0; border-bottom: 1px dashed var(--line); }
.stat:last-child { border-bottom: 0; }
.stat .k { color: var(--muted); font-size: 13.5px; }
.stat .v { font-variant-numeric: tabular-nums; font-weight: 600; }
.big { font-size: 30px; font-weight: 700; line-height: 1.1; letter-spacing: -0.02em; }
.big.good { color: var(--good); } .big.warn { color: var(--warn); }
.big.stop { color: var(--stop); }
.cap { font-size: 12.5px; color: var(--muted); }
.bar { display: flex; height: 12px; border-radius: 6px; overflow: hidden;
       background: var(--panel-2); margin: 10px 0 6px; }
.bar span { display: block; height: 100%; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12.5px; color: var(--muted); }
.legend i { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 6px; }
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: 12px; text-transform: uppercase;
     letter-spacing: 0.05em; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11.5px;
       border: 1px solid var(--line); color: var(--muted); }
.tag.ok { color: var(--good); border-color: #2c4d43; }
.tag.no { color: var(--warn); border-color: #4d3f2c; }
.controls { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 10px; }
.ctl { display: flex; gap: 10px; align-items: flex-start; background: var(--panel);
       border: 1px solid var(--line); border-radius: 10px; padding: 11px 14px; }
.ctl .m { color: var(--good); font-weight: 700; line-height: 1.5; }
.ctl .x { font-size: 13.5px; }
.ctl .x small { display: block; color: var(--muted); font-size: 12.5px; }
.note { border-left: 3px solid var(--line); padding: 2px 0 2px 14px; color: var(--muted);
        font-size: 13.5px; margin: 14px 0; }
.note.truth { border-left-color: var(--truth); }
footer { margin-top: 44px; padding-top: 18px; border-top: 1px solid var(--line);
         color: var(--muted); font-size: 12.5px; }
"""


def _e(value: object) -> str:
    """Escape anything interpolated into the page. Every value below goes through this."""
    return html.escape(str(value), quote=True)


def _pct(part: int, whole: int) -> str:
    return "0%" if whole == 0 else f"{100 * part / whole:.1f}%"


def _stat(key: str, value: object) -> str:
    return (
        f'<div class="stat"><span class="k">{_e(key)}</span>'
        f'<span class="v">{_e(value)}</span></div>'
    )


def _rows(counts: dict[str, int], total: int) -> str:
    body = "".join(
        f"<tr><td><code>{_e(name)}</code></td><td class='num'>{_e(count)}</td>"
        f"<td class='num'>{_e(_pct(count, total))}</td></tr>"
        for name, count in counts.items()
    )
    return (
        "<table><thead><tr><th>Outcome</th><th class='num'>Count</th>"
        f"<th class='num'>Share</th></tr></thead><tbody>{body}</tbody></table>"
    )


def _segments(parts: list[tuple[str, int, str]], total: int) -> str:
    """A stacked bar plus its legend. Pure CSS — no chart library, no script."""
    if total == 0:
        return ""
    bar = "".join(
        f'<span style="width:{100 * count / total:.4f}%;background:{colour}"></span>'
        for _label, count, colour in parts
        if count
    )
    legend = "".join(
        f'<span><i style="background:{colour}"></i>{_e(label)} {_e(count)}</span>'
        for label, count, colour in parts
    )
    return f'<div class="bar">{bar}</div><div class="legend">{legend}</div>'


def render(snapshot: PipelineSnapshot) -> str:
    """Render the whole page. Deterministic: same snapshot in, same bytes out."""
    s = snapshot
    flow = "".join(
        f'<div class="step"><div class="n">{index}</div>'
        f'<div class="t">{_e(title)}</div><div class="d">{_e(detail)}</div></div>'
        for index, (title, detail) in enumerate(_STAGES, start=1)
    )

    matching_bar = _segments(
        [
            ("exact", s.matching.exact, "#4ec9a5"),
            ("tolerance", s.matching.tolerance, "#6ea8fe"),
            ("ambiguous", s.matching.ambiguous, "#e0a458"),
            ("residual", s.matching.unmatched, "#d1707a"),
        ],
        s.matching.considered,
    )
    class_bar = _segments(
        [
            ("fee_split", s.classification.by_class.get("fee_split", 0), "#4ec9a5"),
            (
                "chargeback_reversal",
                s.classification.by_class.get("chargeback_reversal", 0),
                "#6ea8fe",
            ),
            (
                "cross_period_refund",
                s.classification.by_class.get("cross_period_refund", 0),
                "#b892e8",
            ),
            ("unclassified", s.classification.by_class.get("unclassified", 0), "#59606e"),
        ],
        s.classification.residuals,
    )
    calc_bar = _segments(
        [
            ("priced", s.calculator.priced, "#4ec9a5"),
            ("refused", s.calculator.considered - s.calculator.priced, "#59606e"),
        ],
        s.calculator.considered,
    )

    controls = "".join(
        f'<div class="ctl"><div class="m">✓</div><div class="x">{claim}'
        f"<small>{_e(note)}</small></div></div>"
        for claim, note in _CONTROLS
    )

    examples = "".join(
        f"<tr><td><span class='tag'>{_e(x.stage)}</span></td>"
        f"<td><code>{_e(x.reference)}</code></td><td>{_e(x.detail)}</td>"
        f"<td>{_e(x.outcome)}</td></tr>"
        for x in s.examples
    )

    truth = s.ground_truth
    entries_consumed = f"{s.matching.entries_consumed} of {s.matching.entries_available}"
    unclassified = s.classification.by_class.get("unclassified", 0)
    coverage = _pct(s.classification.classified, s.classification.residuals)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>M2 Pipeline Snapshot — ledger-exception-control-plane</title>
<style>{_CSS}</style></head><body><div class="wrap">

<h1>Ledger Exception Control Plane</h1>
<div class="sub">M2 pipeline snapshot — the deterministic core, end to end,
with no model call.</div>
<div class="meta">
  <span class="chip">profile <b>{_e(s.profile)}</b></span>
  <span class="chip">seed <b>{_e(s.seed)}</b></span>
  <span class="chip">instances <b>{_e(s.instances)}</b></span>
  <span class="chip">settlement lines <b>{_e(s.ingestion.lines_parsed)}</b></span>
  <span class="chip">books <b>{_e(s.functional_currency)}</b></span>
  <span class="chip">ledger config <b>{_e(s.ledger_context_version)}</b></span>
</div>
<p>Every number below is produced by running the real M2 boundaries over a synthetic corpus
generated from that seed. Regenerate with <code>{_e(REGENERATE_COMMAND)}</code>.</p>

<h2>Pipeline</h2>
<div class="flow">{flow}</div>

<h2>What the pipeline did</h2>

<div class="grid">
  <div class="card">
    <h3>Ingestion</h3>
    {_stat("Settlement files", s.ingestion.batches)}
    {_stat("Rows offered", s.ingestion.lines_offered)}
    {_stat("Rows normalised", s.ingestion.lines_parsed)}
    {_stat("Invalid files quarantined", s.ingestion.batches_quarantined)}
    <p class="cap" style="margin-top:10px">One bad row condemns a whole file, so a quarantine is
    counted per file. Persistence is not exercised here — this runs without a database.</p>
  </div>
  <div class="card">
    <h3>Quarantine reasons</h3>
    {_rows(s.ingestion.quarantine_reasons, s.ingestion.batches_quarantined)}
    <p class="cap" style="margin-top:10px">From the deliberately invalid artifacts committed beside
    the corpus. A quarantine path never exercised is a claim, not a behaviour.</p>
  </div>
</div>

<div class="grid" style="margin-top:12px">
  <div class="card">
    <h3>Matching</h3>
    {matching_bar}
    {_stat("Lines considered", s.matching.considered)}
    {_stat("Matched exactly", s.matching.exact)}
    {_stat("Matched within tolerance", s.matching.tolerance)}
    {_stat("Ambiguous — refused", s.matching.ambiguous)}
    {_stat("Residual", s.matching.unmatched)}
    {_stat("Ledger entries consumed", entries_consumed)}
  </div>
  <div class="card">
    <h3>Classification</h3>
    {class_bar}
    {_stat("Residuals", s.classification.residuals)}
    {_stat("Given a class", s.classification.classified)}
    {_stat("Unclassified — safe fallback", unclassified)}
    {_stat("Coverage", coverage)}
    <p class="cap" style="margin-top:10px">Coverage is the secondary number. A wrong class is the
    first step of a wrong posting; an unclassified residual is a decision a human makes.</p>
  </div>
</div>

<div class="grid" style="margin-top:12px">
  <div class="card">
    <h3>Calculator</h3>
    {calc_bar}
    {_stat("Exceptions considered", s.calculator.considered)}
    {_stat("Priced", s.calculator.priced)}
    {_stat("Refused", s.calculator.considered - s.calculator.priced)}
    <p class="cap" style="margin-top:10px">Priced under treatment
    <code>{_e(s.treatment)}</code>, supplied by this demo. Nothing in the system chooses a treatment
    yet — that is a later milestone — and no adjustment is written anywhere.</p>
  </div>
  <div class="card">
    <h3>Why the rest were refused</h3>
    {_rows(s.calculator.refused_by_reason, s.calculator.considered)}
    <p class="cap" style="margin-top:10px">Closed reason codes. Refusing is the designed outcome
    when the evidence cannot support an instruction.</p>
  </div>
</div>

<h2>Controls</h2>
<div class="controls">{controls}</div>

<h2>Representative cases</h2>
<div class="card">
<table><thead><tr><th>Stage</th><th>PSP reference</th><th>Movement</th><th>Outcome</th></tr></thead>
<tbody>{examples}</tbody></table>
<p class="cap" style="margin-top:10px">Chosen by outcome — the first of each the pipeline produced —
rather than hand-picked. All references and amounts are synthetic.</p>
</div>

<h2>Fixture evaluation — ground truth</h2>
<div class="note truth">This section is <b>not</b> something a running system can know. Every
case in the corpus records what it was <i>constructed</i> to be, so the pipeline's answers can be
graded against it. The production code is handed types with no field for any of it.</div>
<div class="grid">
  <div class="card truth">
    <div class="big good">{_e(truth.false_matches)}</div>
    <div class="cap">false financial matches — a line paired with another case's ledger entry</div>
  </div>
  <div class="card truth">
    <div class="big good">{_e(truth.classifications_wrong)}</div>
    <div class="cap">wrong classifications — a class other than the one constructed</div>
  </div>
  <div class="card truth">
    <div class="big good">{_e(truth.wrong_financial_instructions)}</div>
    <div class="cap">wrong financial instructions — wrong amount, account or period</div>
  </div>
</div>
<div class="grid" style="margin-top:12px">
  <div class="card truth">
    <h3>Classification, graded</h3>
    {_stat("Correct", truth.classifications_correct)}
    {_stat("Under-classified — fell back safely", truth.classifications_under)}
    {_stat("Wrong", truth.classifications_wrong)}
    {_stat("No declared intent in the corpus", truth.classifications_no_intent)}
    <p class="cap" style="margin-top:10px">Under-classified means the corpus intended a class
    and the pipeline said <code>unclassified</code>: safe, and why coverage is not higher.</p>
  </div>
  <div class="card truth">
    <h3>Priced instructions, by account</h3>
    {_rows(s.calculator.priced_by_account, s.calculator.priced)}
    <p class="cap" style="margin-top:10px">Accounts are synthetic demo configuration for a fictional
    organisation, not a real chart of accounts.</p>
  </div>
</div>

<footer>
Static snapshot for developers and portfolio readers — <b>not</b> the operations console, which is a
later milestone. Generated from profile <code>{_e(s.profile)}</code> at seed
<code>{_e(s.seed)}</code> with <code>{_e(REGENERATE_COMMAND)}</code>; the same seed renders the same
page. No AI is involved anywhere in this pipeline and nothing here is posted to a ledger.
</footer>

</div></body></html>
"""

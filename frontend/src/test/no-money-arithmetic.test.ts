import { describe, expect, it } from "vitest";

import {
  NUMERIC_COERCION_ALLOWED,
  applicationSources,
  findMoneyArithmetic,
  stripComments,
} from "@/test/source-scan";

/**
 * The console must never do arithmetic on a monetary value.
 *
 * The rule is not stylistic. Every amount in this system is computed once, by deterministic typed
 * Python, with explicit quantisation and an explicit rounding mode, and is returned as a string
 * beside its currency. A browser that adds two of those, converts one, or re-formats one with
 * `toFixed` has produced a *second* amount that no audit event covers and no test constrains — and
 * a reviewer reading the screen has no way to tell which of the two they are looking at.
 *
 * So the guard scans the shipped source for arithmetic applied to money-named identifiers, for
 * numeric coercion of any kind, and for aggregation. It is checked against samples that must fail
 * as well as the tree that must pass: a scanner nobody has watched reject anything is a comment.
 */
describe("the console performs no arithmetic on monetary values", () => {
  it("finds no violation in the shipped source", () => {
    const violations = findMoneyArithmetic(applicationSources());

    expect(
      violations.map((violation) => `${violation.path}:${violation.line} — ${violation.rule}\n    ${violation.text}`),
    ).toEqual([]);
  });

  it("scans a non-trivial number of files, so an empty result means something", () => {
    // A scanner pointed at nothing passes. This pins the tree it actually reads.
    expect(applicationSources().length).toBeGreaterThan(15);
  });

  it("allowlists exactly one module for numeric coercion, and it handles no money", () => {
    expect([...NUMERIC_COERCION_ALLOWED]).toHaveLength(1);

    const config = applicationSources().find((file) =>
      NUMERIC_COERCION_ALLOWED.has(file.path),
    );
    expect(config, "the allowlisted module must exist").toBeDefined();

    // The allowlist is only safe while the file it exempts never touches an amount. Checked over
    // the code rather than the prose: the module's own docstring explains why it handles none.
    const code = stripComments(config?.source ?? "").toLowerCase();
    expect(code).not.toMatch(/\bamount\b|\bcurrency\b|\badjustment\b/);
  });

  describe("positive controls — each of these must be caught", () => {
    const cases: { name: string; source: string; }[] = [
      { name: "adding two amounts", source: "const t = a.amount + b.amount;" },
      { name: "scaling an amount", source: "const half = adjustment.amount * 0.5;" },
      { name: "dividing an amount", source: "const unit = amount / count;" },
      { name: "compound assignment", source: "let total = '0'; total += row.amount;" },
      { name: "totalling a column", source: "const total = rows.reduce((a, r) => a + 1, 0);" },
      { name: "parsing an amount", source: "const n = parseFloat(detail.adjustment.amount);" },
      { name: "coercing with Number", source: "const n = Number(row.amount);" },
      { name: "re-rounding an amount", source: "const shown = value.toFixed(2);" },
      { name: "locale-formatting money", source: "new Intl.NumberFormat('de-DE').format(1);" },
      { name: "percentage of an amount", source: "const pct = amount * 100;" },
    ];

    for (const sample of cases) {
      it(sample.name, () => {
        const violations = findMoneyArithmetic([
          { path: "src/components/sample.tsx", source: sample.source },
        ]);
        expect(violations.length, `not caught: ${sample.source}`).toBeGreaterThan(0);
      });
    }
  });

  describe("negative controls — legitimate code must not be flagged", () => {
    const cases: { name: string; source: string }[] = [
      { name: "rendering the amount string", source: "return <span>{row.amount}</span>;" },
      { name: "comparing an amount to null", source: "if (amount === null) return null;" },
      { name: "passing the amount as a prop", source: "<Money amount={a.amount} currency={a.currency} />" },
      { name: "prose in a comment about arithmetic", source: "// never sum amount + amount here" },
      { name: "an integer field that is not money", source: "const n = attempt.attempt_no + 1;" },
    ];

    for (const sample of cases) {
      it(sample.name, () => {
        const violations = findMoneyArithmetic([
          { path: "src/components/sample.tsx", source: sample.source },
        ]);
        expect(violations, `wrongly flagged: ${sample.source}`).toEqual([]);
      });
    }
  });
});

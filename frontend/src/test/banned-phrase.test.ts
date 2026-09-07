import { describe, expect, it } from "vitest";

import { bannedPhrases, committedText, findBannedPhrase } from "@/test/source-scan";

/**
 * The overclaim this project refuses to make must appear nowhere in the console.
 *
 * The correct phrase is *effectively-once effect*, and it is conditional: it may be claimed only
 * where the ledger adapter declares that it enforces the idempotency key, or that it can be queried
 * by operation identifier, and only with a retry-independent operation identifier. Where the
 * adapter does not meet that bar the claim is withdrawn rather than reworded.
 *
 * The stronger phrase is simply false — the transactional outbox is at-least-once, and duplicate
 * *dispatch* prevention is bounded by what the ledger can tell us — so it is banned in code,
 * comments, copy and documentation alike. A reviewer who finds it stops reading, and they are right
 * to.
 */
describe("the console never makes the stronger delivery claim", () => {
  it("does not contain the banned phrase in any committed text file", () => {
    const violations = findBannedPhrase(committedText());

    expect(
      violations.map((violation) => `${violation.path}:${violation.line} — ${violation.text}`),
    ).toEqual([]);
  });

  it("scans the whole frontend tree, including documentation and copy", () => {
    const scanned = committedText().map((file) => file.path);

    expect(scanned.length).toBeGreaterThan(20);
    expect(scanned).toContain("README.md");
    expect(scanned).toContain("openapi.json");
    expect(scanned.some((path) => path.endsWith(".tsx"))).toBe(true);
    expect(scanned.some((path) => path.endsWith(".css"))).toBe(true);
  });

  it("catches the phrase in each of its spellings", () => {
    for (const phrase of bannedPhrases()) {
      const violations = findBannedPhrase([
        { path: "sample.md", source: `We guarantee ${phrase} delivery.` },
      ]);
      expect(violations, `not caught: ${phrase}`).toHaveLength(1);
    }
  });

  it("catches it regardless of case", () => {
    const [hyphenated] = bannedPhrases();
    const violations = findBannedPhrase([
      { path: "sample.md", source: `EXACTLY${hyphenated?.slice("exactly".length) ?? ""} SEMANTICS` },
    ]);
    expect(violations).toHaveLength(1);
  });

  it("permits the phrase the project actually claims", () => {
    const violations = findBannedPhrase([
      {
        path: "sample.md",
        source: "An effectively-once effect, conditional on the declared adapter capability.",
      },
    ]);
    expect(violations).toEqual([]);
  });
});

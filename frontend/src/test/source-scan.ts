/**
 * Source scanners used by the guard tests.
 *
 * Kept in one module so the guards and their *positive controls* run the same code: a scanner
 * nobody has seen reject anything is a comment, so `no-money-arithmetic.test.ts` feeds it samples
 * that must fail as well as the real tree that must pass.
 */

import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative, sep } from "node:path";

/**
 * `frontend/` — the root of everything the guards scan.
 *
 * Taken from the working directory rather than from `import.meta.url`, because the test runner
 * serves modules over a non-file URL and `fileURLToPath` refuses those. Asserted rather than
 * assumed: a guard silently pointed at the wrong directory scans nothing and passes.
 */
export const FRONTEND_ROOT = process.cwd();

if (!existsSync(join(FRONTEND_ROOT, "package.json"))) {
  throw new Error(`the source guards expect to run from frontend/, not ${FRONTEND_ROOT}`);
}

const SKIP_DIRECTORIES = new Set(["node_modules", ".next", ".git", "out", "coverage"]);

export function walk(root: string, keep: (path: string) => boolean): string[] {
  const found: string[] = [];
  const visit = (directory: string) => {
    for (const entry of readdirSync(directory)) {
      if (SKIP_DIRECTORIES.has(entry)) continue;
      const path = join(directory, entry);
      if (statSync(path).isDirectory()) visit(path);
      else if (keep(path)) found.push(path);
    }
  };
  visit(root);
  return found;
}

/** Every application source file: the console as it ships. Tests and fixtures excluded. */
export function applicationSources(): { path: string; source: string }[] {
  const testDirectory = `src${sep}test${sep}`;
  return walk(join(FRONTEND_ROOT, "src"), (path) => /\.tsx?$/.test(path))
    .filter((path) => !relative(FRONTEND_ROOT, path).startsWith(testDirectory))
    .map((path) => ({ path: relative(FRONTEND_ROOT, path), source: readFileSync(path, "utf8") }));
}

/**
 * Remove line and block comments.
 *
 * The guards run over code, not over the prose explaining it — this file's own neighbours discuss
 * cross-currency arithmetic and re-pricing at length, and a scanner that read those sentences as
 * code would be unusable and would then be switched off. String and template literals are *kept*,
 * because a template literal is where an interpolated expression would hide.
 */
export function stripComments(source: string): string {
  // Block comments are blanked rather than deleted so line numbers in a violation still point at
  // the offending line. The `[^:]` guard keeps `https://` from being read as a line comment.
  return source
    .replace(/\/\*[\s\S]*?\*\//g, (comment) => comment.replace(/[^\n]/g, " "))
    .replace(/(^|[^:])\/\/[^\n]*/g, "$1");
}

// ---------------------------------------------------------------------------------------------
// Guard 1 — no arithmetic on monetary values
// ---------------------------------------------------------------------------------------------

/**
 * Identifier and property names that hold money in this domain.
 *
 * Deliberately short. `fee` and `price` were candidates and were dropped: they appear in prose as
 * "fee_split" and "re-price", and a guard with false positives is a guard somebody weakens.
 */
const MONEY_NAME = String.raw`(?:amount|amounts|total|totals|subtotal|subtotals|balance|balances|money)`;

const ARITHMETIC_OPERATOR = String.raw`(?:\+\+|--|\*\*|[+\-*/%])`;

/** Numeric coercion. Anywhere in the console, a monetary string turned into a number is a defect. */
const NUMERIC_COERCION = [
  /\bparseFloat\s*\(/,
  /\bparseInt\s*\(/,
  /\bNumber\.parseFloat\s*\(/,
  /\bNumber\.parseInt\s*\(/,
  /(?<!\.)\bNumber\s*\(/,
  /\bBigInt\s*\(/,
  /\.toFixed\s*\(/,
  /\bIntl\.NumberFormat\b/,
  /\bMath\.[a-zA-Z]+\s*\(/,
  /\.reduce\s*\(/,
] as const;

/**
 * The one module allowed to coerce a string to a number, and the reason it is safe.
 *
 * `src/lib/server/config.ts` parses a millisecond timeout out of the environment. It handles no
 * monetary value and imports nothing that does. Keeping the single permitted coercion in a file
 * that never sees an amount is what makes this allowlist an exception rather than a hole — any
 * other file that starts coercing numbers fails the guard.
 */
export const NUMERIC_COERCION_ALLOWED = new Set([join("src", "lib", "server", "config.ts")]);

export interface Violation {
  path: string;
  line: number;
  text: string;
  rule: string;
}

export function findMoneyArithmetic(files: { path: string; source: string }[]): Violation[] {
  const violations: Violation[] = [];

  const before = new RegExp(`${MONEY_NAME}\\s*${ARITHMETIC_OPERATOR}`, "i");
  const after = new RegExp(`${ARITHMETIC_OPERATOR}\\s*${MONEY_NAME}\\b`, "i");
  const compound = new RegExp(`${MONEY_NAME}\\s*(?:${ARITHMETIC_OPERATOR})=`, "i");

  for (const file of files) {
    const lines = stripComments(file.source).split("\n");
    lines.forEach((text, index) => {
      const record = (rule: string) =>
        violations.push({ path: file.path, line: index + 1, text: text.trim(), rule });

      if (compound.test(text)) record("compound assignment to a monetary value");
      else if (before.test(text) || after.test(text)) record("arithmetic on a monetary value");

      if (NUMERIC_COERCION_ALLOWED.has(file.path)) return;
      for (const pattern of NUMERIC_COERCION) {
        if (pattern.test(text)) {
          record(`numeric coercion or aggregation (${pattern.source})`);
          break;
        }
      }
    });
  }

  return violations;
}

// ---------------------------------------------------------------------------------------------
// Guard 2 — the banned phrase
// ---------------------------------------------------------------------------------------------

/**
 * The overclaim this project refuses to make, assembled at runtime.
 *
 * Spelling it as a literal would put it in the tree the guard scans — and in every other guard the
 * repository runs over its own text — so the guard would be the one file that violated it.
 */
export function bannedPhrases(): string[] {
  const words = ["exactly", "once"];
  return [words.join("-"), words.join(" "), words.join("_")];
}

/** Every committed text file under `frontend/`, so the guard covers docs and copy, not just code. */
export function committedText(): { path: string; source: string }[] {
  const extensions = /\.(tsx?|jsx?|mjs|cjs|css|json|md|txt|ya?ml)$/;
  return walk(FRONTEND_ROOT, (path) => extensions.test(path))
    .filter((path) => !path.endsWith("package-lock.json"))
    .map((path) => ({ path: relative(FRONTEND_ROOT, path), source: readFileSync(path, "utf8") }));
}

export function findBannedPhrase(files: { path: string; source: string }[]): Violation[] {
  const phrases = bannedPhrases();
  const violations: Violation[] = [];

  for (const file of files) {
    file.source.split("\n").forEach((text, index) => {
      const lowered = text.toLowerCase();
      for (const phrase of phrases) {
        if (lowered.includes(phrase)) {
          violations.push({ path: file.path, line: index + 1, text: text.trim(), rule: phrase });
          break;
        }
      }
    });
  }

  return violations;
}

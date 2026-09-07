import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat({ baseDirectory: dirname(fileURLToPath(import.meta.url)) });

const config = [
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts"] },
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    rules: {
      // The console renders monetary strings the API already computed. Any of these would be a
      // step towards re-deriving one in the browser, which `src/test/no-money-arithmetic.test.ts`
      // also checks by scanning the source; the lint rule is the fast feedback loop.
      "no-restricted-globals": [
        "error",
        { name: "parseFloat", message: "The console never parses a monetary string into a number." },
        { name: "parseInt", message: "The console never parses a monetary string into a number." },
      ],
      "no-restricted-properties": [
        "error",
        {
          object: "Intl",
          property: "NumberFormat",
          message:
            "Amounts are rendered as the API returned them. Locale-formatting money would re-derive it.",
        },
        {
          property: "toFixed",
          message: "Amounts are rendered as the API returned them; rounding them here is a defect.",
        },
      ],
    },
  },
];

export default config;

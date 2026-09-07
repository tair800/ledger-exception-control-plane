import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";
import { createElement, type AnchorHTMLAttributes, type ReactNode } from "react";

/**
 * `next/link` needs the App Router mounted, which a component test does not have. Replaced with a
 * plain anchor so navigation targets stay assertable: the key-flow test checks the `href` a queue
 * row produces, which is the contract between the two screens.
 */
vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    ...rest
  }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string; children: ReactNode }) =>
    createElement("a", { href, ...rest }, children),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));

// `claimApprovalToken` uses `crypto.randomUUID`. Present in Node 22, but jsdom does not always
// expose it on the window object the tests run against.
if (typeof globalThis.crypto?.randomUUID !== "function") {
  const { randomUUID } = await import("node:crypto");
  Object.defineProperty(globalThis, "crypto", {
    value: { ...globalThis.crypto, randomUUID },
    configurable: true,
  });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

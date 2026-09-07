import type { Metadata } from "next";

import { ConsoleSessionProvider } from "@/components/console-session";
import { Nav } from "@/components/nav";

import "./globals.css";

export const metadata: Metadata = {
  title: "Operations console — ledger exception control plane",
  description:
    "Review settlement exceptions, the evidence behind each proposal, the deterministic adjustment, and the posting that followed.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <ConsoleSessionProvider>
          <Nav />
          <main className="mx-auto max-w-7xl px-4 py-6">{children}</main>
        </ConsoleSessionProvider>
      </body>
    </html>
  );
}

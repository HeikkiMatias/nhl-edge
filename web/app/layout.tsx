import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import { THEME_SCRIPT } from "@/lib/theme";

// The theme's faces (#399), self-hosted from their packages (OFL), latin only: no request leaves
// for a font service, at build time or in a reader's browser. Figures and headings in Barlow Semi
// Condensed, text in IBM Plex Sans (globals.css names them).
import "@fontsource/barlow-semi-condensed/latin-500.css";
import "@fontsource/barlow-semi-condensed/latin-600.css";
import "@fontsource/barlow-semi-condensed/latin-700.css";
import "@fontsource/ibm-plex-sans/latin-400.css";
import "@fontsource/ibm-plex-sans/latin-500.css";
import "@fontsource/ibm-plex-sans/latin-600.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "NHL edge",
  description: "Market making and paper trading: the simulator's figures, the bets, the models.",
  robots: { index: false, follow: false },
};

export const viewport: Viewport = {
  themeColor: "#0b1220",
  colorScheme: "dark light",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    // Dark unless this browser chose light; the script sets it before the first paint.
    <html lang="en" data-theme="dark" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}

import type { Metadata, Viewport } from "next";
import { Barlow_Semi_Condensed, IBM_Plex_Sans } from "next/font/google";
import type { ReactNode } from "react";

import { THEME_SCRIPT } from "@/lib/theme";

import "./globals.css";

// The theme's faces (#399), self-hosted by Next at build time: no request reaches Google from a
// reader's browser. Figures and headings in Barlow Semi Condensed, text in IBM Plex Sans.
const display = Barlow_Semi_Condensed({
  subsets: ["latin"],
  weight: ["500", "600", "700"],
  variable: "--font-display",
  display: "swap",
});
const text = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  variable: "--font-text",
  display: "swap",
});

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
    <html lang="en" data-theme="dark" className={`${display.variable} ${text.variable}`} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body>{children}</body>
    </html>
  );
}

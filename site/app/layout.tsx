import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "@/styles/globals.css";

// BoardUI reads --font-inter / --font-mono-source; Geist is mapped onto those slots.
const sans = Geist({ subsets: ["latin"], variable: "--font-inter" });
const mono = Geist_Mono({ subsets: ["latin"], variable: "--font-mono-source" });

export const metadata: Metadata = {
  title: "AgentBisect: git bisect for agent behavior",
  description:
    "Find the commit that broke your agent, learn why it was written, and prove it by reverting it.",
  openGraph: {
    title: "AgentBisect: git bisect for agent behavior",
    description: "Find the commit that broke your agent, learn why it was written, and prove it by reverting it.",
    type: "website",
  },
  icons: { icon: "/logo.png" },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} ${mono.variable}`}>
      <body className="bg-background-full text-text-primary antialiased">
        <a
          href="#main"
          className="sr-only rounded-lg bg-background-primary-default px-4 py-2 text-body-medium focus:not-sr-only focus:fixed focus:start-4 focus:top-4 focus:z-50 focus-visible:ring-2 focus-visible:ring-border-focus-ring"
        >
          Skip to content
        </a>
        {children}
      </body>
    </html>
  );
}

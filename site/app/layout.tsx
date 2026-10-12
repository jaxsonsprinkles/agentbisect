import type { Metadata } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";
import "@/styles/globals.css";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter" });
const mono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-mono-source" });

export const metadata: Metadata = {
  title: "AgentBisect: git bisect for agent behavior",
  description:
    "Give AgentBisect a failing agent run. It finds the commit that broke the behavior, explains why that commit was written, and proves the culprit by reverting it.",
  openGraph: {
    title: "AgentBisect: git bisect for agent behavior",
    description:
      "Find the commit that broke your agent, understand why it was written, and prove it with a revert test.",
    type: "website",
  },
  icons: { icon: "/logo.png" },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${mono.variable}`}>
      <body className="bg-background-full text-text-primary antialiased">{children}</body>
    </html>
  );
}

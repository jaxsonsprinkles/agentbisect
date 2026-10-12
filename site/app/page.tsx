import Image from "next/image";
import {
  RiArrowRightUpLine,
  RiCheckboxCircleLine,
  RiFileSearchLine,
  RiFingerprintLine,
  RiGitCommitLine,
  RiGithubFill,
  RiNodeTree,
  RiRadarLine,
  RiScalesLine,
  RiShieldCheckLine,
  RiToolsLine,
} from "@remixicon/react";
import { ButtonLink } from "@/components/base/buttons/button";
import { ThemeToggle } from "@/components/application/theme/theme-toggle";
import { BisectDemo } from "./bisect-demo";

const REPO = "https://github.com/jaxsonsprinkles/agentbisect";

const PAINS = [
  { t: "Wrong unit", d: "A refactor moves issue_refund to integer cents. The agent still sends dollars, and no unit test notices." },
  { t: "Overreach", d: "A \"helpful\" change makes the agent cancel orders nobody asked to cancel." },
  { t: "Silent cost", d: "A new tool schema quietly inflates token spend on every request." },
];

const STEPS = [
  { icon: RiRadarLine, t: "Intake", d: "Start from a neatlogs trace, a detection, or a GitHub issue. It maps to an eval case and a known-good commit." },
  { icon: RiFingerprintLine, t: "Failure signatures", d: "Every distinct failing check is bisected on its own, so one range can hide several regressions without blaming the wrong commit." },
  { icon: RiGitCommitLine, t: "Bisect", d: "Binary search over first-parent history, run in isolated git worktrees with repeats against a calibrated median baseline." },
  { icon: RiNodeTree, t: "Smart ranking", d: "Weighted bisection from span divergence, code impact, and the intent behind each commit from its Entire checkpoint." },
  { icon: RiScalesLine, t: "Revert test", d: "A culprit is reported with confidence only if reverting it flips the signature from failing to passing." },
  { icon: RiFileSearchLine, t: "Explain", d: "The report says what the commit was written to do and why that causes the failure, not just which SHA." },
  { icon: RiToolsLine, t: "Fix", d: "A fresh agent sees only the diff, the intent and the failing evidence, and proposes a fix that keeps the intent." },
  { icon: RiShieldCheckLine, t: "Verify the fix", d: "Target signature, regression suite, held-out cases and an intent judge must all agree. Anything less is NOT VERIFIED." },
];

const ROWS = [
  ["call_args:issue_refund.amount", "b0be626", "Passed, 0% to 100%", "ok"],
  ["must_not_call (cancel_order)", "0ecffc0", "Passed, 0% to 100%", "ok"],
  ["budget:tokens@order_status", "59c8b10", "Failed, multiple regressions", "bad"],
  ["budget:tokens@refund_outside_policy", "5cee99b", "Inconclusive", "mid"],
  ["call_args:escalate_to_human.priority", "none", "Refused, fails at the good end", "ok"],
] as const;

export default function Home() {
  return (
    <>
      <Nav />
      <main id="main" className="overflow-x-clip">
        <Hero />
        <Problem />
        <Pipeline />
        <Report />
        <Results />
        <Quickstart />
      </main>
      <Footer />
    </>
  );
}

function Wrap({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <div className={`mx-auto w-full max-w-6xl px-5 sm:px-8 ${className}`}>{children}</div>;
}

const navLink =
  "rounded-md px-2 py-2 hover:text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-border-focus-ring";

function Nav() {
  return (
    <header className="sticky top-0 z-30 border-b border-separator-border bg-background-full/85 backdrop-blur-md">
      <Wrap className="flex h-16 items-center justify-between gap-4">
        <a href="#main" aria-label="AgentBisect home" className="rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-border-focus-ring">
          <Image src="/logo-wordmark.png" alt="AgentBisect" width={1200} height={673} className="h-10 w-auto" priority />
        </a>
        <nav aria-label="Primary" className="hidden items-center gap-4 text-body-2-medium text-text-secondary md:flex">
          <a className={navLink} href="#how">How it works</a>
          <a className={navLink} href="#report">Report</a>
          <a className={navLink} href="#results">Results</a>
          <a className={navLink} href="#start">Quickstart</a>
        </nav>
        <div className="flex items-center gap-2">
          <ThemeToggle appearance="segmented" size="small" />
          <ButtonLink href={REPO} size="small" variant="secondary" leadingIcon={RiGithubFill}>GitHub</ButtonLink>
        </div>
      </Wrap>
    </header>
  );
}

function Hero() {
  return (
    <section className="relative">
      <div aria-hidden className="pointer-events-none absolute inset-x-0 top-0 -z-10 h-[620px] bg-[radial-gradient(55%_70%_at_80%_0%,var(--color-accent-100),transparent)] opacity-60" />
      <Wrap className="pb-20 pt-14 sm:pt-20">
        <div className="grid items-center gap-10 lg:grid-cols-[1.35fr_1fr]">
          <div>
            <h1 className="text-balance text-display-4-semibold tracking-tight sm:text-display-2-semibold lg:text-display-1-semibold">
              <code className="font-mono text-accent-600">git bisect</code> for agent behavior.
            </h1>
            <p className="mt-6 max-w-xl text-body-regular text-text-secondary sm:text-headline-regular">
              Find the commit that broke your agent, learn why it was written, and prove it by reverting it.
            </p>
            <div className="mt-8 flex flex-wrap gap-3">
              <ButtonLink href="#start" trailingIcon={RiArrowRightUpLine}>Get started</ButtonLink>
              <ButtonLink href="#how" variant="secondary">How it works</ButtonLink>
            </div>
          </div>
          <div className="mx-auto w-full max-w-[260px] sm:max-w-[300px] lg:order-last lg:justify-self-end">
            <video
              controls
              playsInline
              preload="metadata"
              poster="/demo-poster.jpg"
              aria-label="Walkthrough video: a support agent refunds a customer, shown in a neatlogs trace"
              className="aspect-[9/16] w-full rounded-3xl border border-border-button-default bg-neutral-950 object-cover shadow-[0_24px_60px_-24px_rgb(30_60_200/0.35)]"
            >
              <source src="/demo.mp4" type="video/mp4" />
            </video>
          </div>
        </div>
        <div className="mt-16">
          <BisectDemo />
          <p className="mt-3 text-caption-1-regular text-text-secondary">
            An illustration of a plain bisect over 12 commits. The first, last and culprit hashes come from the demo target, support-agent-demo.
          </p>
        </div>
      </Wrap>
    </section>
  );
}

function Problem() {
  return (
    <section className="border-y border-separator-border bg-background-secondary-default py-20">
      <Wrap className="grid gap-10 lg:grid-cols-[1fr_1.5fr] lg:gap-16">
        <div>
          <h2 className="text-balance text-title-1-semibold sm:text-display-4-semibold">Agents regress in ways unit tests do not catch.</h2>
          <p className="mt-4 text-body-regular text-text-secondary">
            <code className="font-mono text-body-2-medium">git bisect</code> needs a deterministic good/bad test. An LLM agent gives you a noisy one. AgentBisect supplies that test.
          </p>
        </div>
        <dl className="divide-y divide-separator-border self-center">
          {PAINS.map((p) => (
            <div key={p.t} className="grid gap-1 py-5 first:pt-0 last:pb-0 sm:grid-cols-[9rem_1fr] sm:gap-6">
              <dt className="text-headline-semibold">{p.t}</dt>
              <dd className="text-body-regular text-text-secondary">{p.d}</dd>
            </div>
          ))}
        </dl>
      </Wrap>
    </section>
  );
}

function Pipeline() {
  return (
    <section id="how" className="scroll-mt-16 py-24">
      <Wrap className="grid gap-12 lg:grid-cols-[1fr_1.6fr] lg:gap-16">
        <div className="lg:sticky lg:top-28 lg:self-start">
          <h2 className="text-balance text-title-1-semibold sm:text-display-4-semibold">From a failing trace to a verified fix.</h2>
          <p className="mt-4 text-body-regular text-text-secondary">
            It runs the target repo&apos;s eval suite at every commit it probes, with repeats against a calibrated baseline, so a flaky run is not mistaken for a regression.
          </p>
        </div>
        <ol className="relative space-y-8 before:absolute before:inset-y-2 before:start-5 before:w-px before:bg-separator-border">
          {STEPS.map((s) => (
            <li key={s.t} className="relative flex gap-5">
              <span className="relative flex size-10 shrink-0 items-center justify-center rounded-xl border border-border-button-default bg-background-full text-accent-600">
                <s.icon className="size-5" aria-hidden />
              </span>
              <div className="pt-1.5">
                <h3 className="text-headline-semibold">{s.t}</h3>
                <p className="mt-1 max-w-xl text-body-2-regular text-text-secondary">{s.d}</p>
              </div>
            </li>
          ))}
        </ol>
      </Wrap>
    </section>
  );
}

function Report() {
  return (
    <section id="report" className="scroll-mt-16 border-y border-separator-border bg-background-secondary-default py-24">
      <Wrap>
        <h2 className="max-w-2xl text-balance text-title-1-semibold sm:text-display-4-semibold">The answer is a reason, not just a SHA.</h2>
        <p className="mt-4 max-w-xl text-body-regular text-text-secondary">
          Excerpts from the real report for the refund-amount regression. The Verifier reverted the culprit and the failure went away.
        </p>
        <div className="mt-10 grid gap-4 lg:grid-cols-[1.25fr_1fr]">
          <div className="overflow-hidden rounded-3xl bg-neutral-950 font-mono text-caption-1-regular leading-6 text-neutral-300">
            <p className="border-b border-white/10 px-5 py-3 text-neutral-400">evidence/report-6d7b32e553.md</p>
            <div className="space-y-5 overflow-x-auto p-5">
              <div>
                <p className="text-neutral-400">failure signature</p>
                <p className="text-neutral-100">check=call_args(issue_refund.amount) expected 64.0 got 6400</p>
              </div>
              <div>
                <p className="text-neutral-400">culprit commit, medium confidence</p>
                <p className="text-accent-300">b0be626692328e15644a54f3100ab7ad6fc09a5b</p>
              </div>
              <div>
                <p className="text-neutral-400">original intent, from the Entire checkpoint</p>
                <p className="text-neutral-100">Return refund amounts in cents to match Stripe: issue_refund should take and return integer cents, with the schema, tool implementation and tests updated.</p>
              </div>
              <div>
                <p className="text-neutral-400">semantic diff</p>
                <p className="text-neutral-100">tools/impl.py: signature_changed method ToolBox._tool_issue_refund</p>
              </div>
            </div>
          </div>
          <div className="flex flex-col justify-between gap-6 rounded-3xl bg-accent-600 p-7 text-text-white">
            <div>
              <p className="text-body-2-medium text-accent-100">Revert test</p>
              <p className="mt-2 text-display-4-semibold">Passed</p>
            </div>
            <dl className="space-y-3 text-body-regular">
              {[["Good commit", "100%"], ["Culprit", "0%"], ["Culprit reverted on HEAD", "100%"]].map(([k, v]) => (
                <div key={k} className="flex items-baseline justify-between gap-4 border-t border-white/25 pt-3">
                  <dt className="text-accent-100">{k}</dt>
                  <dd className="text-title-3-semibold tabular-nums">{v}</dd>
                </div>
              ))}
            </dl>
          </div>
        </div>
        <ul className="mt-8 grid gap-3 text-body-2-regular text-text-secondary sm:grid-cols-3">
          {["Intent from the coding session that wrote the commit", "Before and after pass rates for every revert", "Confidence labelled honestly, down to inconclusive"].map((t) => (
            <li key={t} className="flex gap-2"><RiCheckboxCircleLine className="mt-0.5 size-4 shrink-0 text-accent-600" aria-hidden />{t}</li>
          ))}
        </ul>
      </Wrap>
    </section>
  );
}

function Results() {
  return (
    <section id="results" className="scroll-mt-16 py-24">
      <Wrap>
        <h2 className="max-w-2xl text-balance text-title-1-semibold sm:text-display-4-semibold">Every number comes from a file in the repo.</h2>
        <div className="mt-10 grid gap-4 md:grid-cols-4 md:grid-rows-2">
          <div className="rounded-3xl bg-accent-100 p-7 md:col-span-2 md:row-span-2">
            <p className="text-display-2-semibold tabular-nums text-accent-700">4 of 6</p>
            <p className="mt-3 max-w-xs text-body-regular text-text-primary">signatures attributed correctly on the full-range re-run. Both behavioral ones and the refusal were right.</p>
            <p className="mt-6 font-mono text-caption-1-medium text-text-secondary">evidence/rerun3-summary.md</p>
          </div>
          <div className="rounded-3xl border border-border-button-default p-6 md:col-span-2">
            <p className="text-display-4-semibold tabular-nums text-accent-600">51.3%</p>
            <p className="mt-2 text-body-2-regular text-text-secondary">less planner context with the Entire graph: 12,869 tokens against 26,420 reading the raw range.</p>
          </div>
          <div className="rounded-3xl border border-border-button-default p-6">
            <p className="text-title-1-semibold tabular-nums text-accent-600">18 vs 24</p>
            <p className="mt-2 text-body-2-regular text-text-secondary">eval rounds, smart vs plain, on the refund-amount signature.</p>
          </div>
          <div className="rounded-3xl border border-border-button-default p-6">
            <p className="text-title-1-semibold tabular-nums text-accent-600">326</p>
            <p className="mt-2 text-body-2-regular text-text-secondary">tests passing, with fake executors and throwaway git repos.</p>
          </div>
        </div>
        <div className="mt-6 overflow-x-auto rounded-3xl border border-border-button-default">
          <table className="w-full min-w-[640px] text-body-2-regular">
            <caption className="sr-only">Per-signature results of the final re-run</caption>
            <thead className="bg-background-secondary-default text-caption-1-semibold text-text-secondary">
              <tr>
                <th scope="col" className="px-5 py-3 text-start">Signature</th>
                <th scope="col" className="px-5 py-3 text-start">Culprit</th>
                <th scope="col" className="px-5 py-3 text-start">Revert test</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-separator-border">
              {ROWS.map(([sig, sha, res, tone]) => (
                <tr key={sig}>
                  <th scope="row" className="px-5 py-3 text-start font-mono text-caption-1-medium">{sig}</th>
                  <td className="px-5 py-3 font-mono text-caption-1-medium text-text-secondary">{sha}</td>
                  <td className={`px-5 py-3 ${tone === "bad" ? "text-text-error-primary" : tone === "ok" ? "text-accent-700" : "text-text-secondary"}`}>{res}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-3 text-caption-1-regular text-text-secondary">
          Final re-run on support-agent-demo, good 1b40b27, bad 0ecffc0.
        </p>
      </Wrap>
    </section>
  );
}

function Quickstart() {
  return (
    <section id="start" className="scroll-mt-16 border-t border-separator-border bg-background-secondary-default py-24">
      <Wrap>
        <h2 className="max-w-2xl text-balance text-title-1-semibold sm:text-display-4-semibold">Calibrate once, then investigate.</h2>
        <p className="mt-4 max-w-xl text-body-regular text-text-secondary">
          Needs Python 3.11 to 3.13, uv and git. Entire and neatlogs are optional; without Entire the planner falls back to plain bisect.
        </p>
        <pre tabIndex={0} className="mt-8 overflow-x-auto rounded-3xl bg-neutral-950 p-6 font-mono text-caption-1-regular leading-6 text-neutral-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-border-focus-ring">
{`# one-time token baseline at the known-good commit
uv run agentbisect calibrate --repo ../support-agent-demo \\
    --good 1b40b27 --llm-proxy auto

# investigate from a bad trace
uv run agentbisect investigate --trace <trace-id> \\
    --repo ../support-agent-demo \\
    --strategy smart --all-signatures --llm-proxy auto

# report, fix, and verify the fix
uv run agentbisect report ID
uv run agentbisect fix ID --llm-proxy auto
uv run agentbisect verify-fix ID --commit SHA`}
        </pre>
        <div className="mt-6">
          <ButtonLink href={REPO} leadingIcon={RiGithubFill}>View the repo</ButtonLink>
        </div>
      </Wrap>
    </section>
  );
}

function Footer() {
  return (
    <footer className="border-t border-separator-border py-14">
      <Wrap className="flex flex-col items-start justify-between gap-8 sm:flex-row sm:items-end">
        <div>
          <Image src="/logo-wordmark.png" alt="AgentBisect" width={1200} height={673} className="w-36" />
          <p className="mt-4 text-body-2-regular text-text-secondary">Built for neatHack, Oct 10, 2026.</p>
        </div>
        <a
          className="rounded-md text-body-2-medium text-text-secondary hover:text-text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-border-focus-ring"
          href={REPO}
        >
          github.com/jaxsonsprinkles/agentbisect
        </a>
      </Wrap>
    </footer>
  );
}

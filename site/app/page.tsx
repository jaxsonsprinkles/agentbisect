import {
  RiArrowRightUpLine,
  RiGitCommitLine,
  RiFingerprintLine,
  RiNodeTree,
  RiScalesLine,
  RiShieldCheckLine,
  RiFileSearchLine,
  RiToolsLine,
  RiCheckboxCircleLine,
  RiGithubFill,
  RiRadarLine,
} from "@remixicon/react";
import { ButtonLink } from "@/components/base/buttons/button";
import { BisectDemo } from "./bisect-demo";

const REPO = "https://github.com/jaxsonsprinkles/agentbisect";

const PAINS = [
  { k: "Wrong unit", t: "A refactor moves issue_refund to integer cents. Refund amounts arrive in the wrong unit, and no unit test notices." },
  { k: "Overreach", t: "A \"helpful\" prompt change makes the agent cancel orders nobody asked to cancel." },
  { k: "Silent cost", t: "A new tool schema quietly doubles token spend on every single request." },
];

const STEPS = [
  { icon: RiRadarLine, t: "Intake", d: "Start from a neatlogs trace, a detection, or a GitHub issue. It maps to an eval case and a known-good commit." },
  { icon: RiFingerprintLine, t: "Failure signatures", d: "Every distinct failing check is bisected on its own, so one range can hide several regressions without blaming the wrong one." },
  { icon: RiGitCommitLine, t: "Bisect", d: "Binary search over first-parent history, run in isolated git worktrees with repeats against a calibrated median baseline." },
  { icon: RiNodeTree, t: "Smart ranking", d: "Weighted bisection from span divergence, code impact, and the intent behind each commit from its Entire checkpoint." },
  { icon: RiScalesLine, t: "Revert test", d: "A culprit is only reported with confidence if reverting it flips the signature from failing to passing." },
  { icon: RiFileSearchLine, t: "Explain", d: "The report says what the commit was written to do and why that causes the failure, not just a SHA." },
  { icon: RiToolsLine, t: "Fix", d: "A fresh agent sees only the diff, the intent and the failing evidence, and proposes a fix that keeps the intent." },
  { icon: RiShieldCheckLine, t: "Verify the fix", d: "Target signature, regression suite, held-out cases and an intent judge must all agree. Anything less is NOT VERIFIED." },
];

const RESULTS = [
  { n: "4 of 6", l: "signatures attributed correctly on the full-range re-run", s: "rerun3-summary.md" },
  { n: "51.3%", l: "less planner context with the Entire graph (12,869 vs 26,420 tokens)", s: "entire-context-savings.md" },
  { n: "18 vs 24", l: "eval rounds, smart vs plain, on the refund-amount signature", s: "rerun3-summary.md" },
  { n: "326", l: "tests passing, on fake executors and throwaway git repos", s: "README" },
];

const ROWS = [
  ["call_args:issue_refund.amount", "b0be626", "Passed · 0% → 100%", "ok"],
  ["must_not_call (cancel_order)", "0ecffc0", "Passed · 0% → 100%", "ok"],
  ["budget:tokens@order_status", "59c8b10", "Failed · multiple regressions", "bad"],
  ["budget:tokens@refund_outside_policy", "5cee99b", "Inconclusive", "mid"],
  ["call_args:escalate_to_human.priority", "none", "Refused · fails at good end", "ok"],
] as const;

export default function Home() {
  return (
    <main className="overflow-x-clip">
      <Nav />
      <Hero />
      <Problem />
      <Pipeline />
      <Report />
      <Results />
      <Quickstart />
      <Footer />
    </main>
  );
}

function Mark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 28 28" className={className} aria-hidden>
      <rect width="28" height="28" rx="8" className="fill-accent-600" />
      <circle cx="9" cy="14" r="2.6" fill="white" />
      <circle cx="19" cy="9" r="2.2" fill="white" opacity=".55" />
      <circle cx="19" cy="19" r="2.2" fill="white" />
      <path d="M11.4 13l5.4-3M11.4 15l5.4 3" stroke="white" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}

function Wrap({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <div className={`mx-auto w-full max-w-6xl px-5 sm:px-8 ${className}`}>{children}</div>;
}

function Eyebrow({ children }: { children: React.ReactNode }) {
  return <p className="font-mono text-caption-1-semibold uppercase tracking-widest text-accent-600">{children}</p>;
}

function Nav() {
  return (
    <header className="sticky top-0 z-20 border-b border-separator-border bg-background-full/80 backdrop-blur-md">
      <Wrap className="flex h-14 items-center justify-between">
        <a href="#top" className="flex items-center gap-2">
          <Mark className="size-7" />
          <span className="text-headline-semibold">AgentBisect</span>
        </a>
        <nav className="hidden items-center gap-7 text-body-2-medium text-text-secondary sm:flex">
          <a className="hover:text-text-primary" href="#how">How it works</a>
          <a className="hover:text-text-primary" href="#report">Report</a>
          <a className="hover:text-text-primary" href="#results">Results</a>
          <a className="hover:text-text-primary" href="#start">Quickstart</a>
        </nav>
        <ButtonLink href={REPO} size="small" variant="secondary" leadingIcon={RiGithubFill}>GitHub</ButtonLink>
      </Wrap>
    </header>
  );
}

function Hero() {
  return (
    <section id="top" className="relative">
      <div aria-hidden className="pointer-events-none absolute inset-x-0 top-0 -z-10 h-[640px] bg-[radial-gradient(60%_60%_at_50%_0%,var(--color-accent-100),transparent)] opacity-70" />
      <Wrap className="pb-20 pt-16 sm:pt-24">
        <div className="mx-auto max-w-3xl text-center">
          <p className="mx-auto inline-flex items-center gap-2 rounded-full border border-border-button-default bg-background-primary-default px-3 py-1 text-caption-1-medium text-text-secondary">
            <span className="size-1.5 rounded-full bg-accent-500" /> Built for neatHack · Oct 10, 2026
          </p>
          <h1 className="mt-6 text-balance text-display-4-semibold tracking-tight sm:text-display-2-semibold lg:text-display-1-semibold">
            <code className="font-mono text-accent-600">git bisect</code> for agent behavior.
          </h1>
          <p className="mx-auto mt-6 max-w-2xl text-balance text-body-regular text-text-secondary sm:text-headline-regular">
            Hand it a failing agent run. It finds the commit that broke the behavior, explains why that commit was written, and proves the culprit by reverting it.
          </p>
          <div className="mt-8 flex flex-wrap justify-center gap-3">
            <ButtonLink href="#start" trailingIcon={RiArrowRightUpLine}>Get started</ButtonLink>
            <ButtonLink href="#how" variant="secondary">See how it works</ButtonLink>
          </div>
        </div>
        <div className="mx-auto mt-14 max-w-4xl">
          <BisectDemo />
          <p className="mt-3 text-center text-caption-1-regular text-text-tertiary">
            Illustration of a plain bisect over 12 commits. Hashes are from the demo target, support-agent-demo.
          </p>
        </div>
      </Wrap>
    </section>
  );
}

function Problem() {
  return (
    <section className="border-y border-separator-border bg-background-secondary-default py-20">
      <Wrap>
        <div className="grid gap-10 lg:grid-cols-[1fr_1.4fr]">
          <div>
            <Eyebrow>The problem</Eyebrow>
            <h2 className="mt-3 text-balance text-title-1-semibold sm:text-display-4-semibold">
              Agents regress in ways unit tests do not catch.
            </h2>
            <p className="mt-4 text-body-regular text-text-secondary">
              <code className="font-mono text-body-2-medium">git bisect</code> can find the commit, but it needs a deterministic good/bad test. An LLM agent gives you a noisy one. AgentBisect supplies that test.
            </p>
          </div>
          <ul className="grid gap-3">
            {PAINS.map((p) => (
              <li key={p.k} className="rounded-3xl border border-border-button-default bg-background-primary-default p-5">
                <p className="font-mono text-caption-1-semibold uppercase tracking-widest text-text-error-primary">{p.k}</p>
                <p className="mt-2 text-body-regular text-text-primary">{p.t}</p>
              </li>
            ))}
          </ul>
        </div>
      </Wrap>
    </section>
  );
}

function Pipeline() {
  return (
    <section id="how" className="scroll-mt-14 py-24">
      <Wrap>
        <div className="max-w-2xl">
          <Eyebrow>How it works</Eyebrow>
          <h2 className="mt-3 text-balance text-title-1-semibold sm:text-display-4-semibold">
            From a failing trace to a verified fix.
          </h2>
          <p className="mt-4 text-body-regular text-text-secondary">
            It runs the target repo&apos;s eval suite at every commit it probes, repeating runs against a calibrated baseline so a flaky run is never mistaken for a regression.
          </p>
        </div>
        <ol className="mt-12 grid gap-px overflow-hidden rounded-3xl border border-border-button-default bg-separator-border sm:grid-cols-2 lg:grid-cols-4">
          {STEPS.map((s, i) => (
            <li key={s.t} className="group bg-background-primary-default p-6 transition-colors hover:bg-background-secondary-default">
              <div className="flex items-center justify-between">
                <span className="flex size-10 items-center justify-center rounded-xl bg-accent-100 text-accent-700">
                  <s.icon className="size-5" aria-hidden />
                </span>
                <span className="font-mono text-caption-1-medium text-text-tertiary">{String(i + 1).padStart(2, "0")}</span>
              </div>
              <h3 className="mt-5 text-headline-semibold">{s.t}</h3>
              <p className="mt-2 text-body-2-regular text-text-secondary">{s.d}</p>
            </li>
          ))}
        </ol>
      </Wrap>
    </section>
  );
}

function Report() {
  return (
    <section id="report" className="scroll-mt-14 bg-neutral-950 py-24 text-text-white">
      <Wrap className="grid items-center gap-12 lg:grid-cols-[1fr_1.15fr]">
        <div>
          <p className="font-mono text-caption-1-semibold uppercase tracking-widest text-accent-400">The answer, not a SHA</p>
          <h2 className="mt-3 text-balance text-title-1-semibold sm:text-display-4-semibold">
            This commit, written to do X, causes Y.
          </h2>
          <p className="mt-4 text-body-regular text-neutral-400">
            Each culprit report quotes the Entire checkpoint behind the commit, the semantic diff, and the first place the bad run diverged from a good one. Then the Verifier reverts it to check.
          </p>
          <ul className="mt-6 space-y-3 text-body-2-regular text-neutral-300">
            {["Intent from the coding session that wrote the commit", "Revert test with before and after pass rates", "Confidence labelled honestly, down to inconclusive"].map((t) => (
              <li key={t} className="flex gap-2"><RiCheckboxCircleLine className="mt-0.5 size-4 shrink-0 text-accent-400" aria-hidden />{t}</li>
            ))}
          </ul>
        </div>
        <div className="overflow-hidden rounded-3xl border border-white/10 bg-neutral-900 font-mono text-body-2-regular shadow-2xl">
          <div className="flex items-center gap-1.5 border-b border-white/10 px-4 py-3">
            <span className="size-2.5 rounded-full bg-neutral-700" /><span className="size-2.5 rounded-full bg-neutral-700" /><span className="size-2.5 rounded-full bg-neutral-700" />
            <span className="ms-3 text-caption-1-regular text-neutral-500">report · call_args:issue_refund.amount</span>
          </div>
          <div className="space-y-4 p-5 text-neutral-300">
            <p><span className="text-neutral-500">culprit </span><span className="text-accent-400">b0be626</span> <span className="text-neutral-500">(medium confidence)</span></p>
            <div>
              <p className="text-neutral-500">why it was written</p>
              <p className="mt-1 text-neutral-200">Deliberately move issue_refund to integer cents.</p>
            </div>
            <div className="overflow-x-auto rounded-xl bg-black/40 p-3 text-caption-1-regular leading-6">
              <p className="text-neutral-500">diff (abridged)</p>
              <p className="text-red-400">- issue_refund(amount in dollars)</p>
              <p className="text-emerald-400">+ issue_refund(amount in cents)</p>
            </div>
            <div>
              <p className="text-neutral-500">first divergence</p>
              <p className="mt-1">bad run passes <span className="text-red-400">cents</span> where the eval expects <span className="text-emerald-400">dollars</span></p>
            </div>
            <p className="border-t border-white/10 pt-4"><span className="text-neutral-500">revert test </span><span className="text-emerald-400">PASSED</span> <span className="text-neutral-400">pass rate 0% → 100%</span></p>
          </div>
          <p className="px-5 pb-4 text-caption-1-regular text-neutral-500">Illustrative layout. Real reports: evidence/report-*.html in the repo.</p>
        </div>
      </Wrap>
    </section>
  );
}

function Results() {
  return (
    <section id="results" className="scroll-mt-14 py-24">
      <Wrap>
        <div className="max-w-2xl">
          <Eyebrow>Measured so far</Eyebrow>
          <h2 className="mt-3 text-balance text-title-1-semibold sm:text-display-4-semibold">Every number comes from a file in the repo.</h2>
        </div>
        <dl className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {RESULTS.map((r) => (
            <div key={r.l} className="rounded-3xl border border-border-button-default p-6">
              <dt className="sr-only">{r.l}</dt>
              <dd className="text-display-4-semibold tabular-nums text-accent-600">{r.n}</dd>
              <p className="mt-3 text-body-2-regular text-text-secondary">{r.l}</p>
              <p className="mt-4 font-mono text-caption-2-medium text-text-tertiary">{r.s}</p>
            </div>
          ))}
        </dl>
        <div className="mt-8 overflow-x-auto rounded-3xl border border-border-button-default">
          <table className="w-full min-w-[640px] text-start text-body-2-regular">
            <thead className="bg-background-secondary-default text-caption-1-semibold text-text-secondary">
              <tr>
                <th className="px-5 py-3 text-start">Signature</th>
                <th className="px-5 py-3 text-start">Culprit</th>
                <th className="px-5 py-3 text-start">Revert test</th>
              </tr>
            </thead>
            <tbody>
              {ROWS.map(([sig, sha, res, tone]) => (
                <tr key={sig} className="border-t border-separator-border">
                  <td className="px-5 py-3 font-mono text-caption-1-medium">{sig}</td>
                  <td className="px-5 py-3 font-mono text-caption-1-medium text-text-secondary">{sha}</td>
                  <td className="px-5 py-3">
                    <span className="inline-flex items-center gap-2">
                      <span className={`size-2 rounded-full ${tone === "ok" ? "bg-accent-500" : tone === "bad" ? "bg-text-error-primary" : "bg-border-button-active"}`} />
                      {res}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="mt-3 text-caption-1-regular text-text-tertiary">
          Final re-run on support-agent-demo, good 1b40b27, bad 0ecffc0. Behavioral signatures were right; token-budget signatures are the weak spot.
        </p>
      </Wrap>
    </section>
  );
}

function Quickstart() {
  return (
    <section id="start" className="scroll-mt-14 border-t border-separator-border bg-background-secondary-default py-24">
      <Wrap className="grid gap-10 lg:grid-cols-[1fr_1.3fr]">
        <div>
          <Eyebrow>Quickstart</Eyebrow>
          <h2 className="mt-3 text-balance text-title-1-semibold sm:text-display-4-semibold">Calibrate once, then investigate.</h2>
          <p className="mt-4 text-body-regular text-text-secondary">
            Needs Python 3.11 to 3.13, uv and git. Entire and neatlogs are optional; without Entire the planner falls back to commit messages and plain bisect.
          </p>
          <div className="mt-6 flex gap-3">
            <ButtonLink href={REPO} leadingIcon={RiGithubFill}>View the repo</ButtonLink>
          </div>
        </div>
        <pre className="overflow-x-auto rounded-3xl border border-border-button-default bg-background-primary-default p-5 font-mono text-caption-1-regular leading-6 text-text-primary">
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
      </Wrap>
    </section>
  );
}

function Footer() {
  return (
    <footer className="border-t border-separator-border py-10">
      <Wrap className="flex flex-col items-start justify-between gap-4 text-body-2-regular text-text-secondary sm:flex-row sm:items-center">
        <div className="flex items-center gap-2">
          <Mark className="size-6" />
          <span>AgentBisect · built for neatHack, Oct 10, 2026</span>
        </div>
        <a className="hover:text-text-primary" href={REPO}>github.com/jaxsonsprinkles/agentbisect</a>
      </Wrap>
    </footer>
  );
}

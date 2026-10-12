"use client";

import { useEffect, useState } from "react";
import { RiPlayFill, RiRestartLine } from "@remixicon/react";
import { Button } from "@/components/base/buttons/button";
import { cx } from "@/utils/cx";

const COMMITS = [
  "1b40b27", "a41c9e2", "7d03f1b", "e92a6c4", "3fb81d0", "b0be626", "59c8b10", "c27e4a9",
  "5cee99b", "81d6f33", "d4a0b75", "0ecffc0",
];
const CULPRIT = 5;
// Probe sequence for a plain bisect over indices 0..11 with culprit at 5.
const STEPS: { probe: number; bad: boolean; lo: number; hi: number }[] = [
  { probe: 5, bad: true, lo: 0, hi: 5 },
  { probe: 2, bad: false, lo: 3, hi: 5 },
  { probe: 4, bad: false, lo: 5, hi: 5 },
];

type Status = "good" | "bad" | "unknown";

export function BisectDemo() {
  const [step, setStep] = useState(-1);
  const [playing, setPlaying] = useState(false);

  useEffect(() => {
    if (!playing) return;
    if (step >= STEPS.length) {
      setPlaying(false);
      return;
    }
    const t = setTimeout(() => setStep((s) => s + 1), step < 0 ? 400 : 1400);
    return () => clearTimeout(t);
  }, [playing, step]);

  const done = step >= STEPS.length;
  const status = (i: number): Status => {
    if (i === 0) return "good";
    if (i === COMMITS.length - 1) return "bad";
    for (let s = 0; s <= Math.min(step, STEPS.length - 1); s++) {
      if (STEPS[s].probe === i) return STEPS[s].bad ? "bad" : "good";
    }
    // Implied by monotonic assumption
    const known = STEPS.slice(0, step + 1);
    const lo = known.length ? known[known.length - 1].lo : 0;
    const hi = known.length ? known[known.length - 1].hi : COMMITS.length - 1;
    if (i < lo) return "good";
    if (i > hi) return "bad";
    return "unknown";
  };
  const current = step >= 0 && step < STEPS.length ? STEPS[step] : null;
  const lastStep = step >= 0 ? STEPS[Math.min(step, STEPS.length - 1)] : null;

  return (
    <div className="rounded-3xl border border-border-button-default bg-background-primary-default p-4 shadow-sm sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 font-mono text-caption-1-medium text-text-secondary">
          <span className="relative flex size-2">
            <span className={cx("absolute inline-flex size-full rounded-full", playing && "animate-ping bg-accent-400")} />
            <span className="relative inline-flex size-2 rounded-full bg-accent-500" />
          </span>
          agentbisect investigate --strategy smart
        </div>
        <Button
          size="small"
          variant={done ? "secondary" : "primary"}
          leadingIcon={done ? RiRestartLine : RiPlayFill}
          onClick={() => {
            setStep(done ? 0 : step < 0 ? 0 : step);
            setPlaying(true);
          }}
          disabled={playing}
        >
          {done ? "Replay" : step < 0 ? "Run bisect" : "Resume"}
        </Button>
      </div>

      <ol className="mt-8 grid grid-cols-6 gap-x-2 gap-y-6 sm:grid-cols-12" aria-label="Commits between good and bad">
        {COMMITS.map((sha, i) => {
          const st = status(i);
          const isProbe = current?.probe === i;
          const isCulprit = done && i === CULPRIT;
          return (
            <li key={sha} className="flex flex-col items-center gap-2">
              <div
                className={cx(
                  "relative flex size-9 items-center justify-center rounded-full border text-caption-2-semibold transition-all duration-500",
                  st === "good" && "border-separator-border bg-background-secondary-default text-text-secondary",
                  st === "bad" && "border-text-error-primary/40 bg-background-tertiary-error text-text-error-primary",
                  st === "unknown" && "border-dashed border-border-button-hover bg-background-primary-default text-text-tertiary",
                  isProbe && "scale-110 ring-4 ring-accent-200",
                  isCulprit && "scale-110 border-accent-600 bg-accent-600 text-text-white ring-4 ring-accent-200",
                )}
              >
                {isCulprit ? "!" : st === "good" ? "✓" : st === "bad" ? "✕" : "?"}
              </div>
              <code className={cx("font-mono text-caption-2-medium", isCulprit ? "text-accent-700" : "text-text-tertiary")}>{sha}</code>
            </li>
          );
        })}
      </ol>

      <div className="mt-8 grid gap-3 border-t border-separator-border pt-5 sm:grid-cols-3">
        <Stat label="Probes" value={done ? "3" : String(Math.max(step + 1, 0))} sub="eval runs on commits" />
        <Stat label="Range" value={lastStep ? `${lastStep.hi - lastStep.lo + 1} commits` : "12 commits"} sub="left to suspect" />
        <div className="rounded-2xl bg-background-secondary-default p-4">
          <p className="text-caption-1-medium text-text-secondary">Verdict</p>
          <p className="mt-1 text-headline-medium text-text-primary">
            {done ? (
              <>
                Culprit <code className="font-mono text-accent-700">b0be626</code>
              </>
            ) : (
              "Searching…"
            )}
          </p>
          <p className="text-body-2-regular text-text-secondary">
            {done ? "Revert flips refund test 0% → 100%" : "Each probe runs the eval suite"}
          </p>
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="rounded-2xl bg-background-secondary-default p-4">
      <p className="text-caption-1-medium text-text-secondary">{label}</p>
      <p className="mt-1 text-headline-medium tabular-nums text-text-primary">{value}</p>
      <p className="text-body-2-regular text-text-secondary">{sub}</p>
    </div>
  );
}

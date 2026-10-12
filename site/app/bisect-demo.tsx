"use client";

import { useEffect, useState } from "react";
import { RiCheckLine, RiCloseLine, RiFlagLine, RiPlayFill, RiQuestionMark, RiRestartLine } from "@remixicon/react";
import { Button } from "@/components/base/buttons/button";
import { cx } from "@/utils/cx";

// Illustration: 12 commits, culprit at index 5. Only the first, last and culprit hashes are real
// (support-agent-demo); the commits between are labelled by position.
const COMMITS = ["1b40b27", "c1", "c2", "c3", "c4", "b0be626", "c6", "c7", "c8", "c9", "c10", "0ecffc0"];
const CULPRIT = 5;
const STEPS = [
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
    const t = setTimeout(() => setStep((s) => s + 1), step < 0 ? 300 : 1300);
    return () => clearTimeout(t);
  }, [playing, step]);

  const done = step >= STEPS.length;
  const known = STEPS.slice(0, Math.min(step + 1, STEPS.length));
  const last = known.length ? known[known.length - 1] : null;
  const status = (i: number): Status => {
    if (i === 0) return "good";
    if (i === COMMITS.length - 1) return "bad";
    const hit = known.find((s) => s.probe === i);
    if (hit) return hit.bad ? "bad" : "good";
    if (last && i < last.lo) return "good";
    if (last && i > last.hi) return "bad";
    return "unknown";
  };
  const current = step >= 0 && step < STEPS.length ? STEPS[step] : null;
  const remaining = last ? last.hi - last.lo + 1 : COMMITS.length;

  return (
    <div className="rounded-3xl border border-border-button-default bg-background-primary-default p-4 shadow-[0_1px_2px_rgb(0_0_0/0.04),0_12px_32px_-12px_rgb(30_60_200/0.18)] sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <code className="font-mono text-caption-1-medium text-text-secondary">agentbisect investigate --strategy smart</code>
        <Button
          size="small"
          variant={done ? "secondary" : "primary"}
          leadingIcon={done ? RiRestartLine : RiPlayFill}
          onClick={() => {
            setStep(done ? 0 : Math.max(step, 0));
            setPlaying(true);
          }}
          disabled={playing}
        >
          {done ? "Replay" : "Run bisect"}
        </Button>
      </div>

      <ol className="mt-8 grid grid-cols-6 gap-x-2 gap-y-6 sm:grid-cols-12" aria-label="Twelve commits from known good to known bad">
        {COMMITS.map((sha, i) => {
          const st = status(i);
          const isProbe = current?.probe === i;
          const isCulprit = done && i === CULPRIT;
          const Icon = isCulprit ? RiFlagLine : st === "good" ? RiCheckLine : st === "bad" ? RiCloseLine : RiQuestionMark;
          return (
            <li key={sha} className="flex flex-col items-center gap-2">
              <div
                className={cx(
                  "flex size-9 items-center justify-center rounded-full border transition-[transform,background-color,border-color] duration-500 motion-reduce:transition-none",
                  st === "good" && "border-separator-border bg-background-secondary-default text-text-secondary",
                  st === "bad" && "border-border-error-default bg-background-tertiary-error text-text-error-primary",
                  st === "unknown" && "border-dashed border-border-button-hover text-text-tertiary",
                  isProbe && "scale-110 ring-4 ring-accent-200",
                  isCulprit && "scale-110 border-accent-600 bg-accent-600 text-text-white ring-4 ring-accent-200",
                )}
              >
                <Icon className="size-4" aria-hidden />
              </div>
              <code className={cx("font-mono text-caption-2-medium", isCulprit ? "text-accent-700" : "text-text-secondary")}>{sha}</code>
            </li>
          );
        })}
      </ol>

      <div className="mt-8 grid gap-3 border-t border-separator-border pt-5 sm:grid-cols-3">
        <Stat label="Eval probes" value={String(done ? STEPS.length : Math.max(step + 1, 0))} sub="runs of the suite" />
        <Stat label="Still suspect" value={`${remaining} commits`} sub={done ? "one left" : "of 12 in range"} />
        <div className="rounded-2xl bg-background-secondary-default p-4">
          <p className="text-caption-1-medium text-text-secondary">Verdict</p>
          <p className="mt-1 text-headline-medium" role="status">
            {done ? <>Culprit <code className="font-mono text-accent-700">b0be626</code></> : "Searching"}
          </p>
          <p className="text-body-2-regular text-text-secondary">{done ? "Next: revert it to prove it" : "Each probe runs the evals"}</p>
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value, sub }: { label: string; value: string; sub: string }) {
  return (
    <div className="rounded-2xl bg-background-secondary-default p-4">
      <p className="text-caption-1-medium text-text-secondary">{label}</p>
      <p className="mt-1 text-headline-medium tabular-nums">{value}</p>
      <p className="text-body-2-regular text-text-secondary">{sub}</p>
    </div>
  );
}

#!/usr/bin/env bash
# Live demo path: Fixer -> Verifier on the R3 refund-amount investigation.
# Works on a throwaway clone of the demo repo and a copy of the saved state, so the
# real demo repo and evidence/ are untouched. Needs a logged-in `claude` CLI.
# usage: scripts/demo_fix_verify.sh [investigation-id]   (default fd47af3916)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ID="${1:-fd47af3916}"
WORK="${WORK:-/tmp/agentbisect-demo}"
SRC_REPO="${SRC_REPO:-$HOME/Downloads/codingprojects/support-agent-demo}"
rm -rf "$WORK"; mkdir -p "$WORK/state/investigations"
git clone -q "$SRC_REPO" "$WORK/repo"
cp "$ROOT/evidence/rerun3-smart/investigations/$ID.json" "$WORK/state/investigations/"
python3 - "$WORK" "$ID" <<'P'
import json,sys
w,i=sys.argv[1:3]; p=f"{w}/state/investigations/{i}.json"
d=json.load(open(p)); d["repo"]=f"{w}/repo"; json.dump(d,open(p,"w"),indent=2)
P
export DEMO_FLAKE=0 AGENTBISECT_BASELINE="$ROOT/evidence/baseline-1b40b27.json"
cd "$ROOT"
AB="uv run agentbisect --state-dir $WORK/state"
$AB fix "$ID" --llm-proxy auto --out "$WORK/fix"
SHA=$(git -C "$WORK/repo" rev-parse "refs/heads/agentbisect/fix-$(python3 -c "import json;print(json.load(open('$WORK/state/investigations/$ID.json'))['culprit']['sha'][:8])")")
echo "fix commit: $SHA"
$AB verify-fix "$ID" --commit "$SHA" --llm-proxy auto
echo "state/outputs in $WORK"

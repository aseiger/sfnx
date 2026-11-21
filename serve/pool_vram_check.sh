#!/bin/bash
# The K/V pool from free VRAM, proven against the pool from --max-context.
#
# Runs the same long conversations twice through the front end: once with CONFIG as given (it should set
# FNX_KV_RESERVE_MIB, which sizes the pool from every card's free VRAM - docs/config.md) and once with that one setting
# removed, so the pool is back at 1.714 x --max-context positions. The control is expected to run short and park a
# conversation to host memory; the run with the knob is expected not to. MAX_NEW is deliberately large: prompt reads
# serialise (~45 s for 160k tokens), so with short replies every conversation would finish before the last was admitted
# and nothing would ever hold context at the same time.
#
#   serve/pool_vram_check.sh CONFIG [TOK] [MAX_NEW]        (default 160000 tokens, 6000 tokens a reply)
# Stops sfnx-test as it goes; leave prod alone or stop it yourself first - the pool is sized from free VRAM at load, so
# anything else holding the cards shrinks it.
set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd); root=$(dirname "$here")
cfg=$(realpath "${1:?usage: serve/pool_vram_check.sh CONFIG [TOK] [MAX_NEW]}")
TOK=${2:-160000}
MAXNEW=${3:-6000}
ctl=$root/serve/.pool_vram_control.json

port() { python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['port'])" "$1"; }
# the engine's own shortage lines only - "conversation cache: parked N tokens" is host-side LRU
pressure() { grep -cE "K/V pool short|K/V pool full|waits for a page|slot [0-9]+ paused" "$1"; }

python3 - "$cfg" "$ctl" <<'PY' || exit 1
import json, sys
c = json.load(open(sys.argv[1]))
if "FNX_KV_RESERVE_MIB" not in c.get("env", {}):
    sys.exit(f"{sys.argv[1]} sets no FNX_KV_RESERVE_MIB: there would be nothing to compare")
c["port"] += 1; c["env"].pop("FNX_KV_RESERVE_MIB")
json.dump(c, open(sys.argv[2], "w"), indent=1)
PY
p_ctl=$(port "$ctl"); p_cfg=$(port "$cfg")
log_ctl=$root/serve/pool-vram-control.log; log_cfg=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['log'])" "$cfg")

for run in control test; do
    if [ "$run" = control ]; then c=$ctl; p=$p_ctl; lg=$log_ctl; n=3; say="control, pool from --max-context"
    else c=$cfg; p=$p_cfg; lg=$log_cfg; n=4; say="with FNX_KV_RESERVE_MIB, pool from free VRAM"; fi
    echo; echo "===== $say: $n x $TOK tokens, $MAXNEW tokens a reply ====="
    : > "$lg" 2>/dev/null || sudo rm -f "$lg"
    "$here/run.sh" "$c" > /dev/null 2>&1
    up=""
    for _ in $(seq 120); do curl -sf -m 3 "http://127.0.0.1:$p/v1/models" > /dev/null && { up=1; break; }; sleep 5; done
    [ -n "$up" ] || { echo "!! the front end did not come up on $p"; tail -20 "$lg"; exit 1; }
    grep -E "K/V pool from free VRAM|positions free" "$lg" | tail -2 | sed 's/^/  /'
    "$root/.venv/bin/python" "$here/pool_fill.py" "http://127.0.0.1:$p/v1" "$n" "$TOK" "$MAXNEW" 2>&1 | tail -8 | sed 's/^/  /'
    eval "press_$run=$(pressure "$lg")"
    echo "  pool shortage lines: $(eval echo "\$press_$run")"
done

echo
echo "control: ${press_control:-?} shortage lines over 3 x $TOK; with the knob: ${press_test:-?} over 4 x $TOK"
if [ "${press_control:-0}" -gt 0 ] && [ "${press_test:-0}" = 0 ]; then echo "PASS: the pool was the limit before, and is not now."
elif [ "${press_control:-0}" = 0 ]; then echo "INCONCLUSIVE: the control showed no shortage either - raise TOK or MAX_NEW."
else echo "CHECK: shortage with the knob too - read $log_cfg"; fi

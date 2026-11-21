#!/usr/bin/env python3
"""Fill the K/V pool with several long conversations at once: C of about TOK tokens each, decoded together, so they
all hold their pages at the same time. Reports what the server counted, so a fill that came out short is visible.

The point is a total well past the 449,856 positions an arena-off pool used to hold: C conversations of about TOK
tokens each, read in (the prompt path serialises them) and then decoded together, so they all hold their pages at the
same time. A probe run measures how many tokens this model's tokenizer makes of the filler words, so "TOK" is what the
server ends up counting rather than a guess from word counts.

    serve/pool_fill.py [BASE_URL] [C] [TOK] [MAX_NEW]

Read the engine log afterwards for `K/V pool short`, `K/V pool full` and `slot N paused`: those are the shortage
lines. `conversation cache: parked N tokens` is ordinary host-side LRU and says nothing about the pool.
"""
import json
import random
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8096/v1"
C = int(sys.argv[2]) if len(sys.argv) > 2 else 4
TOK = int(sys.argv[3]) if len(sys.argv) > 3 else 160_000
MAX_NEW = int(sys.argv[4]) if len(sys.argv) > 4 else 64

WORDS = ("server cache kernel tensor socket memory request decode prefill expert layer token batch lane queue page "
         "window ring buffer weight head scale pool slot").split()


def post(words, max_new):
    body = json.dumps({"model": "x", "messages": [{"role": "user", "content": words}],
                       "max_tokens": max_new, "temperature": 0}).encode()
    req = urllib.request.Request(BASE + "/chat/completions", data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3600) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:                       # the API's own reason is the finding: keep it
        return None, f"HTTP {e.code}: {e.read()[:300].decode('utf8', 'replace')}"
    except Exception as e:                                    # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"


def filler(n_words, i):
    rnd = random.Random(9000 + i)
    return " ".join(rnd.choice(WORDS) for _ in range(n_words))


probe, err = post(filler(2000, 99) + "\n\nReply with the single word ok.", 8)
if not probe or probe.get("usage", {}).get("prompt_tokens") is None:
    sys.exit(f"probe failed at {BASE}: {err or probe}")
tpw = probe["usage"]["prompt_tokens"] / 2000.0
print(f"{BASE}: this tokenizer makes {tpw:.3f} tokens per filler word; aiming at ~{TOK:,} tokens a conversation")

res = [None] * C


def one(i):
    n_words = max(16, int(TOK / tpw) - 40)
    t0 = time.time()
    body, err = post(filler(n_words, i) + "\n\nIn one short sentence, what was in this context?", MAX_NEW)
    if body and body.get("choices"):
        u = body.get("usage", {})
        res[i] = {"ok": True, "s": time.time() - t0, "pt": u.get("prompt_tokens"), "ct": u.get("completion_tokens"),
                  "finish": body["choices"][0].get("finish_reason")}
    else:
        res[i] = {"ok": False, "s": time.time() - t0, "err": err or json.dumps(body)[:200]}


ts = [threading.Thread(target=one, args=(i,)) for i in range(C)]
print(f"{C} requests x ~{TOK:,} prompt tokens ({C * TOK:,} positions wanted), {MAX_NEW} tokens each")
t0 = time.time()
for t in ts:
    t.start()
for t in ts:
    t.join()
wall = time.time() - t0

filled = 0
for i, r in enumerate(res):
    if r and r.get("ok"):
        filled += r["pt"] or 0
        print(f"  {i}: {r['pt']:,} prompt tokens, {r['ct']} generated in {r['s']:.0f} s ({r['finish']})")
    else:
        print(f"  {i}: FAILED after {r['s']:.0f} s: {r.get('err')}")

bad = [i for i, r in enumerate(res) if not (r and r.get("ok"))]
under = [i for i, r in enumerate(res) if r and r.get("ok") and (r["pt"] or 0) < 0.9 * TOK]
print(f"\nwall {wall:.0f} s; the server counted {filled:,} prompt tokens across the {C} conversations")
if bad:
    print(f"FAIL: {len(bad)} of {C} did not complete")
    sys.exit(1)
if under:
    print(f"FAIL: {len(under)} conversations came in under 90% of the target - the fill is not what the test assumes")
    sys.exit(1)
print(f"OK: all {C} completed, each holding ~{TOK:,} tokens ({filled:,} positions filled together)")

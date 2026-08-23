#!/usr/bin/env python3
"""
Curia autoheal — keep CU_Categories.m3u playable.

For every channel in the playlist, probe its current stream URL. If it's dead,
try to swap in a working alternate for the same channel drawn from a set of
candidate pools. If nothing works, park the channel (comment it out) so players
don't show a dead tile; a parked channel is automatically restored on a later
run if a source comes back.

Design notes vs the Japanese "Yban" healer this is modelled on:
  * Korea has no single hardcoded fallback host, so there are no izzi-style
    slug builders here. Alternates come purely from the candidate pools.
  * Candidate pools are matched to our channels by tvg-id first, then by a
    normalized channel name, because the public Korean lists use romanized ids
    while our playlist uses native Korean ids.
  * Stream-only: this never rewrites tvg-id, tvg-logo, group-title or the EPG
    url — only the URL line under each #EXTINF.

Candidate pools (fetched fresh each run, with a committed local fallback):
  1. hujingguang/ChinaIPTV  -> southKorea.m3u8
  2. wcb1969/iptv           -> 韩国.txt
  3. iptv-org/iptv          -> streams/kr.m3u   (streams only; its EPG is poor)
  4. kr_pool_hans.m3u       -> committed hanssettings Korean bundle (stable seed)
"""

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.request
from urllib.error import URLError, HTTPError

PLAYLIST_MAIN = "CU_Categories.m3u"
PLAYLISTS = [PLAYLIST_MAIN]

# Committed local candidate pool (always present, never depends on network).
LOCAL_POOLS = ["kr_pool_hans.m3u", "kr_pool_korea2.m3u8"]

# Live candidate pools, fetched each run. Raw URLs.
REMOTE_POOLS = [
    "https://raw.githubusercontent.com/hujingguang/ChinaIPTV/main/southKorea.m3u8",
    "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/kr.m3u",
]

# A self-refreshing JSON source (someone maintains this upstream). Parsed
# separately because it's JSON, not m3u. Contributes fresh URLs each run.
REMOTE_JSON_POOLS = [
    "http://141.164.53.195/live/korea-live.json",
]

# Hosts known dead / not to be used as replacements.
DEAD_HOSTS = set()

# Hosts to never import (e.g. anything embedding credentials). None known yet.
BANNED_HOSTS = set()

GIT_BRANCH = "main"
STATE_FILE = ".autoheal_state.json"

PROBE_TIMEOUT = 10
UA = "VLC/3.0.20 LibVLC/3.0.20"

_ctx = ssl.create_default_context()


# ---------------------------------------------------------------- helpers
def read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def write(p, t):
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(t)


def norm(s):
    return re.sub(r"[^a-z0-9\uac00-\ud7a3]", "", (s or "").lower())


def host_of(url):
    m = re.match(r"https?://([^/:]+)", url or "")
    return m.group(1) if m else ""


def entries(text):
    """Yield (extinf_index, tvg_id, name, url_index, url) for each channel.
    url_index/url are None for a parked (commented) channel."""
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        is_ext = line.startswith("#EXTINF")
        is_parked = line.startswith("# PARKED #EXTINF")
        if is_ext or is_parked:
            tid = re.search(r'tvg-id="([^"]*)"', line)
            name = line.split(",", 1)[-1].strip()
            # find url (or parked url) before next #EXTINF
            j = i + 1
            url_idx, url = None, None
            while j < len(lines):
                lj = lines[j]
                if lj.startswith("#EXTINF") or lj.startswith("# PARKED #EXTINF"):
                    break
                if lj.startswith("http") or lj.startswith("# PARKED http"):
                    url_idx = j
                    url = lj.replace("# PARKED ", "", 1)
                    break
                j += 1
            out.append({
                "ext_idx": i,
                "tvg_id": tid.group(1) if tid else "",
                "name": name,
                "url_idx": url_idx,
                "url": url,
                "parked": is_parked,
            })
        i += 1
    return out


# ---------------------------------------------------------------- candidate pools
def parse_pool(text):
    """Return list of (name, tvg_id, url) from an m3u/txt candidate pool."""
    out = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF"):
            tid = re.search(r'tvg-id="([^"]*)"', line)
            name = line.split(",", 1)[-1].strip()
            j = i + 1
            while j < len(lines) and not lines[j].startswith("http"):
                j += 1
            if j < len(lines):
                out.append((name, tid.group(1) if tid else "", lines[j].strip()))
            i = j + 1
        elif "," in line and i + 1 < len(lines) and lines[i + 1].startswith("http"):
            # wcb1969 韩国.txt style: "Name,http://url"
            if line.startswith("http"):
                i += 1
                continue
            name = line.split(",", 1)[0].strip()
            url = line.split(",", 1)[1].strip()
            if url.startswith("http"):
                out.append((name, "", url))
            i += 1
        else:
            i += 1
    return out


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=30, context=_ctx).read().decode(
        "utf-8", errors="replace")


def build_candidate_index(verbose=False):
    """id_or_normname -> [urls] harvested from all candidate pools."""
    idx = {}

    def add(name, tid, url):
        if not url.startswith("http"):
            return
        h = host_of(url)
        if h in DEAD_HOSTS or h in BANNED_HOSTS:
            return
        for key in filter(None, [tid, norm(name),
                                 norm(tid.replace(".kr", "").replace(".us", ""))]):
            idx.setdefault(key, [])
            if url not in idx[key]:
                idx[key].append(url)

    # local pools first (stable), then remote (fresh)
    for lp in LOCAL_POOLS:
        if os.path.exists(lp):
            for name, tid, url in parse_pool(read(lp)):
                add(name, tid, url)
            if verbose:
                print(f"  pool {lp}: loaded")

    for purl in REMOTE_POOLS:
        try:
            txt = fetch_text(purl)
            n = 0
            for name, tid, url in parse_pool(txt):
                add(name, tid, url)
                n += 1
            if verbose:
                print(f"  pool {purl.split('/')[-1]}: {n} entries")
        except (URLError, HTTPError, Exception) as exc:
            if verbose:
                print(f"  pool {purl.split('/')[-1]}: FAILED ({exc})")

    # JSON pools: {channel-name/id: url or [urls]} shapes vary, so be liberal
    import json as _json
    for jurl in REMOTE_JSON_POOLS:
        try:
            data = _json.loads(fetch_text(jurl))
            n = 0
            def walk(obj, label=""):
                nonlocal n
                if isinstance(obj, str):
                    if obj.startswith("http"):
                        add(label, "", obj); n += 1
                elif isinstance(obj, list):
                    for v in obj: walk(v, label)
                elif isinstance(obj, dict):
                    for k, v in obj.items():
                        walk(v, k if isinstance(k, str) else label)
            walk(data)
            if verbose:
                print(f"  json pool {jurl.split('/')[-1]}: {n} urls")
        except Exception as exc:
            if verbose:
                print(f"  json pool {jurl.split('/')[-1]}: FAILED ({exc})")

    return idx


def candidates_for(tid, name, idx):
    seen = []
    for key in filter(None, [tid, norm(name),
                             norm((tid or "").replace(".kr", "").replace(".us", ""))]):
        for u in idx.get(key, []):
            if u not in seen:
                seen.append(u)
    return seen


# ---------------------------------------------------------------- probing
def probe_status(url):
    """Return 'ok' (played/200), 'refused' (403/429/5xx - exists but blocked),
    or 'dead' (DNS/timeout/4xx). Distinguishing refused from ok lets us PREFER
    a genuinely-playable alternate over one that only answers with a block."""
    host = host_of(url)
    if host in DEAD_HOSTS or host in BANNED_HOSTS:
        return "dead"
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT, context=_ctx) as r:
            r.read(2048)
            return "ok"
    except HTTPError as e:
        if e.code in (403, 429) or 500 <= e.code < 600:
            return "refused"
        return "dead"
    except (URLError, ssl.SSLError, TimeoutError, OSError):
        return "dead"
    except Exception:
        return "dead"


def probe(url):
    """Back-compat: alive if not dead (refused counts as alive)."""
    return probe_status(url) != "dead"


# ---------------------------------------------------------------- state
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.loads(read(STATE_FILE))
        except Exception:
            return {}
    return {}


def save_state(st):
    write(STATE_FILE, json.dumps(st, ensure_ascii=False, indent=0))


# ---------------------------------------------------------------- git
def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True)


def push(files, msg):
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "github-actions[bot]@users.noreply.github.com")
    git("add", "-A", "--", *files)
    st = git("diff", "--staged", "--quiet")
    if st.returncode == 0:
        print("No changes to commit")
        return
    git("commit", "-m", msg)
    pull = git("pull", "--rebase", "origin", GIT_BRANCH)
    if pull.returncode != 0:
        git("rebase", "--abort")
    git("push")


# ---------------------------------------------------------------- core
def heal_playlist(path, idx, state, check_only, verbose):
    text = read(path)
    lines = text.splitlines()
    chans = entries(text)

    healed = parked = restored = alive = no_source = 0

    for c in chans:
        tid, name = c["tvg_id"], c["name"]

        if c["parked"]:
            # try to bring it back
            cands = candidates_for(tid, name, idx)
            for u in cands:
                if probe(u):
                    if not check_only:
                        lines[c["ext_idx"]] = lines[c["ext_idx"]].replace(
                            "# PARKED ", "", 1)
                        lines[c["url_idx"]] = u
                    print(f"  [restore] {name} -> {host_of(u)}")
                    restored += 1
                    break
            continue

        url = c["url"]
        cur = probe_status(url) if url else "dead"

        if cur == "ok":
            alive += 1
            continue

        # Current URL is dead or only 'refused' (e.g. tving 403 from our region).
        # Look for an alternate. Prefer an 'ok' one; fall back to keeping a
        # 'refused' current url only if nothing better exists.
        cands = [u for u in candidates_for(tid, name, idx) if u != url]
        ok_alt = None
        for u in cands:
            if probe_status(u) == "ok":
                ok_alt = u
                break

        if ok_alt:
            if not check_only and c["url_idx"] is not None:
                lines[c["url_idx"]] = ok_alt
            print(f"  [heal] {name}: {host_of(url)} ({cur}) -> {host_of(ok_alt)} (ok)")
            healed += 1
        elif cur == "refused":
            # keep it: it exists, just blocked here; a player elsewhere/VPN may
            # still reach it, and we have nothing genuinely better.
            alive += 1
            print(f"  [keep] {name}: {host_of(url)} refused here, no ok alternate")
        else:
            if cands:
                no_source += 1
            if not check_only and c["url_idx"] is not None:
                lines[c["ext_idx"]] = "# PARKED " + lines[c["ext_idx"]]
                lines[c["url_idx"]] = "# PARKED " + lines[c["url_idx"]]
            print(f"  [park] {name} (no working source)")
            parked += 1

    if not check_only:
        write(path, "\n".join(lines) + "\n")

    print(f"\n{path}: {alive} alive, {healed} healed, {restored} restored, "
          f"{parked} parked ({no_source} had candidates but none live now)")
    return healed + restored + parked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="dry run: report, write nothing")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--lint", action="store_true",
                    help="structural check of the playlist, then exit")
    args = ap.parse_args()

    if args.lint:
        for p in PLAYLISTS:
            t = read(p)
            assert t.lstrip().startswith("#EXTM3U"), f"{p}: missing #EXTM3U"
            n = t.count("#EXTINF")
            assert n >= 20, f"{p}: only {n} channels"
        print("Playlists look structurally clean.")
        return

    print("Building candidate index from pools...")
    idx = build_candidate_index(verbose=args.verbose)
    total_urls = sum(len(v) for v in idx.values())
    print(f"  candidate index: {len(idx)} keys, {total_urls} urls")

    state = load_state()
    changed = 0
    for p in PLAYLISTS:
        changed += heal_playlist(p, idx, state, args.check, args.verbose)

    if not args.check:
        save_state(state)


if __name__ == "__main__":
    main()
 

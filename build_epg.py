#!/usr/bin/env python3
"""
Build curia_epg.xml.gz by merging three live Korean EPG sources, keeping the
deepest guide available for each channel the playlist actually uses.

Priority when a channel appears in more than one source: whichever source has
the most programmes for it wins (typically iptv-epg, then epgshare, then
open-epg). Only channels referenced by CU_Categories.m3u are emitted, so the
output stays small and every channel maps to a real tvg-id.

If a source fails to download or parse, it is skipped and the others are used;
the previously committed curia_epg.xml.gz is only overwritten if we produced a
valid result with a reasonable channel count, so a bad fetch never blanks the
guide.
"""

import gzip
import io
import re
import sys
import urllib.request

from lxml import etree

PLAYLIST = "CU_Categories.m3u"
OUT = "curia_epg.xml.gz"

SOURCES = [
    # name, url, priority (higher = preferred when depth ties)
    ("iptv-epg", "https://iptv-epg.org/files/epg-kr.xml.gz", 3),
    ("epgshare", "https://epgshare01.online/epgshare01/epg_ripper_KR1.xml.gz", 2),
    ("open-epg", "https://www.open-epg.com/files/korea1.xml.gz", 1),
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
TIMEOUT = 90
MIN_CHANNELS = 40  # refuse to overwrite the committed file below this


def norm(s):
    return re.sub(r"[^a-z0-9\uac00-\ud7a3]", "", (s or "").lower())


def wanted_ids_and_names():
    """From the playlist: the set of tvg-ids we care about, plus a
    normalized-name -> tvg-id bridge for matching romanized source ids."""
    ids = set()
    bridge = {}
    cur_id = None
    for line in open(PLAYLIST, encoding="utf-8"):
        if line.startswith("#EXTINF"):
            m = re.search(r'tvg-id="([^"]*)"', line)
            name = line.split(",", 1)[-1].strip()
            cur_id = m.group(1) if m else ""
            if cur_id:
                ids.add(cur_id)
                bridge[norm(name)] = cur_id
                bridge[norm(cur_id.replace(".kr", "").replace(".us", ""))] = cur_id
    return ids, bridge


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=TIMEOUT).read()
    data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    return etree.fromstring(data, etree.XMLParser(recover=True, huge_tree=True))


def main():
    wanted, bridge = wanted_ids_and_names()
    print(f"playlist references {len(wanted)} tvg-ids")

    # per our-id: best (priority_key, source_name, root, source_channel_id)
    best = {}          # our_id -> (score, sname, src_cid)
    roots = {}         # sname -> parsed root
    prio = {}          # sname -> priority

    for sname, url, priority in SOURCES:
        try:
            root = fetch(url)
        except Exception as exc:
            print(f"  {sname}: FETCH FAILED ({exc}) - skipping")
            continue
        roots[sname] = root
        prio[sname] = priority

        id_to_name = {}
        for c in root.findall("channel"):
            dn = c.find("display-name")
            id_to_name[c.get("id")] = dn.text if dn is not None else ""

        counts = {}
        for pr in root.findall("programme"):
            cid = pr.get("channel")
            counts[cid] = counts.get(cid, 0) + 1

        mapped = 0
        for src_cid, cnt in counts.items():
            our = None
            if src_cid in wanted:
                our = src_cid
            else:
                k1 = norm(src_cid.replace(".kr", "").replace(".us", ""))
                k2 = norm(id_to_name.get(src_cid, ""))
                our = bridge.get(k1) or bridge.get(k2)
            if not our:
                continue
            mapped += 1
            # score: programme count dominates, priority breaks ties
            score = (cnt, priority)
            if our not in best or score > best[our][0]:
                best[our] = (score, sname, src_cid)
        print(f"  {sname}: {len(counts)} channels, mapped {mapped} to playlist")

    if not best:
        print("ERROR: no EPG data mapped from any source")
        # leave any existing committed file untouched
        sys.exit(0)

    # Assemble output: one <channel> + its <programme>s per covered playlist id
    tv = etree.Element("tv")
    tv.set("generator-info-name", "curia-epg-merger")

    # cache programmes per (sname) once
    progs_by_source = {}
    for sname, root in roots.items():
        d = {}
        for pr in root.findall("programme"):
            d.setdefault(pr.get("channel"), []).append(pr)
        progs_by_source[sname] = d

    channels_written = 0
    programmes_written = 0
    for our_id, (score, sname, src_cid) in sorted(best.items()):
        ch = etree.SubElement(tv, "channel")
        ch.set("id", our_id)
        dn = etree.SubElement(ch, "display-name")
        dn.text = our_id.replace(".kr", "").replace(".us", "")
        channels_written += 1
        for pr in progs_by_source[sname].get(src_cid, []):
            newpr = etree.fromstring(etree.tostring(pr))
            newpr.set("channel", our_id)  # rewrite to our id
            tv.append(newpr)
            programmes_written += 1

    if channels_written < MIN_CHANNELS:
        print(f"ERROR: only {channels_written} channels (< {MIN_CHANNELS}); "
              "keeping previous committed file")
        sys.exit(0)

    xml = etree.tostring(tv, encoding="utf-8", xml_declaration=True)
    with gzip.open(OUT, "wb") as fh:
        fh.write(xml)
    print(f"\nwrote {OUT}: {channels_written} channels, "
          f"{programmes_written} programmes")


if __name__ == "__main__":
    main()

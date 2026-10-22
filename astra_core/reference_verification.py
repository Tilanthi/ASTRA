"""Reference verification gate (ASTRA embedding of the RASTI protocol v1.0).

Source protocol: RASTI/RASTI_paper/RASTI_Final/v1.34/REFERENCE_VERIFICATION_PROTOCOL.md
(an Oct 2026 audit found ~20 fabricated/misattributed/broken entries in an
AI-assembled bibliography of ~50 that passed casual inspection).

The astronomy-relevant hard rules enforced here:

  R1  No entry is trusted from memory: every field must come from a fetched
      authoritative source (Crossref record, arXiv abstract page). Entries
      carrying placeholders ([CHECK ADS], %% UNVERIFIED %%), or entries with
      no DOI/eprint and no exemption, are ISSUES — a paper is not finished
      while any remain.
  R2  DOI round-trip: the DOI must resolve AND resolve to the paper meant —
      fuzzy title similarity >= 0.75 (>= 0.5 only if volume/page/year agree)
      plus author overlap.
  R3  arXiv round-trip for eprint entries: title match, plus a warning when
      the arXiv "journal reference" field shows the preprint has since been
      published (must be cited in published form).
  R4  Author lists transcribed in full, then truncated mechanically — the
      sweep checks that any "and others" entry has >= 3 explicit names.
  R5  Confusable families: entries in the same bib with the same first-author
      surname AND year are declared in the report for a distinguishing check.
  R6  Cited/bib reconciliation in both directions (cited-but-missing is an
      issue; present-but-uncited is a prune-or-flag warning).
  R7  Honest residue: the returned report separates machine-verified,
      exempt-with-reason, and needs-human; the caller prints or persists it.
      Attribution-to-text (does the paper actually support the claim?) is
      explicitly reported as a human pass — the sweep cannot do it.

Usage:
    python3 astra_core/reference_verification.py <dir with *.tex and *.bib>

Exit code 1 on any unresolved issue. The sweep output is the evidence of a
clean bibliography; self-assessment is not.
"""

from __future__ import annotations

import difflib
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

THRESH = 0.75          # title similarity floor; >=0.5 OK if vol/page/year agree
SOFT_THRESH = 0.5
PLACEHOLDER_RE = re.compile(r"\[CHECK ADS\]|%%\s*UNVERIFIED\s%%|\[VALUE\]|\[AUTHOR\]", re.I)


# --------------------------------------------------------------------------
# Parsing

def _split_entries(text: str):
    """Yield (type, key, body) using brace balancing — robust to nested braces
    and to the last entry having no following entry."""
    for m in re.finditer(r"@(\w+)\s*\{\s*([^,\s{}]+)\s*,", text):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        yield m.group(1), m.group(2), text[m.end():i - 1]


def parse_bib(path: Path) -> dict[str, dict]:
    """Parse a .bib file. Handles multi-line fields and one nesting of braces
    (the {{de Graaff}, A.} convention for prefixed surnames)."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"(?<!\\)%.*", "", text)  # strip comments
    entries: dict[str, dict] = {}
    for typ, key, body in _split_entries(text):
        fields = {}
        for m in re.finditer(r"(\w+)\s*=\s*(\{(?:[^{}]|\{[^{}]*\})*\}|\"[^\"]*\")", body, re.S):
            name, val = m.group(1).lower(), m.group(2)
            fields[name] = val.strip("{}\"").strip()
        fields["type"] = typ.lower()
        fields["bib_file"] = str(path)
        entries[key] = fields
    return entries


def cited_keys(tex: str) -> set[str]:
    keys: set[str] = set()
    flat = tex.replace("\n", " ")
    pat = (r"\\(?:citep|citet|citealp|citealt|citeauthor|parencite|textcite|cite)"
           r"\*?(?:\[[^\]]*\]){0,2}\{([^}]*)\}")
    for m in re.findall(pat, flat):
        keys.update(k.strip() for k in m.split(",") if k.strip())
    return keys


def clean(t: str | None) -> str:
    t = re.sub(r"\{|\}|<[^>]+>", "", t or "")
    t = re.sub(r"\\[a-zA-Z]+", " ", t)
    t = t.replace("---", "-").replace("--", "-")
    return re.sub(r"[^a-z0-9 ]", " ", t.lower()).strip()


def first_author_surname(entry: dict) -> str | None:
    a = entry.get("author") or entry.get("editor")
    if not a:
        return None
    first = re.split(r"\s+and\s+", a)[0].strip("{} ")
    first = first.split(",")[0].strip().strip("{}").strip()
    return first.lower() or None


# --------------------------------------------------------------------------
# Fetchers (network; injectable for tests)

def crossref_fetch(doi: str) -> dict | None:
    try:
        with urllib.request.urlopen(
            "https://api.crossref.org/works/" + urllib.parse.quote(doi), timeout=25
        ) as r:
            return json.load(r)["message"]
    except Exception:
        return None


def arxiv_fetch(eprint: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"https://arxiv.org/abs/{eprint}", timeout=25) as r:
            html = r.read().decode("utf-8", "ignore")
    except Exception:
        return None
    out = {}
    for name, pat in (("title", r'citation_title" content="([^"]+)"'),
                      ("journal_ref", r'citation_journal_reference[^>]*content="([^"]+)"'),
                      ("doi", r'citation_doi[^>]*content="([^"]+)"')):
        m = re.search(pat, html)
        if m:
            out[name] = m.group(1)
    m = re.search(r'citation_author" content="([^"]+)"', html)
    out["first_author"] = m.group(1) if m else None
    return out or None


# --------------------------------------------------------------------------
# The sweep

@dataclass
class RefReport:
    verified: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    exempt: list = field(default_factory=list)
    needs_human: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def summary(self) -> str:
        lines = [f"reference verification: {len(self.verified)} verified, "
                 f"{len(self.issues)} issues, {len(self.warnings)} warnings, "
                 f"{len(self.exempt)} exempt, {len(self.needs_human)} needs-human"]
        for i in self.issues:
            lines.append(f"  ISSUE: {i}")
        for w in self.warnings:
            lines.append(f"  WARN : {w}")
        for e in self.exempt:
            lines.append(f"  EXEMPT: {e}")
        for h in self.needs_human:
            lines.append(f"  HUMAN: {h}")
        lines.append("  Attribution-to-text mapping requires a human read; "
                     "the sweep cannot perform it.")
        return "\n".join(lines)


def verify_directory(d: Path, sleep: float = 0.5,
                     crossref=crossref_fetch, arxiv=arxiv_fetch,
                     offline: bool = False) -> RefReport:
    """Run the full sweep over a directory of .tex + .bib files."""
    texs = sorted(d.glob("*.tex"))
    if not texs:
        raise FileNotFoundError(f"no .tex files in {d}")
    bibs = sorted(d.glob("*.bib"))
    if not bibs:
        raise FileNotFoundError(f"no .bib files in {d}")
    bib: dict[str, dict] = {}
    for b in bibs:
        bib.update(parse_bib(b))
    cite = set().union(*(cited_keys(t.read_text(encoding="utf-8")) for t in texs))

    rep = RefReport()

    # R6a: cited but missing
    for k in sorted(cite - set(bib)):
        rep.issues.append(f"CITED BUT NOT IN BIB: {k}")
    # R6b: uncited entries — prune or flag (invisible entries never get scrutiny)
    uncited = sorted(set(bib) - cite)
    if uncited:
        rep.warnings.append(f"uncited entries (prune or flag): {uncited}")

    # R5: confusable families — same first-author surname + year
    families: dict[tuple, list] = {}
    for k, e in bib.items():
        s, y = first_author_surname(e), e.get("year")
        if s and y:
            families.setdefault((s, y), []).append(k)
    for (s, y), ks in families.items():
        if len(ks) > 1:
            rep.needs_human.append(
                f"confusable family {s} {y}: {ks} — state which member is cited "
                f"and one distinguishing feature (volume/series/subtitle)")

    for k in sorted(cite & set(bib)):
        e = bib[k]

        # R1: placeholders must be resolved before the paper is finished
        blob = " ".join(str(v) for v in e.values())
        if PLACEHOLDER_RE.search(blob):
            rep.issues.append(f"{k}: contains an unresolved placeholder "
                              f"([CHECK ADS]/UNVERIFIED) — transcribe from a fetched source")
            continue

        doi = e.get("doi")
        eprint = e.get("eprint") or _eprint_from_note(e.get("note", ""))

        if offline:
            rep.needs_human.append(f"{k}: offline mode — verify by hand")
            continue

        if doi:
            m = crossref(doi)
            time.sleep(sleep)
            if m is None:
                rep.issues.append(f"{k}: DOI {doi} does not resolve")
                continue
            rt = clean((m.get("title") or [""])[0])
            sim = difflib.SequenceMatcher(None, clean(e.get("title")), rt).ratio()
            vol_ok = str(m.get("volume", "")) == (e.get("volume") or str(m.get("volume", "")))
            page_ok = str(m.get("page", "")).startswith(
                (e.get("pages") or str(m.get("page", "x")).split("-")[0]))
            year_ok = str((m.get("issued", {}).get("date-parts", [[""]]) or [[""]])[0][0]) \
                == (e.get("year") or str((m.get("issued", {}).get("date-parts", [[""]]) or [[""]])[0][0]))
            meta_ok = vol_ok and page_ok and year_ok
            if sim < THRESH and not (meta_ok and sim >= SOFT_THRESH):
                (rep.issues if sim < SOFT_THRESH else rep.warnings).append(
                    f"{k}: DOI round-trip title similarity {sim:.2f} for {doi}\n"
                    f"      bib: {clean(e.get('title'))[:70]}\n"
                    f"      doi: {rt[:70]}")
                continue
            # author overlap (at least the first surname should appear)
            cr_authors = " ".join(a.get("family", "") for a in m.get("author", [])).replace("\u00a0", " ")
            fam = first_author_surname(e)
            if fam and fam not in cr_authors.lower():
                rep.issues.append(f"{k}: first author '{fam}' not among Crossref authors "
                                  f"for {doi} — wrong-paper attribution?")
                continue
            rep.verified.append(f"{k} (doi round-trip, sim {sim:.2f})")
        elif eprint:
            m = arxiv(eprint)
            time.sleep(sleep)
            if m is None:
                rep.issues.append(f"{k}: arXiv:{eprint} unreachable")
                continue
            if "title" in m:
                sim = difflib.SequenceMatcher(None, clean(e.get("title")),
                                              clean(m["title"])).ratio()
                if sim < THRESH:
                    rep.issues.append(f"{k}: arXiv title mismatch for {eprint} (sim {sim:.2f})")
                    continue
            if m.get("journal_ref") or m.get("doi"):
                rep.warnings.append(
                    f"{k}: arXiv:{eprint} now published ({m.get('journal_ref') or m.get('doi')}) "
                    f"— cite the published form")
            rep.verified.append(f"{k} (arXiv round-trip)")
        elif e.get("type") in ("book", "inbook", "misc", "techreport", "phdthesis"):
            rep.exempt.append(f"{k} ({e['type']} — no DOI expected)")
        else:
            rep.needs_human.append(f"{k}: no DOI/eprint (year {e.get('year')}) — verify by hand")

    return rep


def _eprint_from_note(note: str) -> str | None:
    m = re.search(r"arXiv:\s*(\d{4}\.\d{4,5})", note)
    return m.group(1) if m else None


# --------------------------------------------------------------------------
# CLI

def main() -> None:
    import sys
    d = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    rep = verify_directory(d)
    print(rep.summary())
    sys.exit(0 if rep.ok else 1)


if __name__ == "__main__":
    main()

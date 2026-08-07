#!/usr/bin/env python3
"""Stage pre-2021 competition archives into the shape the PDF ingester expects.

The archives published for 2015-2020 are not PDFs. CUMCM ships one ``.rar`` per
year holding Word statements -- sometimes ``.doc``, sometimes ``.docx``,
sometimes a nested ``.rar`` per problem -- while 2019 happens to ship real PDFs
alongside. Rather than teach the extractor three more container formats, this
tool normalizes everything to PDF once, locally, so
``ingest_full_problem_archives`` keeps its single input type.

Two host tools are required, and both are used only here:

* WinRAR's ``UnRAR.exe`` expands ``.rar``. Python has no stdlib reader for it.
* Word (via COM) converts ``.doc``/``.docx`` to PDF. It is the only converter on
  this workspace that keeps embedded figures and tables -- ``antiword`` returns
  text only, and LibreOffice is not installed. Figures matter: the 2016 A and
  2018 B statements carry real PNGs that a text-only path would silently drop.

Nothing here runs during a rebuild. ``datasets/raw/**`` is gitignored, so this
staging output stays on the machine that produced it, and the deterministic
rebuild gate reads the already-extracted ``problems.json`` instead. That keeps
the two host-tool dependencies out of CI.

Usage::

    python datasets/recipes/stage_legacy_archives.py fetch --years 2015-2020
    python datasets/recipes/stage_legacy_archives.py expand --years 2015-2020
    python datasets/recipes/stage_legacy_archives.py convert --years 2015-2020
    python datasets/recipes/stage_legacy_archives.py convert --groups apmcm-2016

``fetch`` and ``expand`` are CUMCM-specific -- they read the node table above.
``convert`` is not: it walks whatever staged directory it is pointed at, so the
APMCM archives (fetched separately, same ``extracted-v2`` root) convert through
the same code path.

``convert`` needs ``pywin32``, which the bundled interpreter does not carry; run
it from a venv that does. The other two commands run on any interpreter.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_ROOT = ROOT / "datasets/raw/sources/full-problem-archives"
EXTRACTED_ROOT = ARCHIVE_ROOT / "extracted-v2"
STAGE_LOG = ARCHIVE_ROOT / "legacy-staging.json"

USER_AGENT = "OpenMathModelDatasetBot/0.1 (+https://github.com/IvanCodesDev/OpenMathModel)"
REQUEST_INTERVAL = 6.0

# Resolved from the official archive index at
# https://www.mcm.edu.cn/html_cn/block/8579f5fce999cdc896f78bca5d4f8237.html
CUMCM_NODES = {
    2015: "ac8b96613522ef62c019d1cd45a125e3",
    2016: "6d026d84bd785435f92e3079b4a87a2b",
    2017: "460baf68ab0ed0e1e557a0c79b1c4648",
    2018: "7cec7725b9a0ea07b4dfd175e8042c33",
    2019: "b0ae8510b9ec0cc0deb2266d2de19ecb",
    2020: "10405905647c52abfd6377c0311632b5",
}

UNRAR_CANDIDATES = (
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "WinRAR/UnRAR.exe",
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "WinRAR/Rar.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "WinRAR/UnRAR.exe",
)

# Word writes these next to the statement; they are not part of any problem.
SKIP_STEMS = {"format2015", "format2016", "format2017", "format2018", "format2019", "format2020"}


def log(message: str) -> None:
    print(message, flush=True)


def find_unrar() -> Path:
    for candidate in UNRAR_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise SystemExit(
        "UnRAR.exe not found. Install WinRAR or set PROGRAMFILES so one of these resolves:\n  "
        + "\n  ".join(str(item) for item in UNRAR_CANDIDATES)
    )


def parse_years(value: str) -> list[int]:
    years: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            years.update(range(int(start), int(end) + 1))
        else:
            years.add(int(part))
    unknown = sorted(years - set(CUMCM_NODES))
    if unknown:
        raise SystemExit(f"No archive node recorded for year(s): {unknown}")
    return sorted(years)


def read_log() -> dict[str, Any]:
    if STAGE_LOG.is_file():
        return json.loads(STAGE_LOG.read_text(encoding="utf-8"))
    return {"fetched": {}, "expanded": {}, "converted": {}}


def write_log(data: dict[str, Any]) -> None:
    STAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
    STAGE_LOG.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def archive_link(year: int) -> str:
    """Read the year's archive URL off its official node page.

    The href is a content-hashed path under ``/upload_cn/node/<id>/``, so it
    cannot be constructed from the year; it has to be read from the page.
    """
    page = get(f"https://www.mcm.edu.cn/html_cn/node/{CUMCM_NODES[year]}.html").decode("utf-8", "replace")
    hits = re.findall(r'href="(/upload_cn/[^"]+\.(?:rar|zip))"', page, re.I)
    if not hits:
        raise RuntimeError(f"No archive link on the {year} node page")
    return "https://www.mcm.edu.cn" + hits[0]


def cmd_fetch(years: list[int]) -> None:
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    data = read_log()
    for year in years:
        target = ARCHIVE_ROOT / f"cumcm-{year}.rar"
        if target.is_file() and target.stat().st_size > 0:
            log(f"[fetch] {year} already staged ({target.stat().st_size} bytes)")
            continue
        url = archive_link(year)
        log(f"[fetch] {year} <- {url}")
        target.write_bytes(get(url))
        data["fetched"][str(year)] = {"url": url, "bytes": target.stat().st_size}
        write_log(data)
        log(f"[fetch] {year} stored {target.stat().st_size} bytes")
        time.sleep(REQUEST_INTERVAL)


def unrar(unrar_exe: Path, archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    # -o+ overwrites, so a re-run is idempotent; -y answers the recovery-record
    # and next-volume prompts that would otherwise block a non-interactive run.
    result = subprocess.run(
        [str(unrar_exe), "x", "-o+", "-y", str(archive), str(destination) + os.sep],
        capture_output=True,
    )
    if result.returncode != 0:
        detail = result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace")
        raise RuntimeError(f"UnRAR failed on {archive.name} (exit {result.returncode}):\n{detail.strip()}")


def expand_nested(unrar_exe: Path, root: Path) -> int:
    """Expand ``.rar``/``.zip`` members in place until none are left.

    2016 wraps each of C and D in its own inner ``.rar``, and 2018 B wraps its
    second appendix, so one pass is not enough. Each inner archive expands into a
    sibling ``<stem>_x`` directory and the archive itself is removed -- leaving it
    would make the ingester publish a container the reader cannot open anyway.
    """
    expanded = 0
    for _ in range(6):
        pending = [item for item in sorted(root.rglob("*"))
                   if item.is_file() and item.suffix.lower() in {".rar", ".zip"}]
        if not pending:
            break
        for archive in pending:
            inner = archive.parent / f"{archive.stem}_x"
            log(f"[expand]   nested {archive.relative_to(root).as_posix()}")
            unrar(unrar_exe, archive, inner)
            archive.unlink()
            expanded += 1
    return expanded


# Statement names drift year to year, in two families:
#   2015-2019  "CUMCM-2018-Problem-A-Chinese", "CUMCM2016-problem-B-..."
#   2020       "2020A-炉温曲线"  (no "problem" anywhere)
# The trailing guard stops "2016C" inside a longer token, and "Problem-D" from
# also matching the "D" of an unrelated word.
STATEMENT_PATTERNS = (
    re.compile(r"problem[-\s_]*([A-E])(?![A-Za-z0-9])", re.I),
    re.compile(r"20\d{2}[-\s_]*([A-E])(?![A-Za-z0-9])"),
)
# Tokens the letter patterns would otherwise match on a non-statement file:
# appendices ("...Problem-B-Chinese-Appendix1.doc"), download notes, readmes.
NOT_STATEMENT = ("appendix", "附件", "readme", "说明")
STATEMENT_SUFFIXES = {".doc", ".docx", ".pdf", ".wps"}


def statement_letter(path: Path) -> str | None:
    """Return the problem letter this file states, or None if it is not one."""
    if path.suffix.lower() not in STATEMENT_SUFFIXES:
        return None
    if path.stem.lower() in SKIP_STEMS:
        return None
    lowered = path.name.lower()
    if any(token in lowered for token in NOT_STATEMENT):
        return None
    for pattern in STATEMENT_PATTERNS:
        match = pattern.search(path.stem)
        if match:
            return match.group(1).upper()
    return None


def cmd_expand(years: list[int]) -> None:
    unrar_exe = find_unrar()
    data = read_log()
    for year in years:
        archive = ARCHIVE_ROOT / f"cumcm-{year}.rar"
        if not archive.is_file():
            raise SystemExit(f"Archive not staged for {year}; run `fetch` first.")
        group = EXTRACTED_ROOT / f"cumcm-{year}"
        if group.exists():
            shutil.rmtree(group)
        log(f"[expand] {year} <- {archive.name}")
        unrar(unrar_exe, archive, group)
        nested = expand_nested(unrar_exe, group)
        letters = sorted({letter for item in group.rglob("*") if item.is_file()
                          and (letter := statement_letter(item))})
        data["expanded"][str(year)] = {"nested_archives": nested, "letters": letters}
        write_log(data)
        log(f"[expand] {year} letters={''.join(letters) or '-'} nested={nested}")


WD_FORMAT_PDF = 17


def convert_to_pdf(word: Any, source: Path, target: Path) -> None:
    """Export one Word statement to PDF, figures and tables included.

    Word is the only converter available here that keeps inline shapes; a
    text-only path would drop the 2016 A and 2018 B figures. Paths must be
    absolute native strings -- Word rejects relative forward-slash names.
    """
    document = word.Documents.Open(
        str(source), ReadOnly=True, AddToRecentFiles=False, Visible=False,
        ConfirmConversions=False,
    )
    try:
        document.SaveAs2(str(target), FileFormat=WD_FORMAT_PDF)
    finally:
        document.Close(SaveChanges=0)


def cmd_convert(groups: list[str]) -> None:
    """Convert every Word statement in the named staged groups to PDF.

    Keyed on group directory rather than year because APMCM needs it too: its
    2016 archive ships ``.doc`` statements while 2015, 2017, 2018 and 2026 ship
    PDFs already.
    """
    try:
        import win32com.client as win32
    except ImportError:
        raise SystemExit(
            "convert needs pywin32. Run it from a venv that has it, e.g.\n"
            "  py -m venv .docvenv && .docvenv/Scripts/pip install pywin32==306"
        )
    data = read_log()
    word = win32.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0
    try:
        for name in groups:
            group = EXTRACTED_ROOT / name
            if not group.is_dir():
                raise SystemExit(f"{group} missing; run `expand` first.")
            converted: list[str] = []
            for source in sorted(group.rglob("*")):
                if not source.is_file() or statement_letter(source) is None:
                    continue
                if source.suffix.lower() == ".pdf":
                    continue
                target = source.with_suffix(".pdf")
                if target.is_file():
                    log(f"[convert] {name} {source.name} -> already converted")
                    continue
                log(f"[convert] {name} {source.name}")
                convert_to_pdf(word, source.resolve(), target.resolve())
                converted.append(target.relative_to(EXTRACTED_ROOT).as_posix())
            data["converted"][name] = converted
            write_log(data)
            log(f"[convert] {name} produced {len(converted)} PDF(s)")
    finally:
        word.Quit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["fetch", "expand", "convert"])
    parser.add_argument("--years", default="2015-2020", help="e.g. 2015-2020 or 2016,2018")
    parser.add_argument(
        "--groups",
        help="convert only: staged directories under extracted-v2, e.g. apmcm-2016,cumcm-2017. "
        "Defaults to the cumcm-<year> groups implied by --years.",
    )
    args = parser.parse_args()
    if args.command == "convert":
        # Groups, not years: APMCM stages beside CUMCM under the same root, and its
        # 2016 archive is the other place a Word statement needs converting.
        groups = (
            [item.strip() for item in args.groups.split(",") if item.strip()]
            if args.groups
            else [f"cumcm-{year}" for year in parse_years(args.years)]
        )
        cmd_convert(groups)
        return 0
    if args.groups:
        raise SystemExit("--groups applies to `convert` only; use --years for fetch/expand.")
    {"fetch": cmd_fetch, "expand": cmd_expand}[args.command](parse_years(args.years))
    return 0


if __name__ == "__main__":
    sys.exit(main())

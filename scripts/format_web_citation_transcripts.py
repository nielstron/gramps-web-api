#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["beautifulsoup4>=4.13", "requests>=2.32", "tabulate>=0.9"]
# ///
"""Audit and repair formatting of generated web-citation transcripts.

The command is read-only unless ``--apply`` is passed. Applying changes also
requires an explicit backup path. Authentication is read from the macOS
Keychain so API keys never appear in process arguments or output.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from tabulate import tabulate

NDB_TRANSCRIPTS = {
    "NTRANSCRIPTCMUENDLERKITTELNDB": "https://www.deutsche-biographie.de/gnd116194367.html",
    "NTRANSCRIPTCMUENDLERNDBKUEHNLE": "https://www.deutsche-biographie.de/gnd136587747.html",
}
KNOWN_HEADINGS = {
    "Genealogie",
    "Biographie",
    "Werke",
    "Nachlass",
    "Literatur",
    "Autor/in",
    "Zitierweise",
    "Personen:",
    "Familien:",
    "Biografischer Text:",
}
URL_RE = re.compile(r"https?://[^\s<>]+")
LABEL_RE = re.compile(r"^([\wÄÖÜäöüß /-]{2,35}:)(?=\s|$)")


def _add_range(
    ranges: dict[tuple[str, str | None], list[list[int]]],
    name: str,
    start: int,
    end: int,
    value: str | None = None,
) -> None:
    if end > start:
        ranges[(name, value)].append([start, end])


def _tags(ranges: dict[tuple[str, str | None], list[list[int]]]) -> list[dict]:
    result = []
    for (name, value), raw_ranges in ranges.items():
        merged: list[list[int]] = []
        for start, end in sorted(raw_ranges):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        result.append({"name": name, "value": value, "ranges": merged})
    return result


def style_structured_plain_text(text: str) -> dict:
    """Add conservative semantics without changing an existing transcript."""
    ranges: dict[tuple[str, str | None], list[list[int]]] = defaultdict(list)
    offset = 0
    for index, line in enumerate(text.splitlines(keepends=True)):
        content = line.rstrip("\r\n")
        stripped = content.strip()
        leading = len(content) - len(content.lstrip())
        start = offset + leading
        if stripped and (index == 0 or stripped in KNOWN_HEADINGS):
            _add_range(ranges, "bold", start, start + len(stripped))
        else:
            label = LABEL_RE.match(stripped)
            if label:
                _add_range(ranges, "bold", start, start + len(label.group(1)))
        offset += len(line)
    for match in URL_RE.finditer(text):
        value = match.group(0).rstrip(".,;)")
        _add_range(ranges, "link", match.start(), match.start() + len(value), value)
    return {"_class": "StyledText", "string": text, "tags": _tags(ranges)}


def html_to_styled_text(html: str, base_url: str) -> dict:
    """Convert the NDB article body to the subset supported by StyledText."""
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one("#ndbcontent ul.bioartikel")
    if node is None:
        raise ValueError(f"NDB article body missing at {base_url}")
    for unwanted in node.select("script, style, nav, header, footer, noscript, button"):
        unwanted.decompose()
    # Historical NDB markup wraps a nested title and lead paragraph in an
    # outer <h1>. Browsers repair that invalid nesting, but html.parser keeps
    # it and would incorrectly bold the whole lead paragraph.
    for heading in list(node.find_all("h1")):
        if heading.find(["h1", "p"], recursive=False):
            heading.unwrap()

    text = ""
    ranges: dict[tuple[str, str | None], list[list[int]]] = defaultdict(list)

    def append(value: str, formats: list[tuple[str, str | None]]) -> None:
        nonlocal text
        start = len(text)
        text += value
        for name, tag_value in formats:
            _add_range(ranges, name, start, len(text), tag_value)

    def double_break() -> None:
        nonlocal text
        if text and not text.endswith("\n\n"):
            text += "\n" if text.endswith("\n") else "\n\n"

    def formats_for(element: Tag) -> list[tuple[str, str | None]]:
        name = element.name.lower()
        classes = set(element.get("class", []))
        formats: list[tuple[str, str | None]] = []
        if name in {"b", "strong", "h1", "h2", "h3", "h4", "h5", "h6"}:
            formats.append(("bold", None))
        if name in {"i", "em"} or "italics" in classes:
            formats.append(("italic", None))
        if name == "u":
            formats.append(("underline", None))
        if name in {"s", "del", "strike"}:
            formats.append(("strikethrough", None))
        if name == "sup":
            formats.append(("superscript", None))
        if name == "a" and element.get("href"):
            href = urljoin(base_url, element["href"])
            if href.startswith(("http://", "https://", "mailto:")):
                formats.append(("link", href))
        return formats

    def walk(current, active: list[tuple[str, str | None]], pre: bool = False) -> None:
        if isinstance(current, NavigableString):
            value = str(current)
            if not pre:
                value = re.sub(r"\s+", " ", value)
                if value.startswith(" ") and (not text or text.endswith((" ", "\n"))):
                    value = value[1:]
            if value:
                append(value, active)
            return
        if not isinstance(current, Tag):
            return
        name = current.name.lower()
        if name in {"script", "style", "noscript", "img"}:
            return
        if name == "br":
            append("\n", active)
            return
        block = name in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote"}
        if block:
            double_break()
        new_active = [*active, *formats_for(current)]
        for child in current.children:
            walk(child, new_active, pre or name == "pre")
        if block:
            double_break()

    walk(node, [])
    text = text.strip("\n ")
    limit = len(text)
    clean_ranges: dict[tuple[str, str | None], list[list[int]]] = defaultdict(list)
    for key, spans in ranges.items():
        for start, end in spans:
            _add_range(clean_ranges, key[0], max(0, start), min(limit, end), key[1])
    return {"_class": "StyledText", "string": text, "tags": _tags(clean_ranges)}


def keychain_token(service: str, account: str) -> str:
    return subprocess.check_output(
        ["security", "find-generic-password", "-w", "-a", account, "-s", service],
        text=True,
    ).strip()


def clean_for_put(note: dict) -> dict:
    return {
        key: value
        for key, value in note.items()
        if key not in {"change", "profile", "extended", "backlinks", "formatted"}
    }


def canonical_api_value(value):
    """Ignore class markers that Gramps accepts but omits when serializing."""
    if isinstance(value, dict):
        return {
            key: canonical_api_value(item)
            for key, item in value.items()
            if key != "_class"
        }
    if isinstance(value, list):
        return [canonical_api_value(item) for item in value]
    return value


def desired_note(note: dict, session: requests.Session) -> tuple[dict, str]:
    updated = clean_for_put(note)
    note_id = note["gramps_id"]
    if note_id in NDB_TRANSCRIPTS:
        url = NDB_TRANSCRIPTS[note_id]
        response = session.get(url, timeout=30)
        response.raise_for_status()
        updated["text"] = html_to_styled_text(response.text, url)
        updated["format"] = 0
        return updated, "NDB HTML → StyledText"
    text = note["text"]["string"]
    updated["text"] = style_structured_plain_text(text)
    if note_id.startswith("NTRANSCRIPTCGEDBAS"):
        updated["format"] = 1
        return updated, "preformatted GEDCOM"
    updated["format"] = note.get("format", 0)
    return updated, "semantic headings/labels"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--base-url", default="https://misc.niels.bond/stammbaum/api")
    parser.add_argument("--keychain-service", default="misc.niels.bond/stammbaum/api")
    parser.add_argument("--keychain-account", default="niels")
    args = parser.parse_args()
    if args.apply and args.backup is None:
        parser.error("--apply requires --backup")

    session = requests.Session()
    session.headers["Authorization"] = (
        f"Bearer {keychain_token(args.keychain_service, args.keychain_account)}"
    )
    response = session.get(
        f"{args.base_url}/notes/?pagesize=300&profile=all", timeout=30
    )
    response.raise_for_status()
    targets = [
        note
        for note in response.json()
        if note.get("type") == "Transcript"
        and note.get("gramps_id", "").startswith("NTRANSCRIPT")
    ]
    changes = []
    desired = {}
    for note in targets:
        updated, method = desired_note(note, session)
        before = clean_for_put(note)
        if canonical_api_value(updated) != canonical_api_value(before):
            changes.append(
                [
                    note["gramps_id"],
                    method,
                    note.get("format", 0),
                    updated.get("format", 0),
                    len(note.get("text", {}).get("tags", [])),
                    len(updated["text"].get("tags", [])),
                ]
            )
            desired[note["handle"]] = updated

    print(
        tabulate(
            changes,
            headers=[
                "Note",
                "Method",
                "Old format",
                "New format",
                "Old tags",
                "New tags",
            ],
            tablefmt="github",
        )
    )
    print(f"\nGenerated transcripts audited: {len(targets)}; changes: {len(changes)}")
    if not args.apply:
        print("Dry run only; pass --apply and --backup PATH to write changes.")
        return

    args.backup.parent.mkdir(parents=True, exist_ok=True)
    args.backup.write_text(
        json.dumps(targets, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for handle, updated in desired.items():
        put = session.put(f"{args.base_url}/notes/{handle}", json=updated, timeout=30)
        put.raise_for_status()
        verify = session.get(f"{args.base_url}/notes/{handle}", timeout=30)
        verify.raise_for_status()
        actual = clean_for_put(verify.json())
        if canonical_api_value(actual) != canonical_api_value(updated):
            raise RuntimeError(f"verification failed for {updated['gramps_id']}")
    print(f"Updated and verified {len(desired)} notes; backup: {args.backup}")


if __name__ == "__main__":
    main()

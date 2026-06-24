#!/usr/bin/env python3
"""
Deterministic localization consistency checks for Runesmith2.

This script does not translate or rewrite text. It only compares a target
localization against a source localization and reports mechanical issues that
can cause missing text or broken SmartFormat/rich-text formatting in-game.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


LOCALIZATION_ROOT = Path("Runesmith2/localization")
TAG_RE = re.compile(r"\[(/?)([A-Za-z]+)(?:=[^\]]*)?\]")
KNOWN_FORMATTERS = {
    "diff()",
    "energyIcons()",
    "plural",
    "rs2Plural",
    "show",
}


@dataclass(frozen=True)
class Issue:
    path: Path
    key: str | None
    message: str

    def format(self) -> str:
        if self.key:
            return f"ERROR {self.path} {self.key}\n  {self.message}"
        return f"ERROR {self.path}\n  {self.message}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate that one Runesmith2 localization mirrors another without "
            "breaking JSON, keys, SmartFormat placeholders, or rich-text tags."
        )
    )
    parser.add_argument("--base", default="eng", help="source locale directory, default: eng")
    parser.add_argument("--target", default="fra", help="target locale directory, default: fra")
    parser.add_argument(
        "--root",
        default=Path.cwd(),
        type=Path,
        help="repository root, default: current working directory",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    base_dir = root / LOCALIZATION_ROOT / args.base
    target_dir = root / LOCALIZATION_ROOT / args.target

    issues: list[Issue] = []
    if not base_dir.is_dir():
        issues.append(Issue(base_dir.relative_to(root), None, "base localization directory does not exist"))
    if not target_dir.is_dir():
        issues.append(Issue(target_dir.relative_to(root), None, "target localization directory does not exist"))
    if issues:
        print_issues(issues)
        return 1

    base_files = {path.name: path for path in sorted(base_dir.glob("*.json"))}
    target_files = {path.name: path for path in sorted(target_dir.glob("*.json"))}

    for name in sorted(base_files.keys() - target_files.keys()):
        issues.append(Issue((target_dir / name).relative_to(root), None, "missing target JSON file"))
    for name in sorted(target_files.keys() - base_files.keys()):
        issues.append(Issue(target_files[name].relative_to(root), None, "extra target JSON file"))

    checked_files = 0
    checked_keys = 0
    for name in sorted(base_files.keys() & target_files.keys()):
        base_path = base_files[name]
        target_path = target_files[name]
        base_data = load_table(base_path, root, issues)
        target_data = load_table(target_path, root, issues)
        if base_data is None or target_data is None:
            continue

        rel_target = target_path.relative_to(root)
        for key in sorted(base_data.keys() - target_data.keys()):
            issues.append(Issue(rel_target, key, "missing target key"))
        for key in sorted(target_data.keys() - base_data.keys()):
            issues.append(Issue(rel_target, key, "extra target key"))

        for key in sorted(base_data.keys() & target_data.keys()):
            base_value = base_data[key]
            target_value = target_data[key]
            checked_keys += 1
            if not isinstance(target_value, str):
                issues.append(Issue(rel_target, key, f"target value must be a string, got {type(target_value).__name__}"))
                continue
            if not isinstance(base_value, str):
                # Source tables should be flat string maps. Report this as a source issue
                # because target comparison is undefined when the base value is not text.
                issues.append(Issue(base_path.relative_to(root), key, f"base value must be a string, got {type(base_value).__name__}"))
                continue

            compare_placeholders(rel_target, key, base_value, target_value, issues)
            compare_tags(rel_target, key, base_value, target_value, issues)

        checked_files += 1

    if issues:
        print_issues(issues)
        return 1

    print(f"OK {args.target}: {checked_files} files, {checked_keys} keys checked against {args.base}.")
    return 0


def load_table(path: Path, root: Path, issues: list[Issue]) -> dict[str, object] | None:
    rel_path = path.relative_to(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        issues.append(Issue(rel_path, None, f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"))
        return None
    except OSError as exc:
        issues.append(Issue(rel_path, None, f"could not read file: {exc}"))
        return None

    if not isinstance(data, dict):
        issues.append(Issue(rel_path, None, f"top-level JSON value must be an object, got {type(data).__name__}"))
        return None
    return data


def compare_placeholders(path: Path, key: str, base_value: str, target_value: str, issues: list[Issue]) -> None:
    base_errors: list[str] = []
    target_errors: list[str] = []
    base_tokens = extract_placeholder_signatures(base_value, base_errors)
    target_tokens = extract_placeholder_signatures(target_value, target_errors)

    for error in base_errors:
        issues.append(Issue(path, key, f"base SmartFormat parse issue: {error}"))
    for error in target_errors:
        issues.append(Issue(path, key, f"target SmartFormat parse issue: {error}"))
    if base_errors or target_errors:
        return

    missing = base_tokens - target_tokens
    extra = target_tokens - base_tokens
    if missing:
        issues.append(Issue(path, key, "missing SmartFormat token(s): " + format_counter(missing)))
    if extra:
        issues.append(Issue(path, key, "extra SmartFormat token(s): " + format_counter(extra)))


def extract_placeholder_signatures(text: str, errors: list[str]) -> Counter[str]:
    signatures: Counter[str] = Counter()
    for expression in iter_placeholder_expressions(text, errors):
        signature = signature_for_expression(expression)
        if signature:
            signatures[signature] += 1
        signatures.update(extract_placeholder_signatures(expression, errors))
    return signatures


def iter_placeholder_expressions(text: str, errors: list[str]) -> list[str]:
    expressions: list[str] = []
    stack: list[int] = []

    for index, char in enumerate(text):
        if char == "{":
            stack.append(index)
        elif char == "}":
            if not stack:
                errors.append(f"unmatched closing brace at offset {index}")
                continue
            start = stack.pop()
            if not stack:
                expressions.append(text[start + 1 : index])

    for start in stack:
        errors.append(f"unmatched opening brace at offset {start}")
    return expressions


def signature_for_expression(expression: str) -> str:
    if expression == "":
        return ""

    parts = split_top_level_colons(expression)
    selector = parts[0].strip()
    if len(parts) < 2:
        return selector

    candidate = parts[1].strip()
    if is_formatter(candidate):
        return f"{selector}:{canonical_formatter(candidate)}"
    return selector


def split_top_level_colons(expression: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(expression):
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        elif char == ":" and depth == 0:
            parts.append(expression[start:index])
            start = index + 1
    parts.append(expression[start:])
    return parts


def is_formatter(candidate: str) -> bool:
    if candidate in KNOWN_FORMATTERS:
        return True
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\([^{}:]*\)", candidate))


def canonical_formatter(formatter: str) -> str:
    # SmartFormat allows culture/options on plural formatters, e.g. plural(fr).
    # The option is locale-specific and should not be forced to match English.
    if re.fullmatch(r"(plural|rs2Plural)\([^{}:]*\)", formatter):
        return formatter.split("(", 1)[0]
    return formatter


def compare_tags(path: Path, key: str, base_value: str, target_value: str, issues: list[Issue]) -> None:
    base_tags = extract_tags(base_value)
    target_tags = extract_tags(target_value)
    missing = base_tags - target_tags
    if missing:
        issues.append(Issue(path, key, "missing rich-text tag(s): " + format_counter(missing)))

    target_balance = tag_balance(target_value)
    unbalanced = {tag: count for tag, count in target_balance.items() if count != 0}
    if unbalanced:
        details = ", ".join(f"{tag}={count:+d}" for tag, count in sorted(unbalanced.items()))
        issues.append(Issue(path, key, "unbalanced target rich-text tag(s): " + details))


def extract_tags(text: str) -> Counter[str]:
    return Counter(f"{'/' if slash else ''}{name.lower()}" for slash, name in TAG_RE.findall(text))


def tag_balance(text: str) -> Counter[str]:
    balance: Counter[str] = Counter()
    for slash, name in TAG_RE.findall(text):
        lowered = name.lower()
        if lowered == "br":
            continue
        if slash:
            balance[lowered] -= 1
        else:
            balance[lowered] += 1
    return balance


def format_counter(counter: Counter[str]) -> str:
    return ", ".join(f"{token} x{count}" if count > 1 else token for token, count in sorted(counter.items()))


def print_issues(issues: list[Issue]) -> None:
    for issue in issues:
        print(issue.format(), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

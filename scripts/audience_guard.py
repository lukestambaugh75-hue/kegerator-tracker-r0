#!/usr/bin/env python3
"""Enforce the standalone Kegerator audience and link boundary."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

try:
    from .refresh_state import utc_iso, validate_refresh_status
except ImportError:
    from refresh_state import utc_iso, validate_refresh_status


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_DASHBOARD_URL = "https://lukestambaugh75-hue.github.io/kegerator-tracker-r0/"
CANONICAL_INDEX_PATH = Path("index.html")
CANONICAL_INDEX_SHA256 = "ae13c7ee7bc9326f019388d5b7034741d35f1f05010f02721ad8ab6f0fede78f"
EXPECTED_RECIPIENTS = ["lukestambaugh75@gmail.com", "devin.mullen89@gmail.com"]
ALLOWED_RETAIL_HOSTS = {
    "kegco.com",
    "www.kegco.com",
    "edgestar.com",
    "www.edgestar.com",
    "homedepot.com",
    "www.homedepot.com",
}
ALLOWED_LOCAL_RUNTIME = {
    "assets/kegerator-hero.png",
    "data/listings.json",
    "data/refresh-status.json",
    "data/specs.json",
    "history.csv",
}
ALLOWED_FETCH_TARGETS = {
    "data/listings.json",
    "data/refresh-status.json",
    "data/specs.json",
    "history.csv",
}

CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.IGNORECASE | re.DOTALL)
CSS_IMPORT_RE = re.compile(
    r"@import\s+(?:url\(\s*)?(['\"])(.*?)\1\s*\)?", re.IGNORECASE | re.DOTALL
)
CSS_ESCAPE_RE = re.compile(
    r"\\(?:([0-9a-fA-F]{1,6})(?:\r\n|[ \t\r\n\f])?|([^\r\n\f0-9a-fA-F]))"
)
ABSOLUTE_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
SCRIPT_ACTIVE_ATTR_RE = re.compile(
    r"\b(href|src|srcset|action|formaction|poster|background|data|xlink:href|ping|srcdoc)"
    r"\s*=\s*(['\"])(.*?)\2",
    re.IGNORECASE | re.DOTALL,
)
SCRIPT_BLOCKED_TAG_RE = re.compile(
    r"<\s*(base|embed|form|iframe|link|meta|object|script|style|svg)\b",
    re.IGNORECASE,
)
INTERACTIVE_TAGS = {"a", "area", "button", "input", "menu", "nav", "option", "summary"}
INTERACTIVE_ROLES = {"button", "link", "menu", "menuitem", "navigation", "tab"}
SVG_RESOURCE_TAGS = {"feimage", "image", "use"}
VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
FORBIDDEN_NAV_TOKENS = {
    "alldealtrackers",
    "alltrackers",
    "babygear",
    "dailydashboard",
    "dailydashboards",
    "dealtrackers",
    "fordraptor",
    "maindashboard",
    "playstation",
    "ps5",
    "raptor",
    "stroller",
    "tokencost",
    "washer",
    "weatherdashboard",
}
UNSAFE_SCRIPT_IDENTIFIERS = {
    "Audio",
    "BroadcastChannel",
    "DOMParser",
    "EventSource",
    "Function",
    "Image",
    "SharedWorker",
    "WebSocket",
    "Worker",
    "XMLHttpRequest",
    "atob",
    "btoa",
    "eval",
    "globalThis",
    "history",
    "importScripts",
    "location",
    "navigator",
    "open",
    "parent",
    "self",
    "sendBeacon",
    "top",
    "window",
}
UNSAFE_URL_MEMBER_IDENTIFIERS = {"action", "formAction", "href", "src", "srcdoc"}
UNSAFE_DOCUMENT_MEMBERS = {
    "URL",
    "cookie",
    "createElement",
    "domain",
    "location",
    "referrer",
    "write",
    "writeln",
}
REGEX_PREFIX_KEYWORDS = {
    "await",
    "case",
    "delete",
    "do",
    "else",
    "in",
    "instanceof",
    "new",
    "of",
    "return",
    "throw",
    "typeof",
    "void",
    "yield",
}
REGEX_PREFIX_PUNCTUATION = {
    "!",
    "%",
    "&",
    "(",
    "*",
    "+",
    ",",
    "-",
    ":",
    ";",
    "<",
    "=",
    ">",
    "?",
    "[",
    "^",
    "{",
    "}",
    "|",
    "~",
}


class AudienceBoundaryError(ValueError):
    """Raised when content crosses the Kegerator audience boundary."""


def listing_source_urls(listings: list[dict]) -> frozenset[str]:
    """Return and validate the exact current retailer source URLs."""
    if not isinstance(listings, list):
        raise AudienceBoundaryError("data/listings.json must contain a JSON array")
    urls: set[str] = set()
    for index, row in enumerate(listings):
        if not isinstance(row, dict):
            raise AudienceBoundaryError(f"listing row {index} must be an object")
        value = str(row.get("source_url") or "").strip()
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or (parsed.hostname or "").lower() not in ALLOWED_RETAIL_HOSTS
        ):
            raise AudienceBoundaryError(
                f"listing row {index} has a source outside the approved retailer hosts: {value}"
            )
        urls.add(value)
    if not urls:
        raise AudienceBoundaryError("at least one current listing source URL is required")
    return frozenset(urls)


def _decode_css_escapes(css_text: str) -> str:
    """Decode CSS escapes before looking for browser-active URL functions."""
    css_text = re.sub(r"\\(?:\r\n|\r|\n|\f)", "", css_text or "")

    def replace(match: re.Match) -> str:
        hex_value, character = match.groups()
        if character is not None:
            return character
        codepoint = int(hex_value, 16)
        if codepoint == 0 or codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
            return "\N{REPLACEMENT CHARACTER}"
        return chr(codepoint)

    return CSS_ESCAPE_RE.sub(replace, css_text)


def _css_references(css_text: str, context: str) -> list[tuple[str, str, str]]:
    css_text = _decode_css_escapes(css_text)
    references = []
    for match in CSS_URL_RE.finditer(css_text):
        references.append(("resource", html.unescape(match.group(2).strip()), context))
    for match in CSS_IMPORT_RE.finditer(css_text):
        references.append(("resource", html.unescape(match.group(2).strip()), context))
    return references


class _BoundaryParser(HTMLParser):
    """Collect every browser-active reference and interactive label."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[tuple[str, str, str]] = []
        self.scripts: list[dict] = []
        self._current_script: dict | None = None
        self._script_depth = 0
        self._style_depth = 0
        self._interactive_stack: list[dict] = []
        self._labels: list[str] = []
        self._id_stack: list[dict] = []
        self._id_text: dict[str, str] = {}
        self._seen_ids: set[str] = set()
        self._aria_labelledby_groups: list[tuple[str, ...]] = []

    def _handle_tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        names = [str(name).lower() for name, _ in attrs]
        if len(names) != len(set(names)):
            raise AudienceBoundaryError(f"duplicate attributes are not allowed on <{tag}>")
        attrs_by_name = {str(name).lower(): value for name, value in attrs}
        if tag == "base":
            raise AudienceBoundaryError("base URL elements are not allowed")
        if tag == "form":
            raise AudienceBoundaryError("forms are not allowed on the Kegerator dashboard")
        if tag == "style":
            self._style_depth += 1
        if tag == "script":
            if self._current_script is not None:
                raise AudienceBoundaryError("nested script elements are not allowed")
            self._script_depth += 1
            self._current_script = {
                "attrs": {
                    str(name).lower(): html.unescape(str(value or "").strip())
                    for name, value in attrs
                },
                "parts": [],
            }
            self.scripts.append(self._current_script)

        role = str(attrs_by_name.get("role") or "").strip().lower()
        is_interactive = tag in INTERACTIVE_TAGS or role in INTERACTIVE_ROLES
        if is_interactive:
            labelled_by = str(attrs_by_name.get("aria-labelledby") or "").strip()
            if labelled_by:
                self._aria_labelledby_groups.append(tuple(labelled_by.split()))
        for name, value in attrs:
            name = str(name).lower()
            value = html.unescape(str(value or "").strip())
            if name.startswith("on"):
                raise AudienceBoundaryError(
                    f"inline event handlers are not allowed: <{tag}> {name}"
                )
            if name in {"srcdoc", "ping"}:
                raise AudienceBoundaryError(
                    f"uninspected active attributes are not allowed: <{tag}> {name}"
                )
            if name in {"action", "formaction"}:
                raise AudienceBoundaryError(
                    f"form submission attributes are not allowed: <{tag}> {name}"
                )
            if not value:
                continue
            context = f"<{tag}> {name}"
            if name in {"alt", "aria-label", "title", "value"}:
                if is_interactive:
                    self._labels.append(value)
                for element in self._interactive_stack:
                    element["parts"].append(value)
                for element in self._id_stack:
                    element["parts"].append(value)
            if name == "style":
                self.references.extend(_css_references(value, context))
            elif name in {"imagesrcset", "srcset"}:
                for candidate in value.split(","):
                    resource = candidate.strip().split()[0] if candidate.strip() else ""
                    if resource:
                        self.references.append(("resource", resource, context))
            elif name in {"src", "poster", "background", "data", "xlink:href"}:
                self.references.append(("resource", value, context))
            elif name == "href":
                kind = "resource" if tag == "link" or tag in SVG_RESOURCE_TAGS else "navigation"
                self.references.append((kind, value, context))
            elif name == "cite":
                self.references.append(("navigation", value, context))

        if tag == "meta" and str(attrs_by_name.get("http-equiv") or "").strip().lower() == "refresh":
            raise AudienceBoundaryError("meta refresh redirects are not allowed")
        return is_interactive

    def _start_id_capture(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_by_name = {str(name).lower(): value for name, value in attrs}
        element_id = str(attrs_by_name.get("id") or "").strip()
        if not element_id:
            return
        if element_id in self._seen_ids:
            raise AudienceBoundaryError(f"duplicate HTML id is not allowed: {element_id}")
        self._seen_ids.add(element_id)
        fallback = " ".join(
            str(attrs_by_name.get(name) or "").strip()
            for name in ("aria-label", "alt", "title", "value")
            if str(attrs_by_name.get(name) or "").strip()
        )
        if tag in VOID_TAGS:
            self._id_text[element_id] = fallback
        else:
            parts = [fallback] if fallback else []
            self._id_stack.append({"tag": tag, "id": element_id, "parts": parts})

    def handle_starttag(self, tag, attrs) -> None:
        tag = tag.lower()
        is_interactive = self._handle_tag(tag, attrs)
        self._start_id_capture(tag, attrs)
        if is_interactive and tag not in VOID_TAGS:
            self._interactive_stack.append({"tag": tag, "parts": []})

    def handle_startendtag(self, tag, attrs) -> None:
        tag = tag.lower()
        self._handle_tag(tag, attrs)
        self._start_id_capture(tag, attrs)
        if self._id_stack and self._id_stack[-1]["tag"] == tag:
            element = self._id_stack.pop()
            self._id_text[element["id"]] = " ".join(element["parts"]).strip()
        if tag == "style":
            self._style_depth -= 1
        if tag == "script":
            self._script_depth -= 1
            self._current_script = None

    def handle_endtag(self, tag) -> None:
        tag = tag.lower()
        if tag == "style" and self._style_depth:
            self._style_depth -= 1
        if tag == "script" and self._script_depth:
            self._script_depth -= 1
            self._current_script = None
        for index in range(len(self._id_stack) - 1, -1, -1):
            if self._id_stack[index]["tag"] == tag:
                for element in self._id_stack[index:]:
                    self._id_text[element["id"]] = " ".join(element["parts"]).strip()
                del self._id_stack[index:]
                break
        for index in range(len(self._interactive_stack) - 1, -1, -1):
            if self._interactive_stack[index]["tag"] == tag:
                for element in self._interactive_stack[index:]:
                    label = " ".join(element["parts"]).strip()
                    if label:
                        self._labels.append(label)
                del self._interactive_stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._style_depth:
            self.references.extend(_css_references(data, "<style>"))
            return
        if self._script_depth:
            if self._current_script is not None:
                self._current_script["parts"].append(data)
            return
        text = data.strip()
        if text:
            for element in self._id_stack:
                element["parts"].append(text)
            for element in self._interactive_stack:
                element["parts"].append(text)

    def labels(self) -> list[str]:
        labels = list(self._labels)
        labels.extend(
            " ".join(element["parts"]).strip()
            for element in self._interactive_stack
            if element["parts"]
        )
        open_ids = {
            element["id"]: " ".join(element["parts"]).strip()
            for element in self._id_stack
        }
        for references in self._aria_labelledby_groups:
            parts = []
            for reference in references:
                if reference in self._id_text:
                    parts.append(self._id_text[reference])
                elif reference in open_ids:
                    parts.append(open_ids[reference])
                else:
                    raise AudienceBoundaryError(
                        f"aria-labelledby references a missing id: {reference}"
                    )
            labels.append(" ".join(parts).strip())
        return labels


def _parse_html(html_text: str) -> _BoundaryParser:
    parser = _BoundaryParser()
    try:
        parser.feed(html_text)
        parser.close()
    except AudienceBoundaryError:
        raise
    except Exception as exc:
        raise AudienceBoundaryError(f"could not parse dashboard HTML: {exc}") from exc
    if parser._style_depth or parser._script_depth:
        raise AudienceBoundaryError("unclosed style or script element")
    return parser


def _validate_navigation(value: str, context: str, allowed_urls: set[str] | frozenset[str]) -> None:
    value = html.unescape(str(value or "").strip())
    if not value or value.startswith("#"):
        return
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or value not in allowed_urls:
        raise AudienceBoundaryError(
            f"navigation must be an exact current Kegerator/source URL in {context}: {value}"
        )


def _validate_local_resource(value: str, context: str, asset_root: Path) -> None:
    value = html.unescape(str(value or "").strip())
    if not value or value.startswith("#"):
        return
    parsed = urlsplit(value)
    path_text = parsed.path.replace("\\", "/")
    if (
        parsed.scheme
        or parsed.netloc
        or value.startswith("//")
        or parsed.query
        or parsed.fragment
        or path_text.startswith("/")
        or path_text not in ALLOWED_LOCAL_RUNTIME
    ):
        raise AudienceBoundaryError(
            f"resource must be an exact local Kegerator runtime file in {context}: {value}"
        )
    root = Path(asset_root).resolve()
    candidate = (root / path_text).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise AudienceBoundaryError(f"resource escapes the Kegerator repository: {value}") from exc
    if not candidate.is_file():
        raise AudienceBoundaryError(f"local runtime resource does not exist: {value}")


def _compact_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _validate_visible_navigation(parser: _BoundaryParser) -> None:
    for label in parser.labels():
        compact = _compact_label(label)
        for token in FORBIDDEN_NAV_TOKENS:
            if token in compact:
                raise AudienceBoundaryError(
                    f"cross-dashboard navigation label is not allowed: {label.strip()}"
                )


def _javascript_tokens(source: str) -> list[tuple[str, str, bool]]:
    """Tokenize executable JavaScript while ignoring comments and template text."""
    tokens: list[tuple[str, str, bool]] = []
    length = len(source)

    def regex_can_start() -> bool:
        if not tokens:
            return True
        kind, value, _ = tokens[-1]
        if kind == "identifier":
            return value in REGEX_PREFIX_KEYWORDS
        if kind == "punctuation":
            return value in REGEX_PREFIX_PUNCTUATION
        return False

    def scan(index: int, stop_on_closing_brace: bool = False) -> int:
        brace_depth = 0
        while index < length:
            char = source[index]
            if stop_on_closing_brace and char == "}" and brace_depth == 0:
                return index + 1
            if char.isspace():
                index += 1
                continue
            if source.startswith("//", index):
                newline = source.find("\n", index + 2)
                index = length if newline == -1 else newline + 1
                continue
            if source.startswith("/*", index):
                closing = source.find("*/", index + 2)
                if closing == -1:
                    raise AudienceBoundaryError("unterminated JavaScript block comment")
                index = closing + 2
                continue
            if char in {"'", '"'}:
                quote = char
                index += 1
                value: list[str] = []
                escaped = False
                while index < length:
                    current = source[index]
                    if current == "\\":
                        escaped = True
                        value.append(current)
                        index += 1
                        if index < length:
                            value.append(source[index])
                            index += 1
                        continue
                    if current == quote:
                        index += 1
                        break
                    value.append(current)
                    index += 1
                else:
                    raise AudienceBoundaryError("unterminated JavaScript string")
                tokens.append(("string", "".join(value), escaped))
                continue
            if char == "`":
                index += 1
                while index < length:
                    current = source[index]
                    if current == "\\":
                        index += 2
                        continue
                    if current == "`":
                        index += 1
                        break
                    if source.startswith("${", index):
                        tokens.append(("template", "", False))
                        index = scan(index + 2, stop_on_closing_brace=True)
                        continue
                    index += 1
                else:
                    raise AudienceBoundaryError("unterminated JavaScript template")
                continue
            if char == "/" and regex_can_start():
                index += 1
                in_class = False
                while index < length:
                    current = source[index]
                    if current == "\\":
                        index += 2
                        continue
                    if current in {"\n", "\r"}:
                        raise AudienceBoundaryError("unterminated JavaScript regex literal")
                    if current == "[":
                        in_class = True
                    elif current == "]":
                        in_class = False
                    elif current == "/" and not in_class:
                        index += 1
                        while index < length and source[index].isalpha():
                            index += 1
                        tokens.append(("regex", "", False))
                        break
                    index += 1
                else:
                    raise AudienceBoundaryError("unterminated JavaScript regex literal")
                continue
            if char.isalpha() or char in {"_", "$"}:
                end = index + 1
                while end < length and (source[end].isalnum() or source[end] in {"_", "$"}):
                    end += 1
                tokens.append(("identifier", source[index:end], False))
                index = end
                continue
            if char.isdigit():
                end = index + 1
                while end < length and (source[end].isalnum() or source[end] in {".", "_"}):
                    end += 1
                tokens.append(("number", source[index:end], False))
                index = end
                continue
            if char == "{":
                brace_depth += 1
            elif char == "}" and brace_depth:
                brace_depth -= 1
            tokens.append(("punctuation", char, False))
            index += 1
        if stop_on_closing_brace:
            raise AudienceBoundaryError("unterminated JavaScript template interpolation")
        return index

    scan(0)
    return tokens


def _call_arguments(
    tokens: list[tuple[str, str, bool]], opening_index: int
) -> tuple[list[list[tuple[str, str, bool]]], int]:
    arguments: list[list[tuple[str, str, bool]]] = []
    current: list[tuple[str, str, bool]] = []
    depth = 0
    pairs = {"(": ")", "[": "]", "{": "}"}
    closing = set(pairs.values())
    for index in range(opening_index, len(tokens)):
        value = tokens[index][1]
        if value in pairs:
            depth += 1
            if depth > 1:
                current.append(tokens[index])
            continue
        if value in closing:
            depth -= 1
            if depth == 0:
                if current or not arguments:
                    arguments.append(current)
                return arguments, index
            current.append(tokens[index])
            continue
        if value == "," and depth == 1:
            arguments.append(current)
            current = []
            continue
        current.append(tokens[index])
    raise AudienceBoundaryError("unterminated JavaScript function call")


def _validate_script_source(
    source: str,
    allowed_urls: set[str] | frozenset[str],
    script_name: str,
) -> None:
    for value in _absolute_urls(source):
        _validate_navigation(value, f"{script_name} literal", allowed_urls)

    blocked_tag = SCRIPT_BLOCKED_TAG_RE.search(source)
    if blocked_tag:
        raise AudienceBoundaryError(
            f"script-generated <{blocked_tag.group(1).lower()}> content is not allowed in {script_name}"
        )

    for match in SCRIPT_ACTIVE_ATTR_RE.finditer(source):
        name = match.group(1).lower()
        value = html.unescape(match.group(3).strip())
        if name == "href" and value == "${escapeHtml(row.source_url)}":
            continue
        raise AudienceBoundaryError(
            f"script-generated active attribute is not pinned to row.source_url: {name}={value}"
        )

    tokens = _javascript_tokens(source)
    values = [token[1] for token in tokens]
    for index, token in enumerate(tokens):
        kind, value, escaped = token
        next_value = values[index + 1] if index + 1 < len(values) else None
        previous = values[index - 1] if index else None
        if (
            kind == "string"
            and not escaped
            and value in UNSAFE_URL_MEMBER_IDENTIFIERS
            and previous == "["
            and next_value == "]"
        ):
            raise AudienceBoundaryError(
                f"computed URL-bearing member is not allowed in {script_name}: {value}"
            )
        if kind != "identifier":
            continue
        if value == "import":
            raise AudienceBoundaryError(f"JavaScript imports are not allowed in {script_name}")
        if value in UNSAFE_URL_MEMBER_IDENTIFIERS:
            raise AudienceBoundaryError(
                f"URL-bearing DOM member is not allowed in {script_name}: {value}"
            )
        is_data_member = previous == "." or next_value == ":"
        if value in UNSAFE_SCRIPT_IDENTIFIERS and not (
            value == "history" and is_data_member
        ):
            raise AudienceBoundaryError(
                f"network or cross-page navigation identifier is not allowed in {script_name}: {value}"
            )
        if value == "document":
            if next_value == "[":
                raise AudienceBoundaryError(
                    f"computed document member access is not allowed in {script_name}"
                )
            member = values[index + 2] if values[index + 1 : index + 2] == ["."] and index + 2 < len(values) else None
            if member in UNSAFE_DOCUMENT_MEMBERS:
                raise AudienceBoundaryError(
                    f"unsafe document member is not allowed in {script_name}: {member}"
                )
        if value in {"outerHTML", "insertAdjacentHTML", "setAttribute"}:
            raise AudienceBoundaryError(
                f"uninspected dynamic HTML API is not allowed in {script_name}: {value}"
            )
        if value == "fetch":
            if previous == "." or next_value != "(":
                raise AudienceBoundaryError(
                    f"fetch must be called directly with one pinned local path in {script_name}"
                )
            arguments, _ = _call_arguments(tokens, index + 1)
            if len(arguments) != 1 or len(arguments[0]) != 1:
                raise AudienceBoundaryError(
                    f"fetch must use exactly one literal local path in {script_name}"
                )
            argument = arguments[0][0]
            if argument[0] != "string" or argument[2] or argument[1] not in ALLOWED_FETCH_TARGETS:
                raise AudienceBoundaryError(
                    f"fetch target is outside the Kegerator runtime allowlist in {script_name}"
                )


def _absolute_urls(value: object) -> set[str]:
    return {
        html.unescape(match).rstrip(".,;")
        for match in ABSOLUTE_URL_RE.findall(str(value or ""))
    }


def validate_html_semantics(
    html_text: str,
    *,
    allowed_listing_urls: set[str] | frozenset[str],
    asset_root: Path = ROOT,
) -> None:
    """Apply parser and script defense-in-depth checks to supplied HTML."""
    allowed_urls = frozenset({CANONICAL_DASHBOARD_URL, *allowed_listing_urls})
    parser = _parse_html(html_text)
    for kind, value, context in parser.references:
        if kind == "resource":
            _validate_local_resource(value, context, asset_root)
        else:
            _validate_navigation(value, context, allowed_urls)
    _validate_visible_navigation(parser)
    for index, script in enumerate(parser.scripts, start=1):
        attrs = script["attrs"]
        content = "".join(script["parts"])
        if "src" in attrs:
            if content.strip():
                raise AudienceBoundaryError("external scripts cannot also contain inline code")
            continue
        if set(attrs) - {"type"}:
            raise AudienceBoundaryError("inline scripts may only use an optional type attribute")
        script_type = attrs.get("type", "").lower()
        if script_type in {"application/json", "application/ld+json"}:
            try:
                json.loads(content)
            except json.JSONDecodeError as exc:
                raise AudienceBoundaryError(f"inline JSON script is invalid: {exc}") from exc
            continue
        if script_type not in {"", "application/javascript", "module", "text/javascript"}:
            raise AudienceBoundaryError(f"unrecognized inline script type: {script_type}")
        _validate_script_source(content, allowed_urls, f"inline script {index}")


def _validate_canonical_index_path(source_path: Path | str, asset_root: Path) -> None:
    if isinstance(source_path, Path):
        expected = (Path(asset_root).resolve() / CANONICAL_INDEX_PATH).resolve()
        if source_path.resolve() != expected:
            raise AudienceBoundaryError(
                f"canonical index path must be {expected}; got {source_path.resolve()}"
            )
        return
    if str(source_path) != CANONICAL_DASHBOARD_URL:
        raise AudienceBoundaryError(
            f"canonical index path must be {CANONICAL_DASHBOARD_URL}; got {source_path}"
        )


def validate_html(
    html_content: bytes,
    *,
    allowed_listing_urls: set[str] | frozenset[str],
    asset_root: Path = ROOT,
    source_path: Path | str,
) -> None:
    """Pin canonical index bytes/path, then apply semantic defense in depth."""
    _validate_canonical_index_path(source_path, asset_root)
    if not isinstance(html_content, bytes):
        raise AudienceBoundaryError("canonical index must be validated from exact bytes")
    raw = html_content
    digest = hashlib.sha256(raw).hexdigest()
    if digest != CANONICAL_INDEX_SHA256:
        raise AudienceBoundaryError(
            "canonical index digest mismatch: "
            f"expected {CANONICAL_INDEX_SHA256}, got {digest}"
        )
    try:
        html_text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AudienceBoundaryError(f"canonical index is not valid UTF-8: {exc}") from exc
    validate_html_semantics(
        html_text,
        allowed_listing_urls=allowed_listing_urls,
        asset_root=asset_root,
    )


def validate_email_payload(
    payload: dict,
    allowed_listing_urls: set[str] | frozenset[str] = frozenset(),
) -> None:
    """Validate exact recipients and all URL-bearing email fields."""
    if payload.get("to") != EXPECTED_RECIPIENTS:
        raise AudienceBoundaryError(
            f"email recipients must be exactly {EXPECTED_RECIPIENTS}; got {payload.get('to')}"
        )
    if payload.get("cc") != [] or payload.get("bcc") != []:
        raise AudienceBoundaryError("email CC and BCC must remain empty")
    if payload.get("dashboard_url") != CANONICAL_DASHBOARD_URL:
        raise AudienceBoundaryError(
            f"email dashboard_url must be exactly {CANONICAL_DASHBOARD_URL}"
        )
    allowed_urls = frozenset({CANONICAL_DASHBOARD_URL, *allowed_listing_urls})
    body_html = str(payload.get("body_html") or "")
    parser = _parse_html(body_html)
    if parser.scripts:
        raise AudienceBoundaryError("email scripts are not allowed")
    for kind, value, context in parser.references:
        if kind == "resource":
            raise AudienceBoundaryError(f"email resource loading is not allowed in {context}: {value}")
        _validate_navigation(value, f"email {context}", allowed_urls)
    _validate_visible_navigation(parser)
    for field in ("dashboard_url", "body_text", "body_html"):
        for value in _absolute_urls(payload.get(field)):
            _validate_navigation(value, f"email {field}", allowed_urls)


def validate_automation_mirror(text: str) -> None:
    """Validate the inactive Codex automation mirror without changing installed state."""
    addresses = {value.casefold() for value in EMAIL_RE.findall(text)}
    if addresses != {value.casefold() for value in EXPECTED_RECIPIENTS}:
        raise AudienceBoundaryError(
            f"automation recipients must be exactly Luke + Devin; found {sorted(addresses)}"
        )
    if "scripts/audience_guard.py" not in text:
        raise AudienceBoundaryError("automation mirror must run scripts/audience_guard.py")
    if "READY_TO_REGISTER" not in text:
        raise AudienceBoundaryError("automation mirror must remain inactive (READY_TO_REGISTER)")
    if "no CC/BCC" not in text and "Do not add CC or BCC" not in text:
        raise AudienceBoundaryError("automation mirror must explicitly keep CC/BCC empty")
    for value in _absolute_urls(text):
        if value != CANONICAL_DASHBOARD_URL:
            raise AudienceBoundaryError(f"automation mirror contains a non-Kegerator URL: {value}")


def validate_repository(root: Path = ROOT) -> frozenset[str]:
    """Validate local dashboard, data, automation mirror, and generated email."""
    root = Path(root).resolve()
    listings = json.loads((root / "data" / "listings.json").read_text(encoding="utf-8"))
    refresh = validate_refresh_status(
        json.loads((root / "data" / "refresh-status.json").read_text(encoding="utf-8"))
    )
    success_at = refresh["data_refreshed_at_utc"]
    if refresh["source_count"] != len(listings) or refresh["row_count"] != len(listings):
        raise AudienceBoundaryError("refresh counts must represent every checked-in listing")
    if refresh["quality_counts"] != {
        "verified": len(listings),
        "estimated": 0,
        "blocked": 0,
    }:
        raise AudienceBoundaryError("refresh quality counts must represent one verified snapshot")
    if any(
        row.get("data_quality") != "confirmed" or utc_iso(row.get("retrieved")) != success_at
        for row in listings
    ):
        raise AudienceBoundaryError("listings must represent the exact last successful data refresh")
    allowed_sources = listing_source_urls(listings)
    index_path = root / CANONICAL_INDEX_PATH
    validate_html(
        index_path.read_bytes(),
        allowed_listing_urls=allowed_sources,
        asset_root=root,
        source_path=index_path,
    )
    validate_automation_mirror(
        (root / "automation" / "kegerator-tracker-email.toml").read_text(encoding="utf-8")
    )
    email_path = root / "out" / "latest-email.json"
    if email_path.is_file():
        validate_email_payload(
            json.loads(email_path.read_text(encoding="utf-8")),
            allowed_sources,
        )
    return allowed_sources


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=os.fspath(ROOT))
    args = parser.parse_args()
    try:
        sources = validate_repository(Path(args.root))
    except Exception as exc:
        print(f"audience boundary violation: {exc}", file=sys.stderr)
        return 1
    print(
        "audience boundary passed: standalone Kegerator page, "
        f"{len(sources)} current listing sources, exact Luke + Devin email"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

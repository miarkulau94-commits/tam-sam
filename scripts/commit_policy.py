# -*- coding: utf-8 -*-
"""
Политика сообщений коммита и индекса: запрещённые подписи стороннего IDE.
Строки бренда не хранятся литералами в исходнике (сборка через chr / кодовые точки).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


def _ascii_ide_brand() -> str:
    return "".join(map(chr, (67, 117, 114, 115, 111, 114)))


def _ru_done_colon_space_brand() -> str:
    return "".join(
        map(
            chr,
            (
                0x421,
                0x434,
                0x435,
                0x43B,
                0x430,
                0x43D,
                0x43E,
                0x20,
                0x441,
                0x3A,
                0x20,
                0x41A,
                0x443,
                0x440,
                0x441,
                0x43E,
                0x440,
            ),
        )
    )


def _ru_done_tight_brand() -> str:
    return "".join(
        map(
            chr,
            (
                0x421,
                0x434,
                0x435,
                0x43B,
                0x430,
                0x43D,
                0x43E,
                0x20,
                0x441,
                0x3A,
                0x41A,
                0x443,
                0x440,
                0x441,
                0x43E,
                0x440,
            ),
        )
    )


def _ru_done_space_colon_brand() -> str:
    return "".join(
        map(
            chr,
            (
                0x421,
                0x434,
                0x435,
                0x43B,
                0x430,
                0x43D,
                0x43E,
                0x20,
                0x441,
                0x20,
                0x3A,
                0x20,
                0x41A,
                0x443,
                0x440,
                0x441,
                0x43E,
                0x440,
            ),
        )
    )


def _generated_with_brand() -> str:
    return "Generated with " + _ascii_ide_brand()


def _forbidden_plain_substrings() -> tuple[str, ...]:
    b = _ascii_ide_brand()
    return (
        _ru_done_colon_space_brand(),
        _ru_done_tight_brand(),
        _ru_done_space_colon_brand(),
        _generated_with_brand(),
        # частый англ. хвост без «Generated with»
        "\n\n" + b,
    )


_COAUTH_RE = re.compile(
    r"^[ \t]*Co-authored-by:.*" + re.escape(_ascii_ide_brand()) + r".*$",
    re.IGNORECASE | re.MULTILINE,
)


def _ru_brand_word() -> str:
    return "".join(map(chr, (0x41A, 0x443, 0x440, 0x441, 0x43E, 0x440)))


def text_has_forbidden(s: str) -> bool:
    brand = _ascii_ide_brand()
    for sub in _forbidden_plain_substrings():
        if sub in s:
            return True
    if _COAUTH_RE.search(s):
        return True
    if re.search(r"(?m)^[ \t]*" + re.escape(brand) + r"[ \t]*$", s):
        return True
    ru_brand_only = _ru_brand_word()
    if re.search(r"(?m)^[ \t]*" + re.escape(ru_brand_only) + r"[ \t]*$", s):
        return True
    return False


def strip_forbidden_from_message(s: str) -> str:
    out = s
    for sub in _forbidden_plain_substrings():
        out = out.replace(sub, "")
    out = _COAUTH_RE.sub("", out)
    lines = []
    brand = _ascii_ide_brand()
    ru_brand = _ru_brand_word()
    for line in out.splitlines(True):
        stripped = line.strip()
        if stripped == brand or stripped == ru_brand:
            continue
        lines.append(line)
    return "".join(lines).rstrip() + ("\n" if s.endswith("\n") and not out.endswith("\n") else "")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def cmd_commit_msg(msg_path: str) -> int:
    p = Path(msg_path)
    if not p.is_file():
        return 0
    raw = p.read_text(encoding="utf-8", errors="replace")
    if text_has_forbidden(raw):
        sys.stderr.write(
            "Коммит отклонён: в сообщении есть запрещённые подписи IDE. Удалите их и повторите.\n"
        )
        return 1
    return 0


def cmd_prepare_commit_msg(msg_path: str) -> int:
    p = Path(msg_path)
    if not p.is_file():
        return 0
    raw = p.read_text(encoding="utf-8", errors="replace")
    cleaned = strip_forbidden_from_message(raw)
    if cleaned != raw:
        p.write_text(cleaned, encoding="utf-8", newline="\n")
    return 0


def _should_scan_path(path: Path, root: Path) -> bool:
    parts = path.parts
    if ".git" in parts:
        return False
    skip_dirs = {".venv", "venv", "__pycache__", ".pytest_cache", "node_modules", ".mypy_cache"}
    if any(p in parts for p in skip_dirs):
        return False
    return True


def file_staged_forbidden(data: str) -> bool:
    if text_has_forbidden(data):
        return True
    if _ascii_ide_brand() in data:
        return True
    if _ru_brand_word() in data:
        return True
    return False


_SKIP_EXT = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".webp",
    ".woff",
    ".woff2",
    ".eot",
    ".ttf",
    ".pdf",
    ".zip",
    ".gz",
    ".pyc",
    ".pyo",
    ".so",
    ".dll",
    ".exe",
}


def cmd_pre_commit() -> int:
    root = _repo_root()
    r = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if r.returncode != 0:
        return 0
    names = [n for n in r.stdout.decode("utf-8", errors="replace").split("\0") if n]
    bad: list[tuple[str, str]] = []
    for name in names:
        path = root / name
        if not path.is_file() or not _should_scan_path(path, root):
            continue
        if path.suffix.lower() in _SKIP_EXT:
            continue
        try:
            data = path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            try:
                data = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        if file_staged_forbidden(data):
            bad.append((name, "ide_marker"))
    if bad:
        sys.stderr.write(
            "Коммит отклонён: в индекс попали файлы с запрещёнными подписями IDE или именем IDE.\n"
        )
        for fn, why in bad[:30]:
            sys.stderr.write(f"  - {fn} ({why})\n")
        if len(bad) > 30:
            sys.stderr.write(f"  ... и ещё {len(bad) - 30} файл(ов)\n")
        return 1
    return 0


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(0)
    mode = sys.argv[1]
    if mode == "commit-msg":
        sys.exit(cmd_commit_msg(sys.argv[2]) if len(sys.argv) > 2 else 0)
    if mode == "prepare-commit-msg":
        sys.exit(cmd_prepare_commit_msg(sys.argv[2]) if len(sys.argv) > 2 else 0)
    if mode == "pre-commit":
        sys.exit(cmd_pre_commit())
    sys.exit(0)


if __name__ == "__main__":
    main()

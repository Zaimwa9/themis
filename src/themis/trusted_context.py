"""Trusted native context for review agents (issue #9, MVP).

When a repo opts in (`agent.context` / `agent.skills`), the agent gets
instruction files and skill packages through the engine's native discovery
instead of prompt concatenation — but resolved from the PR *base* revision,
never the PR head. The workspace becomes intentionally synthetic:
application code from the head, agent inputs from the trusted base. A PR can
therefore change instructions or skills without those changes steering its
own review; the diff still shows them.

Everything here fails closed per capability: any doubt (unresolvable
reference that the head would satisfy, oversized content, escaping paths)
disables the capability for the run and leaves its namespace empty, which is
exactly the pre-opt-in behavior.
"""

import asyncio
import logging
import os
import posixpath
import re
import shutil
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Instruction files engines discover natively (codex: AGENTS.md; the claude
# harness: CLAUDE.md, plus AGENTS.md via its fallback). CLAUDE.local.md is
# never materialized but always masked: it is user-plane, not repo-plane.
INSTRUCTION_BASENAMES = ("CLAUDE.md", "AGENTS.md")
_MASK_BASENAMES = frozenset(INSTRUCTION_BASENAMES + ("CLAUDE.local.md",))
SKILLS_PREFIX = ".claude/skills/"
# Skills bridge (issue #49): engines without native skill discovery get a
# synthesized index of the base-revision skills at this path. It lives in
# the generated-inputs directory, never in a natively-discovered namespace.
SKILLS_INDEX_PATH = ".review-input/skills-index.md"

MAX_FILE_BYTES = 1_048_576  # 1 MiB per file
MAX_TOTAL_BYTES = 10_485_760  # 10 MiB per capability
MAX_FILES = 200  # per capability
MAX_REF_DEPTH = 5  # matches the claude harness's own import depth limit
MAX_INDEX_ENTRIES = 50
MAX_INDEX_DESCRIPTION = 200  # chars per index entry description

# Claude Code imports are `@` + a non-whitespace path run, evaluated only
# outside code fences and code spans. Matching the provider grammar exactly
# matters in both directions: an untracked form leaves a PR-head file
# loadable; an over-matched one overwrites PR files with base content.
_IMPORT_PATTERN = re.compile(r"(?:^|\s)@(\S+)")
_FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CODE_SPAN_PATTERNS = (
    re.compile(r"``.+?``", re.DOTALL),
    re.compile(r"`[^`\n]*`"),
)


class TrustedContextError(Exception):
    pass


class _UnsupportedImport(Exception):
    """An import form we cannot resolve from the base tree (absolute,
    home-relative, or escaping the workspace). Fails the capability closed."""

    def __init__(self, ref: str) -> None:
        super().__init__(ref)
        self.ref = ref


async def _git_bytes(workspace: Path, *args: str, timeout: float = 60) -> bytes:
    """Local git plumbing (no network, no token); raw bytes out."""
    process = await asyncio.create_subprocess_exec(
        "git", *args, cwd=workspace,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError as error:
        process.kill()
        raise TrustedContextError(f"git {args[0]} timed out") from error
    if process.returncode != 0:
        raise TrustedContextError(
            f"git {args[0]} failed: {stderr.decode(errors='replace')[-300:]}"
        )
    return stdout


async def _base_entries(workspace: Path, base: str) -> dict[str, tuple[str, str, int]]:
    """path -> (mode, blob sha, size) for the whole base tree."""
    raw = await _git_bytes(workspace, "ls-tree", "-r", "-l", "-z", base)
    entries: dict[str, tuple[str, str, int]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        mode, _, sha, size = meta.split()
        entries[path.decode(errors="replace")] = (
            mode.decode(), sha.decode(), int(size) if size != b"-" else 0,
        )
    return entries


def _mask_instruction_files(workspace: Path) -> None:
    """Remove every natively-discoverable instruction file from the working
    tree. os.walk without followlinks: never delete through a symlink."""
    for dirpath, dirnames, filenames in os.walk(workspace):
        if ".git" in dirnames:
            dirnames.remove(".git")
        for name in filenames:
            if name in _MASK_BASENAMES:
                (Path(dirpath) / name).unlink(missing_ok=True)


def _remove_node(path: Path) -> None:
    """Remove whatever sits at path: file, symlink, or directory. A PR can
    validly commit a directory under any of these names; removal by the
    wrong type must not crash the review."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _scrub_executable_config(workspace: Path) -> None:
    """Native discovery must never re-enable executable surfaces: settings,
    hooks, plugins, agents, commands, MCP config. Only `.claude/skills` may
    exist afterwards, and only because it is rebuilt from the base tree."""
    _remove_node(workspace / ".mcp.json")
    _remove_node(workspace / ".claude")
    # A head-committed skills index must never survive: the review prompt
    # may point the agent at this path, so only Themis writes it.
    _remove_node(workspace / SKILLS_INDEX_PATH)


def _safe_dest(workspace: Path, rel_path: str) -> Path | None:
    """The write target, or None if reaching it would escape the workspace
    (absolute path, `..`, or a symlinked ancestor the PR head planted)."""
    parts = Path(rel_path).parts
    if not parts or rel_path.startswith("/") or ".." in parts:
        return None
    current = workspace
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            return None
    return workspace / rel_path


def _importable_text(text: str) -> str:
    """Drop code fences and code spans: Claude does not evaluate imports
    there, so neither may the planner. Fence handling mirrors GFM (closing
    fence matches the opener's character at >= length; unclosed runs to the
    end), because a mismatch in either direction is a real defect: treating
    prose as code hides an import a PR-head file could satisfy, treating
    code as prose overwrites PR files the reviewer should be assessing."""
    kept: list[str] = []
    open_fence: tuple[str, int] | None = None
    for line in text.splitlines():
        match = _FENCE_LINE.match(line)
        if match:
            run = match.group(1)
            if open_fence is None:
                open_fence = (run[0], len(run))
                continue
            if run[0] == open_fence[0] and len(run) >= open_fence[1]:
                open_fence = None
                continue
        if open_fence is None:
            kept.append(line)
    prose = "\n".join(kept)
    for pattern in _CODE_SPAN_PATTERNS:
        prose = pattern.sub(" ", prose)
    return prose


def _refs_in(text: str, referrer: str) -> list[list[str]]:
    """Candidate repo-relative paths per @-import, in resolution priority
    (relative to the referring file first, then repo root). Raises
    _UnsupportedImport for forms that cannot be resolved from the base tree
    (absolute, home-relative, workspace-escaping)."""
    candidates = []
    referrer_dir = posixpath.dirname(referrer)
    for ref in _IMPORT_PATTERN.findall(_importable_text(text)):
        if "://" in ref:
            continue  # a URL, not a file import
        if ref.startswith(("/", "~")):
            raise _UnsupportedImport(ref)
        ordered = []
        for candidate in (posixpath.join(referrer_dir, ref), ref):
            normal = posixpath.normpath(candidate)
            if normal.startswith(("../", "/")) or normal == "..":
                continue
            if normal not in ordered:
                ordered.append(normal)
        if not ordered:
            # Every resolution escapes the workspace: a deliberate outside
            # import we cannot pin to the base tree.
            raise _UnsupportedImport(ref)
        candidates.append(ordered)
    return candidates


async def _plan(
    workspace: Path,
    entries: dict[str, tuple[str, str, int]],
    seeds: list[str],
    *,
    capability: str,
    skip_instruction_refs: bool,
    skip_skills_refs: bool,
) -> list[tuple[Path, bytes | None]] | None:
    """Resolve seeds plus their @-references from the base tree into
    (destination, content) writes. None = fail closed (reason logged).
    All reads and checks happen here; nothing touches the working tree."""
    queue: list[tuple[str, int]] = [(path, 0) for path in seeds]
    planned: set[str] = set(seeds)
    writes: list[tuple[Path, bytes | None]] = []
    total = 0

    def disabled(reason: str, path: str) -> None:
        logger.warning(
            "themis_trusted_context_disabled capability=%s reason=%s path=%s",
            capability, reason, path,
        )

    while queue:
        path, depth = queue.pop(0)
        mode, sha, size = entries[path]
        if mode not in ("100644", "100755"):
            # Symlinks and submodules are never materialized — but the head
            # node at that path must not survive either, or a PR could swap
            # a base symlink for a real file and have the import load it.
            dest = _safe_dest(workspace, path)
            if dest is None:
                disabled("unsafe_path", path)
                return None
            writes.append((dest, None))  # mask only
            logger.info(
                "themis_trusted_context_skipped mode=%s path=%s", mode, path
            )
            continue
        if size > MAX_FILE_BYTES:
            disabled("file_too_large", path)
            return None
        total += size
        if total > MAX_TOTAL_BYTES or len(writes) >= MAX_FILES:
            disabled("budget_exceeded", path)
            return None
        dest = _safe_dest(workspace, path)
        if dest is None:
            disabled("unsafe_path", path)
            return None
        content = await _git_bytes(workspace, "cat-file", "blob", sha)
        writes.append((dest, content))
        if depth >= MAX_REF_DEPTH or not path.endswith(".md"):
            continue
        try:
            ref_lists = _refs_in(content.decode(errors="ignore"), path)
        except _UnsupportedImport as error:
            disabled("unsupported_import", error.ref)
            return None
        for candidates in ref_lists:
            for candidate in candidates:
                if skip_instruction_refs and (
                    posixpath.basename(candidate) in _MASK_BASENAMES
                ):
                    break  # masked namespace: the import cannot load anything
                if skip_skills_refs and candidate.startswith(SKILLS_PREFIX):
                    break  # empty namespace: same
                if candidate in entries:
                    if candidate not in planned:
                        planned.add(candidate)
                        queue.append((candidate, depth + 1))
                    break
                head_copy = workspace / candidate
                if head_copy.is_symlink() or head_copy.is_file():
                    # The trusted file imports a path only the PR provides:
                    # native discovery would load head content. Fail closed.
                    # (A directory cannot be imported; it does not count.)
                    disabled("head_only_reference", candidate)
                    return None
                # Missing everywhere: the import loads nothing. Harmless.
    return writes


def _write(writes: list[tuple[Path, bytes | None]]) -> int:
    for dest, content in writes:
        if content is None:
            # Mask-only entry: a base symlink/submodule at this path is
            # never materialized, and no head node may remain there.
            _remove_node(dest)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        # The head may have anything at this path — including a directory
        # where the base had a file; replace by type, never assume.
        _remove_node(dest)
        dest.write_bytes(content)
    return sum(len(content) for _, content in writes if content is not None)


async def apply_trusted_context(
    workspace: Path, base_ref: str, *, context: bool, skills: bool,
    skills_index: bool = False,
) -> tuple[bool, bool]:
    """Prepare the synthetic workspace; returns the effective capabilities.

    Masking runs on EVERY job, opted in or not: codex discovers AGENTS.md
    natively and its CLI has no flag against that (--ignore-rules only
    covers execpolicy .rules files), so removing PR-head instruction files
    and executable config from the working tree is the isolation mechanism.
    Masking always precedes materialization, so every failure path leaves
    the discoverable namespaces empty rather than head-controlled."""
    try:
        _mask_instruction_files(workspace)
        _scrub_executable_config(workspace)
    except OSError as error:
        # A cleanup error must not abort the job with no PR-facing result;
        # fail every capability closed and let the review proceed.
        logger.warning(
            "themis_trusted_context_disabled capability=all reason=mask_failed"
            " error=%s", str(error)[:200],
        )
        return False, False
    if not (context or skills):
        return False, False
    try:
        entries = await _base_entries(workspace, f"refs/remotes/origin/{base_ref}")
    except TrustedContextError as error:
        logger.warning(
            "themis_trusted_context_disabled capability=all reason=base_unreadable"
            " error=%s", str(error)[:200],
        )
        return False, False

    if context:
        seeds = [
            path for path in entries
            if posixpath.basename(path) in INSTRUCTION_BASENAMES
            and not path.startswith(SKILLS_PREFIX)
        ]
        context = await _apply_capability(
            workspace, entries, seeds, capability="context",
            skip_instruction_refs=False, skip_skills_refs=not skills,
        ) is not None
    if skills:
        seeds = [path for path in entries if path.startswith(SKILLS_PREFIX)]
        writes = await _apply_capability(
            workspace, entries, seeds, capability="skills",
            skip_instruction_refs=not context, skip_skills_refs=False,
        )
        skills = writes is not None
        if skills and skills_index:
            _write_skills_index(workspace, writes or [])
    return context, skills


async def _apply_capability(
    workspace: Path,
    entries: dict[str, tuple[str, str, int]],
    seeds: list[str],
    *,
    capability: str,
    skip_instruction_refs: bool,
    skip_skills_refs: bool,
) -> list[tuple[Path, bytes | None]] | None:
    """The applied writes on success, None when the capability fails closed."""
    try:
        writes = await _plan(
            workspace, entries, seeds, capability=capability,
            skip_instruction_refs=skip_instruction_refs,
            skip_skills_refs=skip_skills_refs,
        )
    except TrustedContextError as error:
        logger.warning(
            "themis_trusted_context_disabled capability=%s reason=git_error"
            " error=%s", capability, str(error)[:200],
        )
        return None
    if writes is None:
        return None
    try:
        written = _write(writes)
    except OSError as error:
        # e.g. a head file where the base had a directory ancestor. The
        # namespaces were masked up front, so failing closed here leaves
        # them empty (partially-written files are base content — trusted).
        logger.warning(
            "themis_trusted_context_disabled capability=%s reason=write_failed"
            " error=%s", capability, str(error)[:200],
        )
        return None
    logger.info(
        "themis_trusted_context_applied capability=%s files=%d bytes=%d",
        capability, len(writes), written,
    )
    return writes


def _skill_frontmatter(text: str) -> dict | None:
    """The yaml frontmatter mapping of a SKILL.md, or None when absent or
    malformed. Same shape the claude harness reads; parsed leniently because
    a broken skill must only cost its own index entry."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for end, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            try:
                data = yaml.safe_load("\n".join(lines[1:end]))
            except yaml.YAMLError:
                return None
            return data if isinstance(data, dict) else None
    return None


def _write_skills_index(
    workspace: Path, writes: list[tuple[Path, bytes | None]]
) -> None:
    """Synthesize the skills index for engines without native skill
    discovery (issue #49), from the base blobs the skills capability just
    materialized. Failure leaves no index; the capability itself stands."""
    entries: list[str] = []
    skipped = 0
    for dest, content in sorted(writes, key=lambda item: item[0].as_posix()):
        relative = dest.relative_to(workspace).as_posix()
        parts = relative.split("/")
        if content is None or len(parts) != 4 or parts[-1] != "SKILL.md":
            continue  # not a `.claude/skills/<name>/SKILL.md` entry
        frontmatter = _skill_frontmatter(content.decode(errors="replace"))
        description = (frontmatter or {}).get("description")
        if not isinstance(description, str) or not description.strip():
            skipped += 1
            logger.warning(
                "themis_skills_index_skipped path=%s reason=no_description",
                relative,
            )
            continue
        name = frontmatter.get("name")
        if not isinstance(name, str) or not name.strip():
            name = parts[2]
        description = " ".join(description.split())
        if len(description) > MAX_INDEX_DESCRIPTION:
            # The ellipsis counts against the cap: the rendered description
            # never exceeds MAX_INDEX_DESCRIPTION characters.
            description = description[: MAX_INDEX_DESCRIPTION - 1].rstrip() + "…"
        entries.append(f"- **{name.strip()}** — {description} (`{relative}`)")
    if len(entries) > MAX_INDEX_ENTRIES:
        logger.warning(
            "themis_skills_index_truncated total=%d kept=%d",
            len(entries), MAX_INDEX_ENTRIES,
        )
        entries = entries[:MAX_INDEX_ENTRIES]
    if not entries:
        logger.info("themis_skills_index_empty skipped=%d", skipped)
        return
    body = (
        "# Repository skills index\n\n"
        "Generated by Themis from the PR base revision. Each entry is a\n"
        "reviewer skill. When an entry's description matches the code under\n"
        "review, read its SKILL.md and follow it.\n\n"
        + "\n".join(entries) + "\n"
    )
    dest = workspace / SKILLS_INDEX_PATH
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _remove_node(dest)
        dest.write_text(body)
    except OSError as error:
        logger.warning(
            "themis_skills_index_failed error=%s", str(error)[:200]
        )
        return
    logger.info("themis_skills_index_written entries=%d", len(entries))

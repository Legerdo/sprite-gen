# SPDX-License-Identifier: Apache-2.0
"""Publish the GitHub release page for a tag, or rehearse it without touching anything.

The gate this closes: a `vX.Y.Z` tag can be pushed, changelogged and announced while the
release page is never created, leaving an older version as Latest. `.github/workflows/
release.yml` runs this on every `v*` tag push, and the same file runs it with `--dry-run`
on `workflow_dispatch` so the decision path can be rehearsed on a branch.

Three outcomes, and no fourth:

* the release already exists -> nothing is created, edited or uploaded, exit 0. Re-running
  a tag is a no-op, so a re-run of a failed job cannot rewrite a published page.
* it does not exist and this is a dry run -> the title, body and asset list are printed
  and exit 0. No tag, no release, no upload.
* it does not exist -> `gh release create` publishes it and `gh release view` confirms it.

`gh` decides nothing by absence: only its own "release not found" counts as missing. Any
other failure (no token, no network, a 5xx) stops the run instead of being read as "not
there yet" and answered with a create.

Standard library only, same as `release_notes.py`; `gh` and `python -m build` are the
only external commands.

    .venv/bin/python scripts/release_publish.py --tag v2.5.3 --dry-run
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import release_notes  # noqa: E402  (sibling script, resolved by the line above)

REPO_ROOT = Path(__file__).resolve().parents[1]
NOT_FOUND = "release not found"


class PublishError(RuntimeError):
    """The release cannot be decided or published. Never answered by retrying blind."""


def _run(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)


def release_exists(gh: str, tag: str, *, repo: str, cwd: Path) -> bool:
    """True / False, or raise. An unreadable answer is not a False."""
    proc = _run([gh, "release", "view", tag, "--repo", repo, "--json", "tagName"], cwd=cwd)
    if proc.returncode == 0:
        return True
    if NOT_FOUND in (proc.stderr + proc.stdout).lower():
        return False
    raise PublishError(
        f"could not tell whether {tag} is released (gh exit {proc.returncode}): "
        f"{(proc.stderr or proc.stdout).strip()}"
    )


def build_distribution(dist_dir: Path, *, cwd: Path) -> list[Path]:
    """wheel + sdist + SHA256SUMS over both, built from this checkout.

    The output directory has to be empty: everything in it is attached to the release, so
    a leftover archive from an earlier version would be uploaded as part of this one.
    """
    if dist_dir.exists() and any(dist_dir.iterdir()):
        raise PublishError(
            f"{dist_dir} is not empty — build into a clean directory so stale archives "
            f"cannot be attached to the release")
    dist_dir.mkdir(parents=True, exist_ok=True)
    proc = _run([sys.executable, "-m", "build", "--outdir", str(dist_dir)], cwd=cwd)
    if proc.returncode != 0:
        raise PublishError(f"python -m build failed:\n{(proc.stderr or proc.stdout).strip()}")
    return checksum_distribution(dist_dir)


def checksum_distribution(dist_dir: Path) -> list[Path]:
    """Write SHA256SUMS next to the archives and return everything to attach."""
    import hashlib

    archives = sorted(p for p in dist_dir.iterdir() if p.suffix in {".whl", ".gz"})
    if not archives:
        raise PublishError(f"no wheel or sdist in {dist_dir}")
    sums = dist_dir / "SHA256SUMS"
    sums.write_text(
        "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in archives),
        encoding="utf-8",
    )
    return [*archives, sums]


def _emit(text: str, summary_path: str | None) -> None:
    print(text)
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True, help="release tag, e.g. v2.5.3")
    parser.add_argument("--dry-run", action="store_true",
                        help="decide and print; create nothing and upload nothing")
    parser.add_argument("--repo", default=release_notes.DEFAULT_REPO)
    parser.add_argument("--branch", default=release_notes.DEFAULT_BRANCH,
                        help="branch the raw asset URLs in the body point at")
    parser.add_argument("--checkout", type=Path, default=REPO_ROOT,
                        help="the tree the notes and the assets are read from")
    parser.add_argument("--dist-dir", type=Path, default=None,
                        help="where the wheel and sdist go (default: <checkout>/dist)")
    parser.add_argument("--skip-build", action="store_true",
                        help="attach only the GIFs; for rehearsing the decision alone")
    parser.add_argument("--gh", default="gh", help="the gh executable to call")
    args = parser.parse_args(argv)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    checkout = args.checkout.resolve()
    dist_dir = (args.dist_dir or checkout / "dist").resolve()

    try:
        page = release_notes.render(
            (checkout / "CHANGELOG.md").read_text(encoding="utf-8"),
            args.tag,
            checkout,
            repo=args.repo,
            branch=args.branch,
        )
        if release_exists(args.gh, args.tag, repo=args.repo, cwd=checkout):
            _emit(f"{args.tag} already has a release page on {args.repo}; "
                  f"body and assets left untouched.", summary)
            return 0

        assets = [str(checkout / rel) for rel in page["assets"]]
        if args.skip_build:
            built: list[Path] = []
        else:
            built = build_distribution(dist_dir, cwd=checkout)
        assets = [str(p) for p in built] + assets

        if args.dry_run:
            listing = "\n".join(f"  {a}" for a in assets) or "  (none)"
            _emit(
                f"DRY RUN — {args.tag} has no release page on {args.repo}. Would create:\n"
                f"\ntitle: {page['title']}\n\nassets:\n{listing}\n\nbody:\n{'-' * 60}\n"
                f"{page['body']}\n{'-' * 60}\n\nNothing was created, uploaded or tagged.",
                summary,
            )
            return 0

        with tempfile.TemporaryDirectory() as scratch:
            notes = Path(scratch) / "release-notes.md"
            notes.write_text(str(page["body"]) + "\n", encoding="utf-8")
            proc = _run([args.gh, "release", "create", args.tag, "--repo", args.repo,
                         "--title", str(page["title"]), "--notes-file", str(notes), *assets],
                        cwd=checkout)
        if proc.returncode != 0:
            raise PublishError(f"gh release create failed:\n{(proc.stderr or proc.stdout).strip()}")
        if not release_exists(args.gh, args.tag, repo=args.repo, cwd=checkout):
            raise PublishError(f"gh release create reported success but {args.tag} has no page")
        _emit(f"published {args.repo} {args.tag}: {proc.stdout.strip()}", summary)
        return 0
    except (release_notes.ReleaseNotesError, PublishError, OSError) as exc:
        print(f"release_publish: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

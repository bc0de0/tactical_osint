"""
Iron Compass report PDF export hook.

Two responsibilities, both scoped to pages under docs/reports/posts/:

1. on_page_markdown -- injects a "Download PDF" button under the page's
   H1, linking to the PDF this hook will generate at build time.

2. on_post_build -- generates one standalone PDF per report by running
   a separate, scoped `mkdocs build` subprocess for each post (using
   the with-pdf plugin) and copying the resulting single-report PDF
   into the already-built site's reports/pdfs/ directory.

Why a subprocess instead of calling mkdocs-with-pdf's API directly:
mkdocs-with-pdf is built to render everything in a site's nav into one
combined PDF. Iron Compass wants one PDF per report. The cleanest way
to get a single-report PDF from a plugin that doesn't natively support
that is to give it a nav containing exactly one page. Doing that
without disturbing the real build's config, theme, or already-produced
site_dir means running it as a separate process against a temporary
MkDocs config that INHERITs the real mkdocs.yml (theme, markdown
extensions, etc.) and overrides only nav, site_dir, plugins, and hooks.

Safety:
- A PDF export failure is logged as a warning and never fails the
  overall build -- the HTML site must always deploy even if a
  specific report's PDF render breaks.
- IRON_COMPASS_PDF_SUBBUILD guards against recursion: the nested build
  runs with this env var set to "1", and this hook's own on_post_build
  no-ops whenever it sees that var set, so a sub-build's own
  post-build phase can never spawn a further nested build. The child
  config also sets `hooks: []` so this hook does not even load inside
  the sub-build -- the env var is a second, independent guard, not the
  only one.
"""

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

RECURSION_GUARD_ENV = "IRON_COMPASS_PDF_SUBBUILD"
REPORTS_POST_PREFIX = "reports/posts/"
PDF_OUTPUT_SUBDIR = "reports/pdfs"
SUB_BUILD_TIMEOUT_SECONDS = 240

log = logging.getLogger("mkdocs.iron_compass_pdf")


def _front_matter(post_path: Path) -> dict:
    text = post_path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        return {}
    return yaml.safe_load(match.group(1)) or {}


def _slug_for_post(post_path: Path) -> str:
    meta = _front_matter(post_path)
    return meta.get("slug") or post_path.stem


def _title_for_post(post_path: Path) -> str | None:
    meta = _front_matter(post_path)
    if meta.get("title"):
        return meta["title"]
    text = post_path.read_text(encoding="utf-8")
    heading = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return heading.group(1).strip() if heading else None


def _meta_for_page(page) -> dict:
    return page.meta or {}


def on_page_markdown(markdown, page, config, files, **kwargs):
    if not page.file.src_uri.startswith(REPORTS_POST_PREFIX):
        return markdown

    meta = _meta_for_page(page)
    slug = meta.get("slug") or Path(page.file.src_uri).stem
    button = (
        f"[:material-file-pdf-box: Download PDF](/{PDF_OUTPUT_SUBDIR}/{slug}.pdf)"
        "{ .md-button .md-button--primary }\n"
    )

    lines = markdown.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("# "):
            return "\n".join(lines[: i + 1]) + "\n\n" + button + "\n" + "\n".join(lines[i + 1 :])
    return button + "\n" + markdown


def on_post_build(config, **kwargs):
    if os.environ.get(RECURSION_GUARD_ENV) == "1":
        return  # this build IS a PDF sub-build; never recurse further

    docs_dir = Path(config["docs_dir"])
    posts_dir = docs_dir / "reports" / "posts"
    if not posts_dir.exists():
        return

    site_dir = Path(config["site_dir"])
    pdf_out_dir = site_dir / PDF_OUTPUT_SUBDIR
    pdf_out_dir.mkdir(parents=True, exist_ok=True)

    for post_path in sorted(posts_dir.glob("*.md")):
        try:
            _build_single_pdf(post_path, config, pdf_out_dir)
            log.info("Iron Compass PDF export: built %s.pdf", _slug_for_post(post_path))
        except Exception as exc:  # noqa: BLE001 -- a failed PDF must never fail the site build
            log.warning(
                "Iron Compass PDF export failed for %s: %s -- HTML page still deployed "
                "without a working Download PDF link for this report.",
                post_path.name,
                exc,
            )


def _build_single_pdf(post_path: Path, config, pdf_out_dir: Path) -> None:
    slug = _slug_for_post(post_path)
    title = _title_for_post(post_path)
    docs_dir = Path(config["docs_dir"])
    rel_post = post_path.relative_to(docs_dir).as_posix()
    main_config_path = Path(config["config_file_path"]).resolve()

    repo_root = main_config_path.parent

    with tempfile.TemporaryDirectory(prefix="ic_pdf_") as tmp_str:
        tmp = Path(tmp_str)
        tmp_site_dir = tmp / "site"
        tmp_pdf_path = tmp / "out" / f"{slug}.pdf"
        tmp_pdf_path.parent.mkdir(parents=True, exist_ok=True)

        with_pdf_options = {
            "enabled_if_env": RECURSION_GUARD_ENV,
            "output_path": str(tmp_pdf_path),
            "custom_template_path": str(repo_root / "pdf_templates"),
            "cover": True,
        }
        if title:
            with_pdf_options["cover_title"] = title

        child_config = {
            "INHERIT": str(main_config_path),
            "site_dir": str(tmp_site_dir),
            "nav": [{"Report": rel_post}],
            "hooks": [],
            "plugins": [{"with-pdf": with_pdf_options}],
        }

        # IMPORTANT: MkDocs resolves every relative path in a config
        # (custom_dir, extra_css, docs_dir if given relatively, etc.)
        # against the *config file's own directory*, not the process's
        # cwd and not the INHERIT parent's directory. The child config
        # must therefore live in the repo root next to mkdocs.yml, not
        # in a system temp dir, or every inherited relative path (e.g.
        # the Material theme's `custom_dir: overrides`) resolves to a
        # nonexistent path and the sub-build fails on config load.
        child_config_path = repo_root / f".ic_pdf_build_{slug}.tmp.yml"
        try:
            child_config_path.write_text(
                yaml.safe_dump(child_config, sort_keys=False), encoding="utf-8"
            )

            env = dict(os.environ)
            env[RECURSION_GUARD_ENV] = "1"

            result = subprocess.run(
                [sys.executable, "-m", "mkdocs", "build", "-f", str(child_config_path)],
                cwd=str(repo_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=SUB_BUILD_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"PDF sub-build exited {result.returncode}\n"
                    f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
                )
            if not tmp_pdf_path.exists():
                raise RuntimeError(f"sub-build succeeded but expected PDF not found at {tmp_pdf_path}")

            shutil.copy2(tmp_pdf_path, pdf_out_dir / f"{slug}.pdf")
        finally:
            child_config_path.unlink(missing_ok=True)

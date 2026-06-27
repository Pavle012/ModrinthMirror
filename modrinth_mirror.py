#!/usr/bin/env python3
"""
Modrinth Upstream Mirror
=========================

Mirrors a Modrinth modpack project's version history into a local git
repository as a linear sequence of commits (oldest -> newest), so you can
diff and merge upstream changes into your own modified fork instead of
re-downloading and manually re-applying your edits every release.

Usage:
    python modrinth_mirror.py --config config.yaml
    python modrinth_mirror.py --config config.yaml --dry-run
    python modrinth_mirror.py --config config.yaml --yes

See config.example.yaml for all available options and README.md for setup,
cron/systemd, and GitHub Actions usage.
"""

import argparse
import fnmatch
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
import yaml

SCRIPT_VERSION = "1.0.0"
DEFAULT_API_BASE = "https://api.modrinth.com/v2"


class ConfigError(Exception):
    pass


class MirrorError(Exception):
    pass


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class ModrinthConfig:
    project: str
    api_base_url: str = DEFAULT_API_BASE
    user_agent: str = ""
    loaders: list = field(default_factory=list)
    game_versions: list = field(default_factory=list)
    version_types: list = field(default_factory=lambda: ["release"])
    featured_only: bool = False
    request_timeout_seconds: int = 30
    max_retries: int = 5
    retry_backoff_seconds: float = 2.0


@dataclass
class RepoConfig:
    path: str
    remote_url: Optional[str] = None
    remote_name: str = "origin"
    mirror_branch: str = "upstream"
    create_orphan_mirror_branch: bool = True
    push_after_mirror: bool = False
    git_user_name: Optional[str] = None
    git_user_email: Optional[str] = None
    preserve_paths: list = field(default_factory=list)
    tag_versions: bool = True
    tag_prefix: str = "upstream-"


@dataclass
class ExtractionConfig:
    mode: str = "full"  # full | overrides_only
    clean_before_extract: bool = True
    keep_modrinth_index: bool = True


@dataclass
class StateConfig:
    file: str = ".mirror_state.json"
    store_in_repo: bool = True


@dataclass
class CommitConfig:
    message_template: str = "Upstream {version_number} ({version_type})\n\n{changelog}"
    author_name: Optional[str] = None
    author_email: Optional[str] = None


@dataclass
class RunConfig:
    max_versions_per_run: int = 0
    confirm_destructive: bool = True


@dataclass
class MergeConfig:
    enabled: bool = False
    target_branch: str = "main"
    strategy: str = "merge"  # merge | rebase
    on_conflict: str = "abort"  # abort | leave


@dataclass
class GithubNotify:
    repo: str = ""
    token_env_var: str = "GITHUB_TOKEN"
    labels: list = field(default_factory=lambda: ["upstream-update"])


@dataclass
class WebhookNotify:
    url: Optional[str] = None


@dataclass
class NotificationConfig:
    enabled: bool = False
    method: str = "github_issue"  # github_issue | webhook
    github: GithubNotify = field(default_factory=GithubNotify)
    webhook: WebhookNotify = field(default_factory=WebhookNotify)


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: Optional[str] = None


@dataclass
class Config:
    modrinth: ModrinthConfig
    repo: RepoConfig
    extraction: ExtractionConfig
    state: StateConfig
    commit: CommitConfig
    run: RunConfig
    merge: MergeConfig
    notifications: NotificationConfig
    logging: LoggingConfig


def load_config(path: str) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {path}")
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    modrinth_raw = raw.get("modrinth", {}) or {}
    if not modrinth_raw.get("project"):
        raise ConfigError("modrinth.project is required (slug or ID)")
    if not modrinth_raw.get("user_agent"):
        raise ConfigError(
            "modrinth.user_agent is required. Modrinth requires a descriptive "
            "User-Agent, e.g. 'github_username/project_name/1.0 (contact_email)'"
        )

    filters = modrinth_raw.get("filters", {}) or {}
    modrinth = ModrinthConfig(
        project=modrinth_raw["project"],
        api_base_url=modrinth_raw.get("api_base_url", DEFAULT_API_BASE),
        user_agent=modrinth_raw["user_agent"],
        loaders=filters.get("loaders", []) or [],
        game_versions=filters.get("game_versions", []) or [],
        version_types=filters.get("version_types", ["release"]) or ["release"],
        featured_only=filters.get("featured_only", False),
        request_timeout_seconds=modrinth_raw.get("request_timeout_seconds", 30),
        max_retries=modrinth_raw.get("max_retries", 5),
        retry_backoff_seconds=modrinth_raw.get("retry_backoff_seconds", 2.0),
    )

    repo_raw = raw.get("repo", {}) or {}
    if not repo_raw.get("path"):
        raise ConfigError("repo.path is required")
    repo = RepoConfig(
        path=repo_raw["path"],
        remote_url=repo_raw.get("remote_url"),
        remote_name=repo_raw.get("remote_name", "origin"),
        mirror_branch=repo_raw.get("mirror_branch", "upstream"),
        create_orphan_mirror_branch=repo_raw.get("create_orphan_mirror_branch", True),
        push_after_mirror=repo_raw.get("push_after_mirror", False),
        git_user_name=repo_raw.get("git_user_name"),
        git_user_email=repo_raw.get("git_user_email"),
        preserve_paths=repo_raw.get("preserve_paths", []) or [],
        tag_versions=repo_raw.get("tag_versions", True),
        tag_prefix=repo_raw.get("tag_prefix", "upstream-"),
    )

    extraction_raw = raw.get("extraction", {}) or {}
    mode = extraction_raw.get("mode", "full")
    if mode not in ("full", "overrides_only"):
        raise ConfigError("extraction.mode must be 'full' or 'overrides_only'")
    extraction = ExtractionConfig(
        mode=mode,
        clean_before_extract=extraction_raw.get("clean_before_extract", True),
        keep_modrinth_index=extraction_raw.get("keep_modrinth_index", True),
    )

    state_raw = raw.get("state", {}) or {}
    state = StateConfig(
        file=state_raw.get("file", ".mirror_state.json"),
        store_in_repo=state_raw.get("store_in_repo", True),
    )

    commit_raw = raw.get("commit", {}) or {}
    commit = CommitConfig(
        message_template=commit_raw.get(
            "message_template",
            "Upstream {version_number} ({version_type})\n\n{changelog}",
        ),
        author_name=commit_raw.get("author_name"),
        author_email=commit_raw.get("author_email"),
    )

    run_raw = raw.get("run", {}) or {}
    run = RunConfig(
        max_versions_per_run=run_raw.get("max_versions_per_run", 0),
        confirm_destructive=run_raw.get("confirm_destructive", True),
    )

    merge_raw = raw.get("merge", {}) or {}
    strategy = merge_raw.get("strategy", "merge")
    if strategy not in ("merge", "rebase"):
        raise ConfigError("merge.strategy must be 'merge' or 'rebase'")
    on_conflict = merge_raw.get("on_conflict", "abort")
    if on_conflict not in ("abort", "leave"):
        raise ConfigError("merge.on_conflict must be 'abort' or 'leave'")
    merge = MergeConfig(
        enabled=merge_raw.get("enabled", False),
        target_branch=merge_raw.get("target_branch", "main"),
        strategy=strategy,
        on_conflict=on_conflict,
    )

    notif_raw = raw.get("notifications", {}) or {}
    github_raw = notif_raw.get("github", {}) or {}
    webhook_raw = notif_raw.get("webhook", {}) or {}
    notifications = NotificationConfig(
        enabled=notif_raw.get("enabled", False),
        method=notif_raw.get("method", "github_issue"),
        github=GithubNotify(
            repo=github_raw.get("repo", ""),
            token_env_var=github_raw.get("token_env_var", "GITHUB_TOKEN"),
            labels=github_raw.get("labels", ["upstream-update"]),
        ),
        webhook=WebhookNotify(url=webhook_raw.get("url")),
    )

    logging_raw = raw.get("logging", {}) or {}
    logging_cfg = LoggingConfig(
        level=logging_raw.get("level", "INFO"),
        file=logging_raw.get("file"),
    )

    return Config(
        modrinth=modrinth,
        repo=repo,
        extraction=extraction,
        state=state,
        commit=commit,
        run=run,
        merge=merge,
        notifications=notifications,
        logging=logging_cfg,
    )


# ---------------------------------------------------------------------------
# Modrinth API client
# ---------------------------------------------------------------------------

class ModrinthClient:
    def __init__(self, cfg: ModrinthConfig, logger):
        self.cfg = cfg
        self.logger = logger
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": cfg.user_agent})

    def _request(self, method, url, **kwargs):
        last_exc = None
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                resp = self.session.request(
                    method, url, timeout=self.cfg.request_timeout_seconds, **kwargs
                )
                if resp.status_code == 429:
                    retry_after = float(
                        resp.headers.get(
                            "Retry-After", self.cfg.retry_backoff_seconds * attempt
                        )
                    )
                    self.logger.warning(f"Rate limited by Modrinth, sleeping {retry_after}s")
                    time.sleep(retry_after)
                    continue
                resp.raise_for_status()
                return resp
            except requests.RequestException as e:
                last_exc = e
                wait = self.cfg.retry_backoff_seconds * attempt
                self.logger.warning(
                    f"Request failed ({e}), retry {attempt}/{self.cfg.max_retries} in {wait:.1f}s"
                )
                time.sleep(wait)
        raise MirrorError(f"Request to {url} failed after {self.cfg.max_retries} attempts: {last_exc}")

    def get_project(self):
        url = f"{self.cfg.api_base_url}/project/{self.cfg.project}"
        return self._request("GET", url).json()

    def get_versions(self):
        url = f"{self.cfg.api_base_url}/project/{self.cfg.project}/version"
        return self._request("GET", url).json()

    def download_file(self, url, dest_path: Path):
        resp = self._request("GET", url, stream=True)
        try:
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
        finally:
            resp.close()


def filter_versions(versions, cfg: ModrinthConfig):
    out = []
    for v in versions:
        if cfg.version_types and v.get("version_type") not in cfg.version_types:
            continue
        if cfg.loaders and not (set(v.get("loaders", [])) & set(cfg.loaders)):
            continue
        if cfg.game_versions and not (set(v.get("game_versions", [])) & set(cfg.game_versions)):
            continue
        if cfg.featured_only and not v.get("featured", False):
            continue
        if not v.get("files"):
            continue
        out.append(v)
    out.sort(key=lambda v: v["date_published"])
    return out


def pick_primary_file(version):
    files = version.get("files", [])
    for f in files:
        if f.get("primary"):
            return f
    return files[0] if files else None


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def run_git(repo_path, args, check=True, capture=False, env=None):
    cmd = ["git", "-C", str(repo_path)] + args
    result = subprocess.run(cmd, capture_output=capture, text=True, env=env)
    if check and result.returncode != 0:
        stderr = result.stderr if capture else "(see above)"
        raise MirrorError(f"git command failed: {' '.join(cmd)}\n{stderr}")
    return result


def ensure_repo(cfg: RepoConfig, logger) -> Path:
    path = Path(cfg.path)
    if not path.exists():
        if cfg.remote_url:
            logger.info(f"Cloning {cfg.remote_url} into {path}")
            subprocess.run(["git", "clone", cfg.remote_url, str(path)], check=True)
        else:
            logger.info(f"Initializing new git repo at {path}")
            path.mkdir(parents=True, exist_ok=True)
            run_git(path, ["init"])

    if cfg.git_user_name:
        run_git(path, ["config", "user.name", cfg.git_user_name])
    if cfg.git_user_email:
        run_git(path, ["config", "user.email", cfg.git_user_email])

    branches = run_git(path, ["branch", "--list", cfg.mirror_branch], capture=True).stdout
    branch_exists = cfg.mirror_branch in branches

    if branch_exists:
        run_git(path, ["checkout", cfg.mirror_branch])
        return path

    has_commits = run_git(
        path, ["rev-parse", "--verify", "HEAD"], check=False, capture=True
    ).returncode == 0

    if cfg.create_orphan_mirror_branch and has_commits:
        logger.info(f"Creating orphan branch '{cfg.mirror_branch}' (no shared history with existing branches)")
        run_git(path, ["checkout", "--orphan", cfg.mirror_branch])
        run_git(path, ["rm", "-rf", "--cached", "."], check=False)
        for entry in path.iterdir():
            if entry.name == ".git":
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    else:
        run_git(path, ["checkout", "-b", cfg.mirror_branch])

    return path


def clean_repo_dir(repo_path: Path, preserve_paths, state_file_rel: Optional[str]):
    preserve = set(preserve_paths)
    preserve.add(".git")
    if state_file_rel:
        preserve.add(state_file_rel)

    def is_preserved(rel_name: str) -> bool:
        for pat in preserve:
            pat_norm = pat.rstrip("/")
            if rel_name == pat_norm or rel_name.startswith(pat_norm + "/"):
                return True
            if fnmatch.fnmatch(rel_name, pat):
                return True
        return False

    for entry in repo_path.iterdir():
        if is_preserved(entry.name):
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


# ---------------------------------------------------------------------------
# .mrpack extraction
# ---------------------------------------------------------------------------

def _copy_tree_merge(src: Path, dst: Path):
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def extract_mrpack(mrpack_path: Path, repo_path: Path, extraction_cfg: ExtractionConfig):
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with zipfile.ZipFile(mrpack_path) as zf:
            zf.extractall(tmp_path)

        index_file = tmp_path / "modrinth.index.json"
        override_dirs = [
            tmp_path / "overrides",
            tmp_path / "client-overrides",
            tmp_path / "server-overrides",
        ]

        for d in override_dirs:
            if d.exists():
                _copy_tree_merge(d, repo_path)

        if extraction_cfg.mode == "full" and extraction_cfg.keep_modrinth_index and index_file.exists():
            shutil.copy2(index_file, repo_path / "modrinth.index.json")


# ---------------------------------------------------------------------------
# Commit / tag
# ---------------------------------------------------------------------------

def commit_version(repo_path: Path, cfg: Config, version, logger) -> Optional[str]:
    run_git(repo_path, ["add", "-A"])
    status = run_git(repo_path, ["status", "--porcelain"], capture=True).stdout
    if not status.strip():
        return None

    changelog = (version.get("changelog") or "(no changelog provided)").strip()
    try:
        message = cfg.commit.message_template.format(
            version_number=version.get("version_number", "?"),
            version_name=version.get("name", "?"),
            version_id=version.get("id", "?"),
            version_type=version.get("version_type", "?"),
            date=version.get("date_published", "?"),
            changelog=changelog,
            project=cfg.modrinth.project,
        )
    except (KeyError, IndexError) as e:
        logger.warning(f"commit.message_template has an invalid placeholder ({e}); using fallback message")
        message = f"Upstream {version.get('version_number', '?')}"

    env = os.environ.copy()
    if cfg.commit.author_name:
        env["GIT_AUTHOR_NAME"] = cfg.commit.author_name
        env["GIT_COMMITTER_NAME"] = cfg.commit.author_name
    if cfg.commit.author_email:
        env["GIT_AUTHOR_EMAIL"] = cfg.commit.author_email
        env["GIT_COMMITTER_EMAIL"] = cfg.commit.author_email

    result = subprocess.run(
        ["git", "-C", str(repo_path), "commit", "-m", message],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MirrorError(f"git commit failed: {result.stderr}")

    return run_git(repo_path, ["rev-parse", "HEAD"], capture=True).stdout.strip()


def tag_version(repo_path: Path, cfg: RepoConfig, version, sha: str):
    if not cfg.tag_versions:
        return
    tag_name = f"{cfg.tag_prefix}{version.get('version_number', version['id'])}".replace(" ", "_")
    existing = run_git(repo_path, ["tag", "-l", tag_name], capture=True).stdout.strip()
    if existing:
        return
    run_git(repo_path, ["tag", tag_name, sha])


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def state_path(cfg: StateConfig, repo_path: Path) -> Path:
    if cfg.store_in_repo:
        return repo_path / cfg.file
    return Path(cfg.file)


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"mirrored_version_ids": [], "last_version_id": None}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Merge into target branch
# ---------------------------------------------------------------------------

def attempt_merge(cfg: Config, repo_path: Path, logger) -> bool:
    """Returns True if a conflict occurred."""
    target = cfg.merge.target_branch
    mirror_branch = cfg.repo.mirror_branch

    branches = run_git(repo_path, ["branch", "--list", target], capture=True).stdout
    if target not in branches:
        logger.warning(f"merge.target_branch '{target}' does not exist locally, skipping merge")
        return False

    logger.info(f"Attempting to {cfg.merge.strategy} '{mirror_branch}' into '{target}'")
    run_git(repo_path, ["checkout", target])

    if cfg.merge.strategy == "merge":
        cmd = [
            "git", "-C", str(repo_path), "merge", mirror_branch,
            "--allow-unrelated-histories",
            "-m", f"Merge upstream changes from {mirror_branch}",
        ]
    else:
        cmd = ["git", "-C", str(repo_path), "rebase", mirror_branch]

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        logger.warning(f"Merge/rebase produced conflicts:\n{result.stdout}\n{result.stderr}")
        if cfg.merge.on_conflict == "abort":
            abort_cmd = "merge" if cfg.merge.strategy == "merge" else "rebase"
            subprocess.run(["git", "-C", str(repo_path), abort_cmd, "--abort"])
            logger.info(f"{abort_cmd} aborted; '{target}' left in its pre-merge state")
        else:
            logger.warning(f"Leaving '{target}' in a conflicted state for manual resolution")
        return True

    logger.info(f"Merge/rebase of '{mirror_branch}' into '{target}' succeeded cleanly")
    return False


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def send_notification(cfg: Config, versions, conflict_happened: bool, logger):
    if cfg.notifications.method == "github_issue":
        _send_github_issue(cfg, versions, conflict_happened, logger)
    elif cfg.notifications.method == "webhook":
        _send_webhook(cfg, versions, conflict_happened, logger)
    else:
        logger.warning(f"Unknown notifications.method: {cfg.notifications.method}")


def _send_github_issue(cfg: Config, versions, conflict_happened: bool, logger):
    token = os.environ.get(cfg.notifications.github.token_env_var)
    if not token:
        logger.warning(
            f"No token in env var {cfg.notifications.github.token_env_var}, skipping issue creation"
        )
        return
    repo = cfg.notifications.github.repo
    if not repo:
        logger.warning("notifications.github.repo not set, skipping issue creation")
        return

    version_list = "\n".join(f"- {v['version_number']} ({v['date_published']})" for v in versions)
    title = f"Upstream update: {len(versions)} new version(s) mirrored"
    body = f"The following upstream versions were mirrored:\n\n{version_list}\n"
    if conflict_happened:
        body += "\n\u26a0\ufe0f Merge into the target branch produced conflicts and needs manual resolution.\n"

    resp = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"title": title, "body": body, "labels": cfg.notifications.github.labels},
        timeout=30,
    )
    if resp.status_code >= 300:
        logger.warning(f"Failed to create GitHub issue: {resp.status_code} {resp.text}")
    else:
        logger.info(f"Created GitHub issue: {resp.json().get('html_url')}")


def _send_webhook(cfg: Config, versions, conflict_happened: bool, logger):
    url = cfg.notifications.webhook.url
    if not url:
        logger.warning("notifications.webhook.url not set, skipping webhook")
        return
    payload = {
        "versions": [
            {"version_number": v["version_number"], "id": v["id"], "date_published": v["date_published"]}
            for v in versions
        ],
        "conflict": conflict_happened,
    }
    resp = requests.post(url, json=payload, timeout=30)
    if resp.status_code >= 300:
        logger.warning(f"Webhook call failed: {resp.status_code} {resp.text}")


# ---------------------------------------------------------------------------
# Main mirror routine
# ---------------------------------------------------------------------------

def mirror(cfg: Config, args, logger):
    client = ModrinthClient(cfg.modrinth, logger)

    logger.info(f"Fetching project info for '{cfg.modrinth.project}'")
    project = client.get_project()
    logger.info(f"Project: {project.get('title')} ({project.get('id')})")

    logger.info("Fetching version list")
    versions = filter_versions(client.get_versions(), cfg.modrinth)
    logger.info(f"{len(versions)} version(s) match configured filters")

    if not versions:
        logger.warning("No versions matched filters; nothing to do")
        return

    repo_path = ensure_repo(cfg.repo, logger)

    sp = state_path(cfg.state, repo_path)
    state_file_rel = None
    if cfg.state.store_in_repo:
        try:
            state_file_rel = str(sp.relative_to(repo_path))
        except ValueError:
            state_file_rel = cfg.state.file
    state = load_state(sp)
    mirrored_ids = set(state.get("mirrored_version_ids", []))

    new_versions = [v for v in versions if v["id"] not in mirrored_ids]
    if not new_versions:
        logger.info("Already up to date with upstream \u2014 no new versions to mirror")
        return

    if cfg.run.max_versions_per_run > 0:
        new_versions = new_versions[: cfg.run.max_versions_per_run]

    logger.info(
        f"{len(new_versions)} new version(s) to mirror: "
        + ", ".join(v["version_number"] for v in new_versions)
    )

    if args.dry_run:
        for v in new_versions:
            logger.info(f"[dry-run] Would mirror {v['version_number']} ({v['id']}, {v['date_published']})")
        return

    if cfg.run.confirm_destructive and not args.yes:
        resp = input(
            f"About to mirror {len(new_versions)} version(s) onto branch "
            f"'{cfg.repo.mirror_branch}' in {repo_path}. Continue? [y/N] "
        )
        if resp.strip().lower() not in ("y", "yes"):
            logger.info("Aborted by user")
            return

    mirrored_count = 0
    with tempfile.TemporaryDirectory() as tmp_dl:
        tmp_dl_path = Path(tmp_dl)
        for version in new_versions:
            file_info = pick_primary_file(version)
            if not file_info:
                logger.warning(f"Version {version['version_number']} has no downloadable file, skipping")
                mirrored_ids.add(version["id"])
                continue

            logger.info(f"Mirroring {version['version_number']} ({version['date_published']})")

            mrpack_path = tmp_dl_path / file_info["filename"]
            client.download_file(file_info["url"], mrpack_path)

            if cfg.extraction.clean_before_extract:
                clean_repo_dir(repo_path, cfg.repo.preserve_paths, state_file_rel)

            extract_mrpack(mrpack_path, repo_path, cfg.extraction)

            sha = commit_version(repo_path, cfg, version, logger)
            if sha:
                logger.info(f"Committed {sha[:10]} for {version['version_number']}")
                tag_version(repo_path, cfg.repo, version, sha)
            else:
                logger.info(f"No file changes for {version['version_number']}, no commit made")

            mirrored_ids.add(version["id"])
            mirrored_count += 1

            state["mirrored_version_ids"] = sorted(mirrored_ids)
            state["last_version_id"] = version["id"]
            state["last_mirrored_at"] = version["date_published"]
            save_state(sp, state)

            if cfg.state.store_in_repo:
                run_git(repo_path, ["add", state_file_rel])
                st = run_git(repo_path, ["status", "--porcelain"], capture=True).stdout
                if st.strip():
                    run_git(repo_path, ["commit", "-m", f"Update mirror state after {version['version_number']}"])

            mrpack_path.unlink(missing_ok=True)

    logger.info(f"Mirrored {mirrored_count} new version(s)")

    if cfg.repo.push_after_mirror and mirrored_count > 0:
        logger.info(f"Pushing '{cfg.repo.mirror_branch}' to '{cfg.repo.remote_name}'")
        run_git(repo_path, ["push", cfg.repo.remote_name, cfg.repo.mirror_branch, "--tags"])

    conflict_happened = False
    if cfg.merge.enabled and mirrored_count > 0:
        conflict_happened = attempt_merge(cfg, repo_path, logger)
        if cfg.repo.push_after_mirror:
            run_git(repo_path, ["push", cfg.repo.remote_name, cfg.merge.target_branch], check=False)

    if cfg.notifications.enabled and mirrored_count > 0:
        send_notification(cfg, new_versions[:mirrored_count], conflict_happened, logger)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def setup_logging(cfg: LoggingConfig, verbose: bool):
    level = logging.DEBUG if verbose else getattr(logging, cfg.level.upper(), logging.INFO)
    handlers = [logging.StreamHandler(sys.stdout)]
    if cfg.file:
        handlers.append(logging.FileHandler(cfg.file))
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s", handlers=handlers)
    return logging.getLogger("modrinth_mirror")


def main():
    parser = argparse.ArgumentParser(description="Mirror a Modrinth modpack's release history into git")
    parser.add_argument("-c", "--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen, make no changes")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose (debug) logging")
    parser.add_argument("--version", action="version", version=f"modrinth_mirror {SCRIPT_VERSION}")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        sys.exit(1)

    logger = setup_logging(cfg.logging, args.verbose)

    try:
        mirror(cfg, args, logger)
    except MirrorError as e:
        logger.error(str(e))
        sys.exit(1)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        sys.exit(130)


if __name__ == "__main__":
    main()

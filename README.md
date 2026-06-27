# Modrinth Upstream Mirror

Mirrors a Modrinth modpack's release history into a git repo as a linear
sequence of commits (oldest \u2192 newest), so you can `git merge`/`git rebase`
upstream changes into your own modified fork instead of manually re-applying
your edits every time the original updates.

## How it works

1. Queries the Modrinth API for every version of a project, filtered by
   loader/game version/release type per your config.
2. For each version it hasn't seen before, downloads the `.mrpack`, extracts
   it into a dedicated `upstream` branch, and commits it.
3. Tracks which version IDs it has already mirrored in a state file, so
   reruns are incremental (only new releases get processed).
4. Optionally merges/rebases the `upstream` branch into your working branch,
   and optionally opens a GitHub issue or hits a webhook when there's
   something new (or a conflict needing your attention).

The `upstream` branch is created as an **orphan branch** by default, so it
never shares history with your own working branch. You merge it in
explicitly, on your terms.

## Setup

```bash
cd modrinth-mirror
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
# edit config.yaml: at minimum set modrinth.project, modrinth.user_agent, repo.path
```

Find the project slug/ID from the Modrinth URL, e.g.
`modrinth.com/modpack/assembly-line-smp` \u2192 slug is `assembly-line-smp`.

## First run

```bash
python modrinth_mirror.py --config config.yaml --dry-run   # see what it would do
python modrinth_mirror.py --config config.yaml             # mirrors everything, asks to confirm
```

This populates the `upstream` branch with one commit per matching upstream
release. From there, branch off for your own changes:

```bash
cd my-pack-fork
git checkout upstream
git checkout -b my-changes
# make your edits, commit them
```

## Staying in sync

Just rerun the script whenever you want to check for updates:

```bash
python modrinth_mirror.py --config config.yaml --yes
```

It only mirrors versions it hasn't seen. If you set `merge.enabled: true`
with `target_branch: my-changes`, it'll also attempt to merge new upstream
commits straight into your branch and tell you if there's a conflict to
resolve by hand. Otherwise, do it yourself:

```bash
git checkout my-changes
git merge upstream --allow-unrelated-histories
```

## Automating it

**GitHub Actions** \u2014 `.github/workflows/mirror-upstream.yml` is included,
runs daily via cron plus manual `workflow_dispatch`. Set `repo.path: "."` and
`repo.push_after_mirror: true` in `config.yaml` for CI use.

**systemd user timer** \u2014 `systemd/modrinth-mirror.service` and `.timer` are
included if you'd rather run it locally (same pattern as your other
self-hosted services):

```bash
mkdir -p ~/modrinth-mirror && cp -r * ~/modrinth-mirror/
cp systemd/modrinth-mirror.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now modrinth-mirror.timer
systemctl --user list-timers | grep modrinth   # confirm it's scheduled
journalctl --user -u modrinth-mirror.service   # check logs after a run
```

## Notes on the MIT license

Mirroring and modifying the pack is fine under MIT \u2014 just keep the original
license/copyright notice in the repo and credit the original author in your
README (see the earlier setup steps for exact wording). MIT covers the pack
curator's config work; individual bundled mods keep their own licenses,
which is also why `.mrpack` files reference mod downloads by URL rather than
embedding the jars.

## Config reference

See `config.example.yaml` \u2014 every option is commented inline. Key ones to
know about:

| Option | Purpose |
|---|---|
| `modrinth.filters.*` | restrict which versions get mirrored (loader, MC version, release/beta/alpha) |
| `repo.preserve_paths` | files never wiped when a new version is extracted (e.g. your `README.md`, `.github/`) |
| `extraction.mode` | `full` (overrides + index.json) or `overrides_only` |
| `run.max_versions_per_run` | cap how many versions get mirrored in one run (useful for a controlled first import) |
| `merge.enabled` | auto-merge upstream into your working branch each run |
| `notifications.*` | GitHub issue or webhook ping on new versions / conflicts |

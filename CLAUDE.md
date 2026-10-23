# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Edgemanage picks the healthiest hosts ("edges") out of a pool by timing an HTTP fetch of a known
test object from each one, then writes bind-compatible zone files whose apex A records point at the
winners. It is a cron-driven batch job (default every 60s), not a service. Python 3, packaged with
setuptools; three console scripts live in [edgemanage/](edgemanage/) with no `.py` extension:
`edge_manage` (the rotation run), `edge_query` (read edge state), `edge_conf` (set edge mode).

## Commands

```bash
tox                              # full gate: pylint -E + flake8 (max-line-length 100) + pytest
tox -e style                     # lint only
pytest                           # unit tests
pytest tests/test_decisionmaker.py::TestDecisionMaker::test_name   # single test
pip install -r requirements.txt -r test-requirements.txt
python setup.py install          # required before integration tests (see below)
```

Dependencies are locked with [uv](https://github.com/astral-sh/uv). The `.in` files are the
hand-edited sources and the `.txt` files are generated, pinned and hash-checked; never edit a
`.txt` by hand. Regenerate after editing an `.in` (runtime lock first, the test lock constrains
against it):

```bash
uv pip compile requirements.in -o requirements.txt \
    --python-version 3.11 --generate-hashes
uv pip compile test-requirements.in -o test-requirements.txt \
    --python-version 3.11 --generate-hashes -c requirements.txt
```

Runtime versions are pinned to the production server's `pip freeze`, with transitive pins held in
`constraints.txt`. setuptools is *not* a runtime dependency: `edge_manage` reads its `--version`
from `importlib.metadata`, not `pkg_resources`. [pyproject.toml](pyproject.toml) declares it as a
build dependency only, and exists so that `pip install -e .` takes the PEP 660 path instead of
legacy `setup.py develop`, which wraps each `scripts` entry in a `pkg_resources` shim that warns on
every run. See INSTALL.md for the full workflow.

Integration tests ([tests/test_edge_manage_integration.py](tests/test_edge_manage_integration.py))
spawn a Flask server via pexpect and shell out to `edge_manage`, so they need the package
**installed on PATH** and must run **from the repo root**: they read `conf/edgemanage.yaml` and
`tests/test_data/` by relative path. They also assert on wall-clock runtime (e.g. under 1.5s for 20
edges), so they are flaky on loaded machines; that is expected, not a regression to "fix" by
loosening unrelated code.

They only override *some* of the paths in `conf/edgemanage.yaml`, so they inherit the real
`prometheus_logs` (`/var/log/prom/`) and `named_dir` (`/var/cache/bind/`). Those do not exist on a
developer laptop, so **run the suite in Docker**, which creates them
([docker/test/Dockerfile](docker/test/Dockerfile)); all 20 tests pass there:

```bash
docker compose run --rm test                  # whole suite, ~25s
docker compose build test                     # after editing source
```

The service is behind a `test` profile, so `docker compose up` ignores it. Do not "fix" a local
`/var/log/prom/` failure by editing the config or the test.

CI is GitHub Actions ([.github/workflows/python-app.yml](.github/workflows/python-app.yml)) on
Python 3.11.17 running `tox`. A legacy CircleCI config is still present but the Actions workflow is
the live one. Two more workflows, `pip-audit` and `version-check`, watch the pinned versions; see
"Pinned versions and CI audits" below.

The GitLab mirror runs the same three jobs from [.gitlab-ci.yml](.gitlab-ci.yml): `tox`,
`version-check` and `pip-audit`. **Change both CI systems together.** Its `workflow: rules:` run an
MR pipeline when the branch has an open MR and a branch pipeline otherwise, so a push never starts
both; `tox` runs in either but not on the schedule. The production Python version is the
`PROD_PYTHON_VERSION` variable there, the literal `python-version` in the GitHub workflows.

## Architecture

One run of `edge_manage -A <dnet>` is a pipeline. `dnet` ("Deflect network") is the unit of
everything: edge list file, state file, zone template dir, canary file, and live list are all keyed
or `{dnet}`-templated by it.

1. **[edge_manage](edgemanage/edge_manage)**: loads YAML config, acquires an fcntl lock
   (`util.acquire_lock`), refuses to run within 30s of the last run unless `--force`, loads the
   `StateFile` JSON, loads the flat edge list, then calls into `EdgeManage`. Also runs the
   `commands.run_before` / `run_after` / `run_after_changes` hooks and writes Prometheus metrics.
2. **[EdgeManage](edgemanage/edgemanage.py)**: the orchestrator and the file you will spend most
   time in. `do_edge_tests()` fans out fetches over a `ThreadPoolExecutor`; `make_edges_live()`
   selects the live set and renders zone files.
3. **[EdgeTest](edgemanage/edgetest.py)**: one fetch. Resolves the edge hostname itself, then uses
   the `OverrideDNS` context manager to monkeypatch `socket.getaddrinfo` so `requests` hits that
   specific IP while sending the configured `Host` header. Compares the md5 of the response against
   the local copy of the test object. Raises `VerifyFailed` / `FetchFailed`.
4. **[DecisionMaker](edgemanage/decisionmaker.py)**: turns fetch times into a judgement per edge,
   one of `const.VALID_HEALTHS`. Order matters and is load-bearing: last value under `goodenough`
   → `pass_threshold`; last value == `FETCH_TIMEOUT` → `fail` (checked *before* averages, so a
   fresh failure can't be masked by a good history); `DECISION_SLICE_WINDOW`-second average under
   `goodenough` → `pass_window`; lifetime average under it → `pass_average`; else `pass`.
5. **[EdgeList](edgemanage/edgelist.py)**: the chosen set, plus `generate_zone()`, which resolves
   each live edge to an IP and renders
   [templates/zonetemplate.j2](edgemanage/templates/zonetemplate.j2) around the operator-supplied
   per-domain include file. Serial number is `int(time.time())`.

### Selection logic in `make_edges_live()`

Stability is the point: DNS is only changed when it has to be. Edges previously live and still
`pass_threshold` are kept (`check_last_live()`); if that fills `edge_count` (or the per-dnet
`dnet_edge_count` override), nothing is rewritten. Otherwise the shortfall is filled by walking
health tiers best-first (`pass_threshold`, `pass_window`, `pass_average`, `pass`), taking the
fastest in each. If all tiers are exhausted, the last live set is re-added even though it is
failing, on the grounds that stale edges beat an empty A record set. Edge `mode` (`available`,
`force`, `blindforce`, `unavailable`; see [const.py](edgemanage/const.py)) is applied on top and
is set out-of-band by `edge_conf`.

Dnets listed in the optional `dnet_rotation_minutes` config take a separate path,
`select_time_rotation_edge()`, which runs *instead of* everything above (the existing block sits
untouched under an `else:`). Such a dnet serves exactly one edge and replaces it when the timer is
due, when it fails, or when another edge is in a strictly better tier. The timer is
`state_obj.last_rotation()`, so a health failover restarts it, with `const.TIME_ROTATION_GRACE` to
absorb cron jitter. The next edge is random within the best tier among edges not yet in the
statefile's `rotation_cycle` (a shuffle bag, reset when the tier is used up). `edgelist_changed`
is derived by comparing against `last_live` rather than by the flag juggling above.

Zone files are rewritten only when the edge list changed, a canary changed, the template's mtime
changed, or `--force-update` was passed. Both the `for/else` constructs and the mtime bookkeeping
in `state_obj.zone_mtimes` exist for this; be careful editing them.

### Canaries

Per-zone extra IPs from `canary_files`, tracked by a **separate** `DecisionMaker` and never counted
toward `edge_count`. A healthy canary displaces one random live edge in that zone's file only. If
`canary_killer` canaries fail, all remaining canary futures are cancelled and every canary is
force-failed, because they usually share a host, so a few failures imply the whole box is gone. Canaries
are always stored with state `out`.

### Persistence

Two flat-file stores, both hand-rolled and both with back-compat quirks:

- **[EdgeState](edgemanage/edgestate.py)**: one JSON `<edge>.edgestore` per edge under
  `healthdata_store`, holding `fetch_times` (capped at `FETCH_HISTORY`), hourly
  `historical_average`, rotation history, mode, state, health, comment. Missing keys are backfilled
  from `ASSUMED_VALS`, which is the migration mechanism for adding a field. Writes go through
  `util.open_atomic` because a truncated statefile blocks future runs. Note `fetch_times` keys are
  timestamps cast to **strings**, a documented legacy hack; code reading them does `float(ts)`.
- **[StateFile](edgemanage/statefile.py)**: one JSON blob per dnet: `last_live`, `last_run`,
  `rotation_list`, `zone_mtimes`, `active_canaries`, `rotation_cycle`. Same pattern: defaults set in `__init__`, then
  overwritten from the loaded dict, so new fields are additive.

Corrupt or unparsable edge state is logged and skipped rather than fatal; several call sites depend
on an edge being absent from `edge_states` mid-run.

### Other pieces

[monitor.py](edgemanage/monitor.py) is a singleton Prometheus registry written to a textfile at the
end of a run; gauges must be created up front from the edge list, and `Monitor().set()` silently
drops unknown keys. [adapter.py](edgemanage/adapter.py) (`EdgemanageAdapter`) is a non-CLI entry
point used by Django callers to read config and reconcile dnet edge-list files.
[nagios/](nagios/) checks are deliberately stdlib-only and read the state and health files directly.

## Conventions

- Python 3, but `six`, `from __future__ import absolute_import` and `six.iteritems` are still
  everywhere; match the surrounding file rather than modernizing in passing.
- flake8 at 100 columns; pylint runs errors-only (`-E`). Several `# pylint: disable=no-member` are
  load-bearing where attributes are set dynamically from `ASSUMED_VALS`.
- `const.FETCH_TIMEOUT` doubles as the sentinel "this edge failed" fetch time; don't introduce a
  separate failure value without updating `DecisionMaker`.
- The config key `testing: true` changes behavior (raw IP sent as the `Host` header, `timeout`
  overrides `FETCH_TIMEOUT`) and exists for the integration suite.
- Branches: `develop` is the main branch; releases and hotfixes merge via PR.

## Pinned versions and CI audits

Every pinned version is watched by exactly one of two jobs, defined both as GitHub Actions
workflows and in `.gitlab-ci.yml`. Both run on every pull/merge request (no path or `changes:`
filters), on a weekly schedule, and on manual runs. On GitHub the schedule is in the workflow
(Monday 06:00 UTC, `develop` only); on GitLab it is a pipeline schedule set in the project
settings (Build > Pipeline schedules), not in the repo.

- **`pip-audit`** ([.github/workflows/pip-audit.yml](.github/workflows/pip-audit.yml)) owns
  `requirements.txt` and `test-requirements.txt`. It checks both locks against known advisories
  and **blocks** the PR when it finds one, so a new upstream disclosure blocks the next PR whatever
  it touches.
  - **Prefer a fix.** Bump the package in the `.in` file (or `constraints.txt` for a transitive
    pin) and re-lock moving only that package: `uv pip compile ... --upgrade-package <pkg>`.
    Runtime pins track the production `pip freeze`, so a runtime fix is also a production upgrade.
  - **Accept an advisory only with an owner's approval**: add `--ignore-vuln <ID>` to the workflow
    with a comment naming the advisory, why it does not apply, and when to revisit it.
- **`version-check`** ([.github/workflows/version-check.yml](.github/workflows/version-check.yml))
  owns everything else: Docker base images, the Python version, GitHub Actions `uses:` pins,
  `*_VERSION` build args and inline `pip install x==1.2.3` lines. It runs
  [scripts/check_versions.py](scripts/check_versions.py) (stdlib only, `python3
  scripts/check_versions.py` from the repo root), which rates each pin high or low by `RISK_RULES`.
  It **never blocks**: a high-risk pin with a newer patch release turns the job into a warning
  with annotations on the PR, and everything else is listed for information.

Nothing in this repo is deployed (`docker/` is a local harness), so almost every pin is low risk.
The high-risk pins are the ones kept in step with production's Python, where `edge_manage`
parses responses from internet edges: `python-version` in `python-app.yml`, `PROD_PYTHON_VERSION`
in `.gitlab-ci.yml` and the base image of `docker/edgemanage/Dockerfile`.

Reading the report:

- `PATCH`: a newer patch release exists. Bump it (and rebuild the image).
- `MINOR`: only a newer minor exists. Information only; plan it as an upgrade.
- `ERROR`: a registry lookup failed. Re-run the job.
- `UNCHECKED`: a `*_VERSION` variable has no `VERSION_VARS` entry.
- `SKIPPED`: the tag is not a version (e.g. `debian:bookworm-slim`), the registry is not Docker
  Hub, or the action is pinned to a commit SHA.

**Whenever you add or change a Dockerfile, compose file, CI file, `.python-version` /
`.tool-versions`, or a requirements file, read and update `scripts/check_versions.py` in the same
change.** Nothing fails loudly when this is missed.

1. A new service, image, or change in exposure needs a `RISK_RULES` entry or adjustment: a
   published port, a bind address moving off loopback, a new reverse-proxy upstream, a harness
   image that starts tracking production, or the reverse of any of these.
2. A new `ARG`/`ENV`/CI variable ending in `VERSION` needs a `VERSION_VARS` entry, or it reports
   as `UNCHECKED`.
3. A file in a new location or with a new name needs a glob in `DOCKER_FILES` / `TOOLCHAIN_FILES` /
   `REQUIREMENTS_FILES`. A renamed file also silently stops matching its `RISK_RULES` glob.
4. A pin the script cannot see needs to move into a `*_VERSION` arg, or needs a discovery change:
   a non-Docker-Hub registry, a version hardcoded in a URL, an apt `=` pin.
5. A new requirements file must be owned by exactly one job: add it to the `pip-audit` command or
   to `REQUIREMENTS_FILES`.
6. Verify with `python3 scripts/check_versions.py`: the pin is in the table with the right RISK
   and WHY.

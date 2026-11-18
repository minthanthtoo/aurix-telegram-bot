# AuriX Games CI/CD implementation plan

**Goal** — Give Games its own validation, release and rollback path. Keep one installed Games release on Singapore and one previous healthy package off-server.

**Architecture** — Keep the existing GitHub repository. Integrate a reviewed Games baseline into `main`, give Games its own CI workflow and use a manual production workflow to promote a verified package. The Games installer owns only the Games service and its release directory.

**Stack** — GitHub Actions, the existing Python package and validation scripts, SSH, and systemd.

## Implementation status

The isolated Games source and CI changes are in local branch `codex/games-cicd-import`; they have not been pushed. Games CI uses hash-locked Python dependencies under `config/games/`. `scripts/game_store_scope.py` routes Games and other-product changes, and `.github/workflows/ci.yml` uses that result to skip its expensive checks for Games-only changes while preserving the existing `safety-net` status name. `scripts/run_product_checks.py` selects tracked non-Games unittest modules before importing them. The manual production workflow builds and verifies a package only; it stops before upload on this public repository because workflow artifacts are readable to repository readers. No live installer, rollback or pruning is enabled.

Local validation on November 18, 2026: `scripts/run_game_store_checks.py` passed 475 Python tests, 35 browser tests, and `compileall`; `ruff check .`, actionlint, and YAML parsing passed. A temporary-tree package smoke check verified a 338-file, 30.6 MB archive with a 32.5 MB expanded size; the temporary archive was removed and no artifact was published. The clean `main` snapshot's 463-test non-Games suite had two stale-expiry Commerce failures and two Connectivity Registry failures from expired fixture credentials. This branch updates only those time-dependent test fixtures; the same 463 non-Games tests now pass, with no production VPN behavior changed.

## Current state and first decision

Verified on November 18, 2026:

- GitHub's default branch is `main` at `1fd6964`. It has no `aurix_games/`, `web/game-store/` or `config/games/` tree. The local theme branch and `main` have diverged substantially. The local theme also has uncommitted work.
- `main` and the current Games theme branch have no common Git ancestor. The local branch contains unrelated AI, VPN, bot and campaign commits, so it cannot be pushed as a Games baseline. Import requires a reviewed snapshot on a short-lived `main`-based branch; the theme's uncommitted UI edits must stay out of that snapshot.
- Singapore's bot updater is configured to follow the existing `codex/aurix-vpn-portal` branch. Its active release is `693af7a` under `/opt/aurix-releases/`. An earlier assessment assumed its default `main` setting was the live setting. The systemd override sets a different branch.
- Games is running from `/opt/aurix-games/current`, pointing to `20261117-admin-catalog-review-clarity-1`. Exactly one unpacked release was present and the root filesystem had 5.1 GB available. Catalog, dispatch, fulfillment, reconciliation, integrity and backup units also use that Games symlink. The release directory name alone does not establish its Git SHA.
- Read-only host check on Nov 18, 2026: `aurix-games.service` and `aurix-games-edge.service` were active; `/api/healthz` and `/api/readyz` returned 200 and readiness reported all checks true. The six Games timers were active, and no systemd jobs were running at observation. The shared `/opt/aurix-venv` versions of `cryptography`, `cffi`, and `pycparser` match the Games lock.
- Existing encrypted `.agb` backups are data backups under `/var/backups/aurix-games`, on the same host. They do not preserve deployable source packages and are not the off-server code rollback requested here.
- Read-only source comparison of the active release against the candidate found 255 remote source files: 236 identical, 19 changed, and none remote-only. The candidate has 146 additional local files. The active release has no proven Git SHA. Most changed storefront/backend files in the candidate are newer than the host copy; however, the host `migrations.py` is 2,192 lines while the clean `main` baseline is 1,694 lines. The candidate intentionally keeps `main`'s shared migration module; its isolated catalog migration tests pass, but compatibility against a production database restore has not been demonstrated.
- Read-only migration metadata check found one registry row: `catalog-policy.sqlite3` is at `games_catalog_policy:1` / `versioned_catalog_merchandising_policy`, matching the candidate. The other eight Games SQLite files have no `schema_migrations` table. This confirms version/name compatibility without reading customer or payment rows; a restore rehearsal is still required before deployment.
- The candidate includes the approved stale-rate behavior: customer catalogs can retain the last approved rate, the admin view shows its observation time and warning, and checkout still requires current pricing approval. Existing backend and frontend tests cover these boundaries. This is source state only and has not been deployed.
- GitHub reports `minthanthtoo/aurix-telegram-bot` is public. GitHub documents that repository readers can download workflow artifacts, so the package workflow stays fail-closed instead of exposing its bundle without an explicit storage/visibility decision. [GitHub artifact access](https://docs.github.com/en/rest/actions/artifacts)
- `main`'s current CI runs broad Python discovery on branch pushes and PRs. Importing Games as-is would add Games tests to those broad runs. A separate Games workflow alone would duplicate that work.

**Recommendation** — Use separate workflows in the existing repo. The production bot already has a separate source ref and release tree. Separate repos become useful if Games needs independent GitHub access or visibility. They would require moving or publishing shared modules and managing version compatibility across repos. A permanent Games release branch would add another branch to synchronize.

## Roll out in three steps

| Step | Deliverable | Gate to proceed |
|---|---|---|
| 1. Source and CI | Reviewed Games baseline in `main` and scoped checks with no production access | Games-only and shared changes route correctly. A push and a merge leave the Games and Bot release pointers as observed, apart from independently approved releases. |
| 2. Operator deploy | GitHub builds a package on demand. An operator sends that package through the Games installer using existing authorized SSH. | The installer passes the interruption and rollback rehearsal. The first production cutover verifies the exact served revision, healthy workers and one installed release. |
| 3. GitHub deploy | A manual Actions run invokes the same installer with a Games-only identity | Repository permissions can scope production access to the trusted workflow. The operator route remains available for recovery. |

Use operator SSH for the first production cutover. The same bundle and installer serve both routes, so the GitHub deploy identity can be added after the installer is proven. If GitHub permissions cannot safely restrict the deploy identity, keep operator deployment. Packaging and CI run on GitHub runners, with no Games runner or new polling job on Singapore.

## 1. Reconcile the baseline before publishing

- [x] Inventory the active Games release and compare its source files with the local candidate. The read-only comparison found 236 exact matches, 19 differing overlaps, no remote-only source files, and no verified active Git SHA.
- [ ] Resolve the 19 overlaps file by file and prove the clean `main` migration module against a restore of production data. Keep the paused, uncommitted theme work out of this source branch. Treat the server as evidence of what is running and record the chosen source for every remaining difference.
- [ ] Save a verified copy of the current runnable Games files in approved private off-server storage. Record its digest and runtime requirements under a legacy release ID if the source SHA cannot be proven. Keep databases, payment evidence, credentials and live configuration in their existing recovery systems.
- [ ] Inspect GitHub repository visibility, required checks, write access and available environment controls. Inventory every automated path that can deploy from `main` or write to the Games release tree, including GitHub workflows, systemd and any external deployment hooks. If an unrelated service follows `main`, establish its explicit deployment gate before the Games import can trigger it.
- [ ] Prepare a scoped baseline patch against current `main`. Preserve `main`'s unrelated code and compare shared imports before selecting any version. Keep the paused theme changes out of this import. Review the complete set of files and commits the push would expose, including assets and config. Reuse an existing branch only if its outbound history is within that scope. If divergent history makes that impractical, announce the purpose and name of a single short-lived import branch. Merge the baseline and CI routing together so broad CI does not absorb Games tests.

## 2. Separate GitHub CI by actual dependencies

| Change | Games checks | Existing shared and product checks |
|---|---|---|
| Games runtime, storefront, Games config, owned supplier scripts and tests, Games workflow or installer | Run | Lightweight diff and repository checks |
| Telegram bot, VPN, AI or fleet files outside the Games dependency list | Skip | Keep the existing validation |
| Shared files used by Games, including `commerce_models.py`, `persistence.py`, `migrations.py`, `telegram_web_app.py`, dependency and lock files | Run | Run |
| Docs only | Skip | Lightweight diff and repository checks |
| Mixed Games and other product changes | Run | Run |
| Unknown path or unavailable comparison base | Run | Run |

- [x] Add `.github/workflows/games-ci.yml` for PRs to `main`, pushes to `main` and manual validation. It uses `scripts/run_game_store_checks.py`, pinned Python/Node versions, hash-locked Games dependencies, and no production secrets.
- [ ] Keep one explicit Games input list shared by CI routing and the installer/package review. Start with the observed runtime directories, the shared imports above, and the required scripts under `scripts/`. Distinguish runtime files from CI-only input files. Account for indirect imports, tools and fixture paths. Treat any future change to that list or to the classifier as shared.
- [x] Keep the existing `safety-net` status name. A cheap path-scope job gates its expensive steps; the non-Games runner excludes Games tests before import. Shared and mixed changes run both suites. This change is only local and is not active on `main` or the live bot branch.
- [ ] Classify the complete change using Git, including both paths on renames. Use the PR merge base for PRs and the before/after commit range for pushes. If either revision cannot be resolved, run both suites. Keep required status jobs reporting on skipped paths and distinguish a successful test job from a skipped job when assessing deploy eligibility. GitHub's workflow-level path filters have diff and required-check limitations. [GitHub workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onpushpull_requestpull_request_targetpathspaths-ignore)

## 3. Promote a Games package explicitly

| Action | Result for Games production |
|---|---|
| Local edit or commit on an existing branch | Local preview |
| Push a topic branch or open a PR | Source and CI only |
| Merge to `main` | Source eligible for promotion after Games CI passes |
| Manually promote a validated package | Install that exact package through operator SSH or the restricted **Games Production** workflow |
| Manually request rollback | Install a previously healthy package by immutable identifier through the same installer |

- [ ] Add `.github/workflows/games-production.yml` with only `workflow_dispatch`. Run its definition from `main`. Start with a package action that accepts an exact candidate SHA. Verify that the SHA is reachable from `main` and the named Games test job actually succeeded on that exact commit from a trusted main-branch run. A skipped or unrelated check cannot satisfy this gate. Add deploy and rollback actions after the operator rehearsal. Both accept an immutable release ID and the expected deployment ID. After the legacy baseline, require normal deploys to advance the active Git revision. Give every activation a new deployment ID, including rollbacks, so a queued request becomes stale even if production returns to the same SHA. Enforce the expected deployment ID under the host lock. A retry can return success only when the same operation already activated the requested digest. [GitHub manual workflow trigger](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch)
- [ ] Reuse `scripts/package_game_store.py` on a clean checkout of the approved commit. Constrain inputs to tracked, approved runtime paths and record the commit SHA, archive checksum, file inventory, dependency lock digest, target runtime and expanded bytes. Verify the archive contains the static assets and modules needed to boot. Build once on a GitHub runner. Record the bundle digest and GitHub run ID in a release record and verify both against the trusted packaging run before installing. Store the verified bundle and its runtime dependencies in a private GitHub release if repository visibility permits, or use the approved private artifact store. Workflow artifacts expire and serve only as a transfer mechanism. Use the same stored bundle for deploy, retry and recovery. [GitHub Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases) · [Workflow artifact retention](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts)
- [ ] Use an operator's established SSH access at first. For automated promotion, use a Games deployment identity with a pinned SSH host key and a fixed server command that invokes the Games installer. The installer and systemd unit definitions live outside the release payload. Give only the production job that identity and reserve package publishing permissions for the trusted workflow. Pin Actions dependencies to full commit SHAs and protect workflow and installer changes under the repository's review rules. Keep PR validation free of production credentials. [GitHub environment support](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments) · [GitHub Actions security guidance](https://docs.github.com/en/actions/reference/security/secure-use)

## 4. Install and recover with one release at rest

- [ ] Create `deploy/install_aurix_games.py` after the baseline and off-server rollback storage are chosen. The live service uses `/opt/aurix-venv/bin/python`, the `aurix-games` account, `/opt/aurix-games/current`, and six systemd timers; a correct installer must preserve these host-owned units and states.
- [ ] Record the previous and candidate paths, expected deployment ID, and current Games service and timer states before any switch. Extract one temporary candidate on the same filesystem. Pause Games timer launches and allow active jobs, including supplier dispatch, to finish within a bounded wait. If they cannot drain, abort before switching. Preserve pending orders and reconciliation state in the existing data directories.
- [ ] Switch the symlink atomically and restart the Games web service. Check systemd, the candidate SHA from an uncached read-only build marker, the public storefront and the versioned assets its HTML references. Use the existing domain probe and `/api/healthz` for reachability. Record `/api/readyz` before and after so a pre-existing supplier or catalog block stays visible without being confused with an installer regression. A healthy HTTP response alone cannot pass activation. Reenable only the Games workers and timers that were active before cutover and confirm their expected state.
- [ ] Run the critical switch as a host-owned operation so an SSH disconnect or Actions cancellation cannot abandon it mid-step. Write a durable phase record before switching or cleaning up. On restart, reconcile the installed symlink, phase and service states under the same lock. Restore the prior release and Games timer state if the candidate was never verified. Make recovery run before Games workers can resume an unfinished activation after a reboot. If that recovery fails, retain both managed trees and the recovery record, and leave affected jobs paused for diagnosis.
- [ ] Limit routine releases to schema changes the previous code can still read and write. Rehearse them against an isolated restore using the existing Games backup tooling. Code rollback restores the app version while keeping current order, wallet and receipt state. Use a separate maintenance and reconciliation plan for any data migration that cannot be rolled back that way.
- [ ] After success or recovery, leave one unpacked version under `/opt/aurix-games/releases` and remove transient deploy files only after verifying the old release is recoverable from approved off-server storage. Keep the audit record outside that tree with the active SHA, digest, previous release ID, free space and health result. A temporary candidate permits rollback during cutover.

## 5. Keep one rollback version

| Location | Steady state |
|---|---|
| Singapore | One active, unpacked Games release. No previous release directories or deploy archives. |
| Private off-server storage | The exact active package for rebuilding a lost host, plus one previous healthy package for rollback when available. |
| GitHub Actions | Run results and release metadata. Use temporary build artifacts for transfers. Git commits and tags can remain after the old binary package is pruned. |

- [ ] Read back the active and candidate packages from durable storage and verify their digests before each cutover. If the active package is missing, recover the exact active package before switching. Use the verified candidate bytes for installation without another download. Allow one temporary candidate for the cutover.
- [ ] After the installer reports a healthy candidate, verify the served revision and confirm that both verified package references still exist. Mark the old active package as the sole rollback target. Only then prune the older package payload. A preflight or activation failure leaves the previous active and rollback packages in place.
- [ ] Add a manual rollback action to the same workflow. It reads the latest confirmed deployment record under the same deployment lock, fetches the designated fallback by digest and runs the same installer without rebuilding or fetching a moving branch. If Actions is unavailable but storage is reachable, the operator can download the same package and use SSH. On success, mark the failed release ineligible as a fallback. After consuming the sole healthy fallback, there may be no second ready-to-install rollback until the next healthy deployment.
- [ ] Run cleanup only against explicitly managed Games packages. Coordinate pruning with deploy and rollback using the expected deployment ID. If storage or deployment status cannot be verified, defer deletion and retry the same cleanup later. Keep payment, data and configuration backups on their own retention policy.

## Cutover gates

- [ ] Rehearse Games-only, non-Games, shared, mixed, rename and missing-base routing. Confirm a Games-only PR runs Games CI once and skips the unrelated heavy checks. Confirm skipped, failed and mismatched SHA results cannot deploy.
- [ ] Rehearse successful activation, low disk, invalid archive, duplicate requests, stale targets, failed health and interruption in an isolated environment. Verify that one release remains after recovery.
- [ ] Rehearse lost SSH, reboot after symlink switch, rollback to the same SHA as an earlier deployment, and a worker that cannot drain. Verify that stale requests are rejected, only the recorded workers resume, and no supplier order is submitted twice in the rehearsal.
- [ ] Capture Games and bot active release IDs before the first push. Verify that a baseline push or merge and a passing Games CI run leave production pointers unchanged in the absence of a separate service's own approved deploy.
- [ ] Build the package through the manual workflow and check that its rollback package is independently retrievable by digest. Use operator SSH for the first controlled deploy. Verify the public SHA, worker state, disk usage, one installed Games release, and the bot release pointer.
- [ ] Rehearse a successful promotion, a failed candidate and a rollback after cleanup. In each case verify the retained package IDs, including that failed builds never replace the sole healthy fallback and a storage read failure defers pruning.
- [ ] Record actual CI duration, bundle size and minimum disk space during cutover. Keep these numbers and the active deployment ID in each release summary. Alert through existing host monitoring if an unfinished deployment persists, the steady-state Games release count exceeds one, or free space falls below the measured preflight requirement.

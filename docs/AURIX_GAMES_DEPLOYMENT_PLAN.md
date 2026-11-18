# AuriX Games CI/CD

## Design

Games has its own checks and release path inside the existing GitHub repository. Normal local commits, pushes, merges, and CI runs never deploy. Production changes require a manual `Games production` workflow run from `main`, the explicit confirmation input, the `GAMES_DEPLOYMENT_ENABLED=true` repository variable, and approval by the protected `games-production` environment.

The workflow builds an immutable code-only archive from the chosen full commit SHA, verifies a successful `Games tests` and `Games check` run on that exact main commit, then transfers the exact GitHub Release asset over pinned SSH. It keeps only the active and previous healthy Games code packages as unpublished GitHub draft releases. Customer records, database files, runtime configuration, payment evidence, and credentials are excluded from release bundles.

On Singapore, the root-owned helper stages one temporary candidate, pauses and drains only the six Games workers, swaps `/opt/aurix-games/current`, checks local health/version and the public storefront, restores the Games timers that were active before deployment, records the active/rollback IDs, and removes old expanded releases. Failed or interrupted switches restore the previous pointer and saved state; boot recovery runs before Games services can start. The app’s existing data, payment configuration, systemd units, and other services remain outside the code release.

The Games dependency lock is included and checked against the server runtime. A deploy is rejected if a normal release changes the lock or if locked packages are missing/different. Dependency upgrades need a separate runtime maintenance and rollback rehearsal; this pipeline does not mutate the shared Python environment.

## One-time activation

Use the existing Games import PR and the following activation sequence. Keep the repository deploy switch unset until preflight passes.

1. Review and merge the Games source/CI/CD change into the repository’s `main`. Keep the existing bot deployment ref and workflow separate.
2. Protect `main` with reviewed pull requests and required CI checks. Use the `Games check` aggregate for Games scope and the existing `safety-net` aggregate for the rest of the repository; confirm their exact check names in Actions before saving the ruleset.
3. Create the GitHub environment `games-production`; restrict deployments to `main` and require an authorized owner to approve. Turn on prevention of self-review when a second authorized reviewer is available.
4. Add `GAMES_DEPLOY_HOST` and `GAMES_DEPLOY_PORT` (`22` if standard SSH) as repository or environment variables.
5. Generate a dedicated Ed25519 keypair for Games deploys. Run `sudo deploy/bootstrap_aurix_games_deployer.sh /path/to/public-key` on Singapore. It installs one forced-command account, narrowly scoped sudo, and boot recovery ordering. It does not restart the Games application. When the root-owned deploy helper or bootstrap changes later, rerun the reviewed bootstrap before dispatching the workflow so the server helper matches the code being used.
6. Add environment secrets `GAMES_DEPLOY_PRIVATE_KEY` and `GAMES_DEPLOY_KNOWN_HOSTS`. Use the server host key from an already trusted source and verify its fingerprint before pinning it; do not trust an unverified `ssh-keyscan` result. For a nonstandard SSH port, the known-hosts entry must use OpenSSH’s `[host]:port` form.
7. Run `Games production` with `preflight` while the deploy switch is off. Confirm runner access and verify that the production baseline and tested candidate can both be restored. Confirm runtime versions, a fresh recoverable data backup, and compatibility with the current database schema.
8. Set the repository variable `GAMES_DEPLOYMENT_ENABLED=true`. During the staffed window, run `Games production` from `main`, choose `deploy`, enter a full main commit SHA with passing Games CI, and confirm production. On first use, the workflow snapshots the active source into the rollback release before switching.

## Routine use

- **Deploy:** dispatch from `main`, choose `deploy`, provide the exact full SHA, confirm, then approve the environment review. The selected commit must be a descendant of the active Games commit. Treat the release drafts as private recovery assets and leave them unpublished. A failed run can leave an unused draft that a later successful run will prune after confirming both release IDs.
- **Rollback:** dispatch from `main`, choose `rollback`, confirm, and approve. The workflow uses the retained package and checks the expected active release so stale requests cannot switch the server.
- A push or merge alone never changes Games production. A failed test, missing package, stale deployment, bad host key, dependency mismatch, unhealthy candidate, or failed recovery stops the operation without pruning the healthy release.

## Verification boundary

Local tests can exercise packaging, archive validation, SSH framing, first install, timer restoration, rollback, and release markers. They cannot prove the GitHub environment approval, GitHub secrets, SSH host-key pin, or a real systemd cutover. Those require the one-time activation and an approved first production run. Confirm the GitHub Actions summary, local `/api/version`, public storefront and catalog, the sign-in entry points, and restored Games timers. Record a rollback and redeploy rehearsal and verify that exactly one expanded release remains. Keep data backups separate from the code rollback packages.

# AuriX backup and restore runbook

`scripts/aurix_backup.py` creates an atomic backup artifact and a JSON manifest
with SHA-256, SQLite integrity/foreign-key results, logical table counts, and
the private receipt-object paths referenced by `payment_evidence`. It does not
print database URLs, access URLs, or encryption keys.

## SQLite scheduled backup

Run this from the application release directory while the normal single writer
is healthy:

```sh
python deploy/../scripts/aurix_backup.py sqlite-backup \
  --source /var/data/bot.db \
  --output-dir /var/backups/aurix
```

The backup file and its `.manifest.json` are written with restrictive
permissions. Copy them to encrypted, access-controlled storage and retain the
manifest with the artifact. Keep `AURIX_ACCESS_URL_KEY` in a separate secret
backup; it is intentionally never included in the database artifact.

Verify an artifact without opening it for writes:

```sh
python deploy/../scripts/aurix_backup.py sqlite-verify \
  --backup /var/backups/aurix/aurix-sqlite-20260910T000000Z.db \
  --receipt-inventory /var/backups/aurix/receipt-objects.txt
```

The inventory may be newline-delimited paths or JSON (`["path"]` or
`{"paths": ["path"]}`). Missing database-referenced receipt paths fail the
verification; extra objects are reported for retention cleanup but do not fail
the restore.

## Isolated SQLite restore drill

Restore only to a new, stopped, non-production path unless an operator has
explicitly chosen `--overwrite`:

```sh
python deploy/../scripts/aurix_backup.py sqlite-restore \
  --backup /var/backups/aurix/aurix-sqlite-20260910T000000Z.db \
  --target /var/tmp/aurix-restore/bot.db \
  --receipt-inventory /var/backups/aurix/receipt-objects.txt
```

The command rechecks integrity, foreign keys, required control-plane tables,
logical counts, and receipt paths. After a successful drill, run the application
health check, maintenance heartbeat check, one disposable claim/revoke, and
one disposable paid workflow against the restored copy before considering the
restore usable.

## PostgreSQL archive

Create a custom-format archive from a secret environment variable and verify
that `pg_restore` can list it:

```sh
python deploy/../scripts/aurix_backup.py postgres-backup \
  --database-url-env COMMERCE_DATABASE_URL \
  --output /var/backups/aurix/aurix-postgres-20260910.dump

python deploy/../scripts/aurix_backup.py postgres-verify \
  --archive /var/backups/aurix/aurix-postgres-20260910.dump
```

An actual PostgreSQL restore must target a fresh isolated database and be
performed by an operator with the provider's backup permissions. The script
does not run a destructive `pg_restore` against a live target. After restoring,
run the schema contract, migration history, order/wallet/subscription/key/job
count checks, and Supabase receipt-object reconciliation.

## Operating targets

- RPO target for the single-writer SQLite pilot: one scheduled backup interval;
  alert if the latest verified manifest is older than that interval plus one
  grace period.
- RTO target: record backup-copy time plus isolated restore time during every
  restore drill; do not publish a target until it is measured twice.
- Retention: keep daily artifacts for the agreed business-retention period and
  at least one known-good pre-migration artifact. Delete artifacts only through
  the chosen encrypted storage's retention policy.
- Rollback: stop the writer, restore only a schema-compatible artifact, keep
  the previous source release available, and reconcile remote provider keys
  separately because a database restore cannot recreate an externally deleted
  credential.

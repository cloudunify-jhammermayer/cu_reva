# Odoo.sh backups — technical

The consultant's part is the Backups page on the REVA site (/reviews/backups, source api/app/static/backups.html).
This page covers what tech does and how the system works. The full operational
docs live on the backup server itself in `/root/docs/`.

## Architecture

```
Odoo.sh project (per client)        backup server (Hetzner VPS "backups")     Hetzner storage box
~/backup.daily/<db>_daily.sql.gz ──ssh──► /var/lib/backups/<client>/ ──restic──► sftp:hetzner-backup:<client>
~/backup.daily/<db>_daily/       ──ssh──► (staging, 2 days)                      encrypted, deduplicated,
                                                                                  7d / 4w / 6m retention
/etc of the VPS                  ───────► profile "default"             ──restic──► sftp:hetzner-backup:restic-repo
```

- One resticprofile profile per client in `/etc/resticprofile/profiles.yaml`,
  plus `default` for the server's own `/etc`.
- Each client has its own restic repository and its own encryption password
  (`/var/lib/restic/passwords/<client>`). Every password is also in the
  password manager. Without it a repository is unreadable.
- Odoo.sh writes the daily dump and filestore into `~/backup.daily/` on the
  production build. The filename carries the build id and is the same every
  day, so the pull always downloads instead of trusting an existing file.

## Access to Odoo.sh

SSH into a project works only when both are set in the project's dashboard:

1. the Odoo.sh user **CU-odoo-sh-backup** is a collaborator with access to the
   production branch. Its profile holds the server's public key
   (`/root/.ssh/odoo_sh_pull.pub`);
2. the server IP **178.105.100.225** is allowed for SSH in the project
   firewall. Odoo.sh drops SSH from any other IP. Changes take up to 5 minutes.

SSH user is the production build id, host is `<project>.odoo.com`. Both are
stored as alias `odoo-<client>` in `/root/.ssh/config` on the server.

## Onboarding a client

Prerequisites from the consultant: client short name, domain, SSH command,
firewall and collaborator done.

```bash
ssh root@178.105.100.225          # key: the work key
backup-add-client.sh <client>     # lowercase, digits, hyphens
```

The script, in order:

1. prints the collaborator and IP reminder, asks for Odoo.sh host and user,
   writes the SSH alias;
2. retries SSH for up to 5 minutes and checks that `~/backup.daily/` holds a
   daily dump. Stops with the reason if not, nothing else is written;
3. creates staging dir, encryption password and restic repository, appends
   the profile, registers the 00:00 UTC timer;
4. runs a full backup immediately and prints the snapshot.

"Done" means a snapshot exists. Then store the password in the password
manager and report the snapshot in the ticket. Re-running for an existing
client is safe. To change host or user, edit the alias block in
`/root/.ssh/config` and re-run.

## The nightly run

All profile timers fire at 00:00 UTC. Per client:

1. `pull-odoo-backup.sh`: SSH test, download dump and filestore tarball to a
   `.tmp` file, rename when complete. Any error aborts the run.
2. `check-backup-fresh.sh`: abort if no staged file is newer than 26 h.
3. `restic backup` into the client repository.
4. Retention 7d / 4w / 6m, prune.
5. `restic check` on 10 % of the data.
6. Staged files older than 2 days are deleted, only after success.

## Alerting

`backup-summary.timer` runs at 00:45 UTC and posts one Google Chat message
with a line per profile: green with snapshot time, red with the pull or check
error from the client log, hourglass if still running. The header is green only
when all lines are. No message at 00:45 means the server or the timer is down.
There is no dead-man's switch yet.

`backup-summary.sh --dry-run` prints the message without posting.

## Troubleshooting

| In `/var/log/resticprofile/<client>.log` | Cause | Fix |
|---|---|---|
| `connect to host <x>.odoo.com port 22: Connection timed out` | IP not in the project firewall | consultant re-adds 178.105.100.225, wait 5 min, `backup-run.sh <client>` |
| `Permission denied (publickey)` | CU-odoo-sh-backup no longer a collaborator, or build id changed | re-add collaborator; compare the SSH command in Odoo.sh with `/root/.ssh/config` |
| `found no daily backup in ~/backup.daily/` | daily backups off on the production branch | enable in Odoo.sh |
| `No fresh backup file ... nothing newer than 26h` | pull produced nothing new | read the pull lines above it |
| `repository is already locked` | a run died mid-way | `restic unlock` on that repo once nothing is running |

Useful on the server: `backup-overview.sh`, `backup-snapshots.sh [client]`,
`backup-run.sh <client>`, `pull-odoo-backup.sh <client>`,
`systemctl list-timers 'resticprofile-backup@*' backup-summary.timer`.

## Removing a client

```bash
remove-backup-client.sh <client>
```

Removes timer, profile, SSH alias and staging data, then asks whether to keep
the repository (snapshots stay frozen, password must be kept) or `delete` it.
Afterwards the consultant removes CU-odoo-sh-backup and the IP from the Odoo.sh
project.

## Restoring

```bash
restore-client.sh <client>
```

Lists the snapshots, asks which one, restores to
`/tmp/restore-<client>-<id>/`. The result is the `.sql.gz` dump and the
filestore `.tar.gz`, ready for `psql` and `tar`. Details in
`/root/docs/restore-client.md` on the server. Delete the restore directory
afterwards, the disk is small.

# Odoo.sh backups — what the consultant does

Every night we pull a copy of each customer's Odoo.sh production (database and
filestore) to our own backup server and store it encrypted on a Hetzner
storage box. Retention: 7 daily, 4 weekly, 6 monthly snapshots. It is
independent of Odoo.sh's own backups, so a customer can get data back from us
even when the Odoo.sh project is gone.

For that to work, the backup server needs SSH access to the customer's Odoo.sh
project. Setting that up is the consultant's part. Everything else is done by
tech.

## Your three steps

All three happen in the customer's Odoo.sh project. A project admin is needed,
which is usually us. If the customer administers the project, send them the
steps.

### 1. Give us the domain

Send tech the project's Odoo.sh domain and the SSH command of the production
branch:

- Domain: `<project>.odoo.com`
- SSH command: open the production branch in Odoo.sh, copy the command shown
  under "Shell" or "SSH". It looks like `ssh 12345678@<project>.odoo.com`.

The number is the production build id. Tech needs both the number and the host.

### 2. Allow the IP

Odoo.sh only accepts SSH from IPs that are allowed in the project's firewall.

- Odoo.sh project, Settings, Firewall: add **178.105.100.225** for SSH.
- Takes up to 5 minutes to apply.

### 3. Put in the user

- Odoo.sh project, Settings, Collaborators: add the Odoo.sh user
  **CU-odoo-sh-backup** with access to the production branch.

This user belongs to us. Its profile holds the backup server's SSH key, so no
key has to be exchanged per customer.

## Hand over to tech

Open a ticket for tech with:

- client short name (lowercase, used as the backup name, e.g. `pomberger`),
- the domain and the SSH command from step 1,
- confirmation that steps 2 and 3 are done.

Tech runs the onboarding on the backup server. It verifies the access, sets up
the encrypted repository and does a first full backup right away. The client is
onboarded when tech reports the first snapshot.

## How you know it keeps working

Every night at 00:45 UTC one message arrives in the backup Google Chat space,
one line per client:

- green: snapshot of tonight exists,
- red: something failed, with the reason.

A red line, or no message at all, goes to tech. The most common red reason is
that the IP was removed from the firewall or the collaborator was removed from
the project. Both are fixed by repeating steps 2 and 3.

## When a client leaves

Tell tech. They stop the nightly backup and ask you one question: keep the
existing snapshots for a retention period, or delete them now. Then remove
CU-odoo-sh-backup from the collaborators and the IP from the firewall in the
Odoo.sh project.

## Restoring data

Ask tech. They can restore any snapshot of the last 6 months as a database
dump plus filestore. See the technical page for what that involves.

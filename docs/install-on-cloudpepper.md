# OmniaCLI on a server already managed by Cloudpepper

Analysis of 19/11/2026 on `95.179.220.4` (my.cloudpepper.io server
`018dcc60-…`), read only: nothing was changed there.

## What is there

- Ubuntu 22.04.4, 1 CPU, 951 MB RAM (430 free) + 2.3 GB swap (760 MB used),
  disk 23 GB, 7.2 GB free; up 120 days; `cloudpepper-agent` active.
- One instance, `spare-parts.odoo4u.cloud`:
  - Odoo **15.0 from the Debian package** (`/usr/bin/odoo`, 15.0.20240403,
    the system `python3` 3.10, `/usr/lib/python3/dist-packages/odoo`), no
    source checkout;
  - systemd unit `odona-spare-parts.odoo4u.cloud`, user `odoo`,
    `WorkingDirectory=/var/odoo/spare-parts.odoo4u.cloud`, ports 3000/3001,
    workers 2, `proxy_mode`, `list_db = False`;
  - database `spare-parts.odoo4u.cloud` (262 MB, PostgreSQL by socket,
    user `odoo`), filestore 59 MB in `/var/lib/odoo/.local/share/Odoo`;
  - addons: `extra-addons/{enterprise,megaplastic,server-tools,web}.git-<id>`,
    **copies, not git repositories** (Cloudpepper deploys them);
  - log `logs/odoo-server.log`, 100 MB, not rotated;
  - `healthcheck.sh` (root): an XML-RPC `version` call on port 3000.
- Permissions: `/var/odoo` 744 and the instance folder 700, both `odoo`:
  no other user can enter them. `odoo.conf` is `root:root 644`.
- Not there: `click_odoo` for the system python, `setfacl` (package `acl`).
- Firewall `ufw`: only 22, 80, 443 open. Odoo listens on `0.0.0.0:3000`, not
  reachable from outside because of ufw.

## What OmniaCLI can do on it

| Action | | Why |
|---|---|---|
| State, log, CPU/RAM load | yes | systemd unit, log readable after step 2 |
| Restart, Start, Stop | yes | sudoers lines of step 4 |
| Backup, download | yes | click-odoo-backupdb as `odoo` (step 3) |
| Update modules (`-u`) | yes | `/usr/bin/odoo -u … --stop-after-init` as `odoo` |
| Log in as | yes | database by socket as `odoo` |
| Pull code | **no** | no git repositories: the code stays Cloudpepper's |
| Copy production to test, staging | **no** | one instance, not Docker |
| Restore, Copy from link | yes | the Odoo version is read from the package's `odoo/release.py` (`odoo_root`): 15, checked on the server |

## Steps (as root, to approve one by one)

1. **The package**: copy `omniacli_<version>_all.deb` of the fork, then
   `apt install ./omniacli_<version>_all.deb`. It creates the user
   `omniacli`, `/opt/omniacli/venv` (needs PyPI), the service on
   `127.0.0.1:8090`, `/etc/omniacli`, `/var/lib/omniacli`. About 100 MB of
   RAM.
2. **Read the config and the log**, without changing the folders' modes:
   ```bash
   apt install acl
   setfacl -m u:omniacli:x /var/odoo
   setfacl -m u:omniacli:rx /var/odoo/spare-parts.odoo4u.cloud /var/odoo/spare-parts.odoo4u.cloud/logs
   sudo -u omniacli head -c 1 /var/odoo/spare-parts.odoo4u.cloud/odoo.conf; echo " $?"   # 0
   ```
   Cloudpepper may reset these modes on a deploy: Test & activate says so.
3. **click-odoo without touching the system python**: a venv of its own that
   sees the packaged Odoo:
   ```bash
   python3 -m venv --system-site-packages /opt/omniacli-odoo15
   /opt/omniacli-odoo15/bin/pip install click-odoo-contrib
   /opt/omniacli-odoo15/bin/python -c "import click_odoo, odoo; print(odoo.release.version)"
   ```
   The instance gets `venv_bin: /opt/omniacli-odoo15/bin`, `run_as: odoo`,
   `odoo_bin: /usr/bin/odoo`. The unit keeps running `/usr/bin/odoo`.
4. **sudoers** (`/etc/sudoers.d/omniacli`, then `visudo -cf`):
   ```
   omniacli ALL=(odoo) NOPASSWD:SETENV: /opt/omniacli-odoo15/bin/
   omniacli ALL=(root) NOPASSWD: /usr/bin/systemctl --no-ask-password restart odona-spare-parts.odoo4u.cloud
   omniacli ALL=(root) NOPASSWD: /usr/bin/systemctl --no-ask-password start odona-spare-parts.odoo4u.cloud
   omniacli ALL=(root) NOPASSWD: /usr/bin/systemctl --no-ask-password stop odona-spare-parts.odoo4u.cloud
   ```
5. **The link from Carlo's OmniaCLI**, SSH only (no port opened): the user
   `omniacli-link` with the restricted key the Links page prints
   (`permitopen="127.0.0.1:8090"`, `command="/bin/false"`) and the
   `Match User` block, then `sshd -t && systemctl reload ssh`.
6. **Discover** on that OmniaCLI → draft `spare-parts`, production, with no
   repositories → **Test & activate**.

## Undo

```bash
apt purge omniacli                       # service, user, config, jobs (backups kept in /var/lib/omniacli/backups)
rm -f /etc/sudoers.d/omniacli
setfacl -x u:omniacli /var/odoo /var/odoo/spare-parts.odoo4u.cloud /var/odoo/spare-parts.odoo4u.cloud/logs
userdel -r omniacli-link                 # and the Match User block of sshd_config
# /opt/omniacli-odoo15 and /var/lib/omniacli/backups: removed by hand
```

## Open points

- RAM: 1 GB with swap already in use; a backup (`pg_dump` + zip) adds load.
- Two managers: Cloudpepper keeps deploying code and may restart Odoo; OmniaCLI
  must not pull. Who updates modules after a Cloudpepper deploy?
- The Odoo version of a package install: done, `odoo_root()` finds the
  package's `odoo/` from the script's python (`/usr/lib/python3/dist-packages`
  here), and Discover says it is a package and how to give click-odoo a venv.

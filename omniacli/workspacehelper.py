# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The root helper of the workspaces: their disks and their instances.

    omniacli workspace-helper disk <workspace> <storage> <size in GB>
    omniacli workspace-helper create <workspace> <storage> <instance> <template> <values as JSON>
    omniacli workspace-helper remove <workspace> <storage> <instance>
    omniacli workspace-helper drop <workspace> <storage>
    omniacli workspace-helper limits <workspace> <cpus> <memory in GB>
    omniacli workspace-helper upstream <workspace> <storage> <instance> <role> <http port>|- <websocket port>|-
    omniacli workspace-helper login-path check | add | remove | sync <host or all>
    omniacli workspace-helper login-path keep on | off
    omniacli workspace-helper portal <host or -> | portal-names
    omniacli workspace-helper pitr-enable | pitr-base | pitr-state <workspace> <storage> <instance> <role>
    omniacli workspace-helper pitr-restore <workspace> <storage> <source> <target> <time> <source db> <target db>
    omniacli workspace-helper machine-db-archive-state | machine-db-archive-off | machine-db-archive-base <major>
    omniacli workspace-helper machine-db-archive-on <major> <budget in GB> <bases kept> <GB left free> restart|no-restart
    omniacli workspace-helper machine-db-recover <workspace> <storage> <source> <target> <time>
    omniacli workspace-helper machine-db-recover-sweep
    omniacli workspace-helper machine-db-replica-facts | -state | -drop | -replaced <major>
    omniacli workspace-helper machine-db-replica-allow | -ready <major>   (their values, JSON, on the input)
    omniacli workspace-helper machine-db-replica-cap <major> <GB>
    omniacli workspace-helper machine-db-replica-read <workspace> <storage> <instance> on|off
    omniacli workspace-helper machine-db-replica-follow <workspace> <storage> <instance>
    omniacli workspace-helper machine-db-replica-make <major> prepare|copy|start   (prepare: its values on the input)
    omniacli workspace-helper machine-db-replica-hosted | -promote | -remove <major>
    omniacli workspace-helper machine-db-replica-sync <major>   (its values, JSON, on the input)
    omniacli workspace-helper machine-db-replica-return <major> facts|allow|state|stop|resume|done|cancel
    omniacli workspace-helper machine-db-return <major> facts|prepare|copy|start|state|wait|promote|note|purge|cancel|forget
    omniacli workspace-helper machine-db-replica-back <workspace> <storage> <instance>
    omniacli workspace-helper machine-db-replica-grant <major> <role> revoke|allow|drop
    omniacli workspace-helper machine-db-replica-lent <major>   (the primary's grants, JSON, on the input)
    omniacli workspace-helper machine-db-replica-unlisten <workspace>.<storage>.<instance>
    (machine-db-return takes <workspace>.<storage>.<instance> too: the database of that instance comes back)
    omniacli workspace-helper machine-db-moved <workspace> <storage> <instance>   (where it is now, JSON, on the input)
    omniacli workspace-helper space
    omniacli workspace-helper ssh-keys | ssh-key-add <account> | ssh-key-remove <account> <SHA256:...>
    omniacli workspace-helper os-updates | os-updates-set on|off never|<HH:MM>
    omniacli workspace-helper storage-setup | storage-check <storage> | storage-reset <storage>
    omniacli workspace-helper enterprise-sync <version>
    omniacli workspace-helper enterprise <workspace> <storage> <instance> on|off
    omniacli workspace-helper relay-facts | relay-hosted | relay-host <key> | relay-unhost <key>   (on a relay)
    omniacli workspace-helper relay-key <workspace> | relay-tunnel <workspace> on|off   (behind NAT)
    (relay-host and relay-tunnel on take their values, JSON, on the input; README, Published through another machine)

OmniaCLI runs it with sudo (sudoers: README, Workspaces). It takes no path
and no command of the caller's: a workspace and an instance are names
(a-z 0-9 -), the storage the name of one of the storages the main
configuration (/etc/omniacli/omniacli.yaml, which only root writes) lists in
workspaces.storage (name: folder), the template one of TEMPLATES (the
package's, root's).

disk: the workspace's virtual disk, .disks/<workspace>.img in the storage, an
ext4 image that takes room as it is used, mounted on <workspace> there
by a systemd mount unit (at every boot), docker started after it (a drop-in
of docker.service); the empty folder beneath is immutable, so nothing is
written there while the disk is not mounted. Again with a larger size, it
grows the disk, mounted; a disk is never made smaller. A disk made or grown
must fit, whole, in the room free on the real disk now.

storage-setup: every storage of workspaces.storage bound on the root path of
its name (config.bound_root) by its unit, omniacli-storage-<name>.service,
which binds it again at every boot from the folder named then (a new disk:
only the folder changes), its identity kept, refused when another folder is
at the same path (README, Workspaces); the units of storages no longer
listed are taken away. Every action takes the storage this way.
storage-check <storage>: what that unit runs. storage-reset <storage>: the
folder now at its path taken as the storage, by root by hand only.

space: the room of each storage root's real disk (total, free) and of the
workspaces' disks there (size, what each takes), in bytes, and the networks
of each workspace's projects.

limits: the CPU and memory of all the workspace's containers together, a
systemd slice /etc/systemd/system/omniacli_<workspace>.slice (CPUQuota of
<cpus> cores, MemoryMax of <memory> GB and no swap; 0 is no limit) the
templates put every service in (cgroup_parent: ${SLICE}); written again
with other limits, they apply to the containers running.

create: an instance of the workspace from a template, production and test,
each a compose project <storage>/<workspace>/<instance>/<role> of root's
(compose.yaml of the template, .env of its checked values and of what this
helper makes: ports on 127.0.0.1, the database password, the project's
name, the workspace's slice; config/odoo.conf; the data folders, given to the image's users;
addons/, the caller's, for the customer's repositories of that role, which
OmniaCLI clones there), and <instance>/instance.json (the template and the
database), root's. It prints what it made, as JSON. The disk must be
mounted and the slice written (limits), the instance must not exist.

conf <workspace> <storage> <instance> <role> <JSON>: the role's odoo.conf
written again from the instance's template, the admin password kept. The
JSON may hold repositories (the addons path: the image's, then
/mnt/customer-addons/<repository> of each, folders of the role's addons/),
parameters (the template's editable Odoo options, checked against its
bounds, or against bounds {name: [min, max]} the workspace's admins of the
machine set) and debug (log_level = debug); what it leaves out stays as it
was, kept in <role>/omniacli.json, root's.

remove: docker compose down -v of both projects, then the instance's folder.

web: the workspace's nginx site, /etc/nginx/conf.d/omniacli-<workspace>.conf,
from the names of <spec> ({domain, main, subdomains, certificate, aliases,
redirects}: README, Domains of a workspace) and the ports of the instances
on its disk, read here; an alias is one more name of its environment's
server block, a redirect a server block of its own answering 301 to a name
of its environment; a name another site of nginx answers on is refused;
nginx -t, or the file before is put back; then, for certificate
letsencrypt, certbot (--webroot, one certificate omniacli-<workspace> for
all the names; an alias or a redirect only once this nginx answers a probe
on its name) and the site again with https. No domain or no instance: the
site and its certificate are removed.

web-names <workspace>: the names the other sites of nginx answer on (nginx
-T), which the workspace may not take; it reads only.

upstream <workspace> <storage> <instance> <role> <http port>|- <websocket
port>|-: the environment's server block of the site proxying to other ports
of 127.0.0.1 (the container of an update without stopping, zerodowntime.py),
or back to its own (-); a port where nothing answers is refused; nginx -t,
or the site before is back; a reload, the requests under way end where they
are. The ports are kept in <project>/upstream.json, root's, which web reads.

portal <host or ->: the customers' portal, OmniaCLI's own panel on host
(docs/portale-clienti.md), /etc/nginx/conf.d/omniacli-_portal.conf: port 80
answers the challenges and sends to https, 443 passes everything to the
service's address (/etc/default/omniacli), websockets too, with HSTS and a
limit on the login form; a name another site of nginx answers on is
refused, nginx -t or the file before back; Let's Encrypt's certificate when
the name leads here (_reaches_here), else a self-signed one; "-" removes
site and certificates. Refused where OmniaCLI does not keep nginx (no
workspaces.storage: a machine with its Odoo of before).
portal-names: the names the other sites answer on; it reads only.

ssh-keys | ssh-key-add <account> ({name, key, by} on the input) |
ssh-key-remove <account> <fingerprint>: the SSH keys of the people who log in
to the machine (README, SSH access; sshkeys.py): root, or a user with a login
shell from uid 1000, never the one OmniaCLI runs as; only the lines of
~/.ssh/authorized_keys OmniaCLI wrote (the mark omniacli-managed in their
comment) are added or taken out, the others kept as they are, in their
place; the key checked with ssh-keygen -l first, the file written 600 for
its account through a temporary file renamed over it, no link followed.

os-updates | os-updates-set on|off never|<HH:MM>: the automatic security
updates of the system (README; osupdates.py): unattended-upgrades as APT's
configuration says, the restart and the last run of its log (it reads
only); set: 20auto-upgrades and 52omniacli-unattended-upgrades written in
/etc/apt/apt.conf.d, refused when a later file decides otherwise.

firewall on|apply|off: the workspaces' containers reach the internet only.
Their networks are /24s of one pool (workspaces.network of the main
configuration, 10.213.0.0/16 when not set; create gives each project its
own, SUBNET in .env); chain OMNIACLI-WS-FWD, jumped from DOCKER-USER, drops
what they send to private addresses (the LAN, other networks of the
machine) but to the pool itself (docker's own rules decide between its
networks), OMNIACLI-WS-IN, jumped first from INPUT, drops what they send to
this machine; replies pass in both (nginx reaches Odoo through docker-proxy,
the answer comes in from the container); OMNIACLI-WS-MAIL, jumped from
DOCKER-USER, refuses SMTP (25, 465, 587, 2525) and IMAP/POP (110, 143, 993, 995) from the network of a staging
whose mail is caught to anything outside that network (its mail service is
in it), whatever its database says; create and remove make it again, from
the instances of the mounted disks and /etc/omniacli/mail-guard.json, where
they are kept for the boot (the disks come after). on: the rules, and the unit
omniacli-workspaces-firewall.service that applies them at every boot before
docker; apply: the rules only (what the unit runs); off: both removed.
create puts them on first. The same unit applies OMNIACLI-DB-FWD and
OMNIACLI-DB-IN, of the machine's clusters that listen for other machines
(/etc/omniacli/db-listen.json): off is refused while one listens.

machine-db-grant <major> (its values, JSON, on the input) | machine-db-ungrant
<major> <role> revoke|allow|drop | machine-db-port <major> close: a database
of the machine's cluster for an instance of another machine, a linked
OmniaCLI's (machinedb.py, AD-003): the cluster listens on a port over TLS
the first time, the role and its database are made, pg_hba.conf has one
line for that address, the firewall lets that address reach the port.

machine-db-archive-on | -off | -base | -state <major>: the recovery at a
minute of the machine's cluster (clusterpitr.py, AD-006): its WAL archive
and base backups, in the cluster's project on the storage's real disk,
switched on (a restart of the cluster only when archive_mode changes, and
only with restart), off (no restart: the bases and the archive deleted), a
base backup with the retention, what there is. machine-db-recover: the
database of an instance on the cluster as it was at a minute, into the
database of a staging of the same workspace, through a temporary cluster
that is deleted whatever happens. machine-db-recover-sweep: what a recovery
that was killed left, deleted.

machine-db-replica-*: a read replica of the machine's cluster on another
machine, a linked OmniaCLI's (managedreplica.py, AD-004). On the machine
that has the cluster: facts (what the other machine says before), allow
(the replication's role with the verifier of its password, one slot, a cap
on the changes it holds, one line of pg_hba.conf, the firewall), ready
(where the replica answers and its CA), state, cap, drop, replaced (the
replica was promoted: the cluster stopped, for good); read on|off (an
instance of the cluster reads from the replica: db_replica_host and
db_replica_port in its odoo.conf, never empty, db_sslmode verify-full and
the replica's CA in PGSSLROOTCERT by a compose.override.yaml, the firewall
of the workspaces letting that address and port through); follow (after a
promotion: the instance takes the replica as its database, over TLS, as an
instance on the cluster of a linked machine does). On the machine that
hosts the replica: make prepare (its project, the room checked, the
password of the replication on the input), copy (pg_basebackup over TLS),
start (a hot standby), hosted (what it is doing), sync (pg_hba.conf and
the tablespaces' folders from what the primary has now), promote, remove.

The replica of the dedicated database of one instance (UX-011 §D): the same
commands, where <major> is <workspace>.<storage>.<instance> on the machine
that has the instance (facts, allow, ready, state, cap, drop, replaced) and
d<n>, the copy's own key, on the machine that hosts it (make, hosted, sync,
promote, remove). There the database's own service listens over TLS on a
port of this machine by a compose.override.yaml of OmniaCLI's beside the
instance's compose.yaml, which is never changed; read on|off and follow
take such an instance too (its Odoo verifies its own database's certificate
and the replica's). machine-db-replica-unlisten: the port closed again and
the override gone, the database's container made again, once it has no
replica (Close the port… on the instance's row of the Database tab).

The return after a promotion (UX-011 §A). machine-db-replica-return, on the
machine that hosts the promoted copy: facts, allow (the cluster it
replaced let in for the replication, from the one address it already comes
from: the role with the verifier of a password made there, a place that
holds the changes, its cap; nothing restarts), state, stop (the first step
of the completion: the copy stopped clean, and where its last record is),
resume (it runs again: the cluster did not get everything), done (it stays
stopped for good: its container taken away), cancel. machine-db-return, on
the machine whose cluster comes back: facts, prepare (the cluster's data
set aside, renamed where it is; the password on the input), copy
(pg_basebackup over TLS into the cluster's own project), start (read only,
following the promoted copy), state, wait (until the last record of the
stopped copy is applied; its position on the input), promote (it takes the
writes and is the machine's cluster again), note, purge (the data set aside
deleted), cancel (the copy deleted, the data set aside back), forget.
machine-db-replica-back: an instance pointed at the promoted copy goes back
on the machine's cluster, by its socket, as it was before the promotion.
The return of the dedicated database of one instance: machine-db-return
<workspace>.<storage>.<instance> does the same steps on the instance's own
data folder, on its workspace's disk, in a project of its own beside the
instance's (.replica/return: no port, a network that goes out to the
promoted copy's port only), which no command of the instance reaches; then
machine-db-replica-back stops that project clean and writes the instance
again as one with a database of its own (the template it had, kept in
.before-promotion), its database listening as before the promotion for no
other machine, and starts it: Odoo and its database, once.

The databases other machines use, after a promotion (UX-011 §B). The
promoted copy lets in the roles the cluster had let in, each from the
address it came from (machine-db-replica-promote takes them on its input;
pg_hba.conf, the firewall); machine-db-replica-grant: one of them revoked,
allowed again, dropped, as machine-db-ungrant on a cluster. machine-db-moved,
on the machine whose instance uses such a database: the instance pointed to
where its database is now (the promoted copy, or the cluster again after
its return), over TLS with that place's CA, and started again; a copy
hosted on this very machine is reached at this machine's own address, and
the firewalls let that one project's network through.

template-install <template>: a template downloaded from the download
server, checked with the key of the main configuration, into
/var/lib/omniacli-templates/<template> (templatestore.py); of it and the
package's, create takes the higher version.

enterprise-sync <version> (UX-012): the working copy of Odoo Enterprise of
that version branch (19.0) OmniaCLI's user fetched, in the folder the main
configuration's jobs_dir names (enterprise.checkouts_of), read by tar run as
that user (never as root: root reads nothing the user could not) and written
by root, without .git, into /var/lib/omniacli-enterprise/<version> (links
out of it, devices and set-id bits refused), root's and read only for the
others, outside every workspace, file by file in place (the folder the
Enterprise instances mount stays the same folder); what it holds is kept in
.state/<version>.json. A copy without one module is refused.
enterprise <workspace> <storage> <instance> on|off: the instance's Odoo
mounts that copy read only (its compose.override.yaml) and has it first in
its addons_path (conf), its record keeps the version; off takes both away.
Whoever asked restarts its Odoo, and checks first (off) that no module of it
is installed in its database.

owncert: the customer's certificate and key, {certificate, key} as JSON on
the standard input (a key is no argument: ps shows those), checked with
openssl (the key is the certificate's, not expired), kept in
/etc/omniacli/certs/<workspace>/, root's only.
"""

from __future__ import annotations

import contextlib
import fcntl
import ipaddress
import json
import os
import pwd
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

import yaml

from . import (clusterpitr, composehelper, config, enterprise, machinedb, managedreplica, pitrhelper, relay,
               stacktemplates, templatestore, workspaceweb)
from .composehelper import ENV, HelperError, caller
from .config import (CERTIFICATES, DOMAIN_NAME, OMNIACLI_COMMAND, SUBDOMAIN, TEST_LABEL, WORKSPACE_NAME,
                     ConfigError, parse_storage_roots, web_names)
from .pluginstore import SYSTEM_CONFIG
from . import osupdates, sshkeys

# the templates root runs: the package's (build_deb.sh), root's. sudo does
# not pass the environment on (OMNIACLI_TEMPLATES is for the tests, as
# OMNIACLI_SYSTEM_CONFIG of pluginstore)
TEMPLATES = Path(os.environ.get("OMNIACLI_TEMPLATES", "/usr/share/omniacli/templates"))
SYSTEMD = Path("/etc/systemd/system")
ROLES = ("production", "test")
# Odoo's role in a new database cluster: no superuser, its password from the
# environment of the database's service, never in this file
DB_ROLE_SCRIPT = """# written by omniacli workspace-helper
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -v password="$ODOO_DB_PASSWORD" <<'SQL'
CREATE ROLE odoo LOGIN CREATEDB PASSWORD :'password';
SQL
"""
# what an instance was made from, root's: conf writes its odoo.conf again from it
INSTANCE_FILE = "instance.json"
# what a role was given (repositories, parameters, debug): conf's own record
ROLE_FILE = "omniacli.json"
# a customer's repository, a folder of the role's addons/
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# the ports an instance's services are published on, on 127.0.0.1
PORTS = range(20000, 30000)
MAX_DISK_GB = 65536
# the workspace's CPU and memory: cores and GB, 0 no limit
MAX_CPUS = 1024
MAX_MEMORY_GB = 65536
GB = 1024 ** 3
NGINX_SITES = Path("/etc/nginx/conf.d")
LETSENCRYPT = Path("/etc/letsencrypt/live")
OWN_CERTS = Path("/etc/omniacli/certs")
# the service's settings: the address OmniaCLI listens on (OMNIACLI_HOST,
# OMNIACLI_PORT), which the workspaces' sites pass the Log in as links to
SERVICE_DEFAULTS = Path("/etc/default/omniacli")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}")
# a certificate with its chain and its key, on the standard input
MAX_PEM = 256 * 1024
# the networks of the workspaces' projects: a /24 each of one pool
NETWORK_POOL = "10.213.0.0/16"
SUBNET_PREFIX = 24
FIREWALL_UNIT = "omniacli-workspaces-firewall.service"
CHAIN_FORWARD = "OMNIACLI-WS-FWD"
CHAIN_INPUT = "OMNIACLI-WS-IN"
# the mail of a capturing staging (UX-008 §5.3) never leaves, whatever its
# database says: its project's network may not reach SMTP outside itself
CHAIN_MAIL = "OMNIACLI-WS-MAIL"
# and IMAP/POP: a real mailbox switched on by hand in its Odoo would be read and
# emptied by the staging's fetchmail (its own inbox service is in its network)
SMTP_PORTS = (25, 465, 587, 2525, 110, 143, 993, 995)
# {project folder: SUBNET} of those projects, root's: the boot's firewall
# apply runs before the workspaces' disks are mounted, it reads them here
MAIL_GUARD = Path("/etc/omniacli/mail-guard.json")
# {project folder: {"subnet", "ip", "port"}} of the projects whose database is
# on another machine at a private address (AD-003): their network reaches
# that one address and port, and nothing else private; root's, as MAIL_GUARD
DB_REMOTE = Path("/etc/omniacli/db-remote.json")
# the machine's clusters that listen for other machines (AD-003, a linked
# OmniaCLI's): {bridge: [addresses]}, root's, kept here for the boot as
# MAIL_GUARD; chain OMNIACLI-DB-FWD, jumped from DOCKER-USER, lets their port
# be reached from those addresses only and lets nothing but answers out of
# their network, OMNIACLI-DB-IN, jumped from INPUT, nothing from it to this machine
DB_LISTEN = Path("/etc/omniacli/db-listen.json")
CHAIN_DB = "OMNIACLI-DB-FWD"
CHAIN_DB_INPUT = "OMNIACLI-DB-IN"
# what a workspace's container may not reach (but the pool: docker decides)
PRIVATE = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10")


def _tool(name: str) -> str:
    found = shutil.which(name, path=ENV["PATH"])
    if found is None:
        raise HelperError(f"{name} is not installed")
    return found


def _run(*cmd: str, check: bool = True) -> str:
    done = subprocess.run([_tool(cmd[0]), *cmd[1:]], env=ENV, capture_output=True, text=True)
    if check and done.returncode:
        raise HelperError(f"{' '.join(cmd)}: {(done.stderr or done.stdout).strip()}")
    return done.stdout


def _system(config: Path = SYSTEM_CONFIG) -> dict:
    try:
        data = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise HelperError(f"cannot read {config}: {exc}") from None
    return data if isinstance(data, dict) else {}


def storage_roots(config: Path = SYSTEM_CONFIG) -> dict[str, str]:
    """{name: folder} of workspaces.storage."""
    data = _system(config)
    try:
        return parse_storage_roots(data.get("workspaces"))
    except Exception as exc:  # noqa: BLE001 - ConfigError, reported as the helper's
        raise HelperError(str(exc)) from None


def _name(kind: str, value: str) -> str:
    if not WORKSPACE_NAME.fullmatch(value):
        raise HelperError(f"{value!r} is not a {kind} name (a-z 0-9 -)")
    return value


def _safe_folder(folder: Path, what: str) -> None:
    """folder and every one above it out of the caller's reach."""
    _, uid, groups = caller()
    if uid == 0:
        return
    if Path(os.path.realpath(folder)) != folder:
        raise HelperError(f"{what} {folder} goes through a symbolic link")
    for path in (folder, *folder.parents):
        st = path.lstat()
        if composehelper._writable(st, uid, groups) and not (st.st_mode & stat.S_ISVTX and path != folder):
            raise HelperError(f"{what} {folder}: {path} is writable by the caller")


def workspace(name: str, storage: str) -> tuple[Path, Path]:
    """(image, mount point) of a workspace in one of the storage roots, on
    the root path the storage is bound on (bind_storage)."""
    _name("workspace", name)
    root = bind_storage(storage)
    _safe_folder(root, "the storage")
    return root / ".disks" / f"{name}.img", root / name


# The storages' binds (README, Workspaces): each storage of workspaces.storage,
# a name and a folder, is bound on a root path of its name (config.bound_root),
# so the folders above it may be anyone's (a bind holds the folder, not its
# path) and a new folder (another disk) may take its place with the name kept.
# Its unit, omniacli-storage-<name>.service, binds it at every boot from the
# folder the main configuration names then, before the workspaces' disks and
# docker. The identity (device, inode) of the folder bound, and its path, are
# kept next to the binds (root's only): at the same path another folder is
# refused (moved or replaced under it, by anyone who may write above it),
# while a path changed in the main configuration (root's) is a new folder,
# taken and kept.
STORAGE_IDS = Path(config.STORAGE_BASE) / ".ids"


def bound(storage: str) -> Path:
    return Path(config.bound_root(storage))


MOUNTINFO = Path("/proc/self/mountinfo")


def _is_mount(path: Path) -> bool:
    """path is a mount point. os.path.ismount does not see a bind of a folder
    of the same file system (same device), hence the kernel's own list."""
    try:
        lines = MOUNTINFO.read_text(encoding="utf-8").splitlines()
    except OSError:
        return os.path.ismount(path)
    target = str(path)
    for line in lines:
        fields = line.split()
        # the mount point, its spaces and such as octal escapes (\040)
        if len(fields) > 4 and re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4]) == target:
            return True
    return os.path.ismount(path)


def check_unit_name(target: Path) -> str:
    return f"omniacli-storage-{target.name}.service"


def storage_unit_text(storage: str, target: Path) -> str:
    return ("# written by omniacli workspace-helper: a storage of the workspaces, bound on a root path\n"
            f"[Unit]\nDescription=OmniaCLI storage {storage}\nBefore=docker.service\n"
            "After=local-fs.target remote-fs.target\n"
            # bound again by every action that finds it unbound: never held back by systemd's start limit
            "StartLimitIntervalSec=0\n"
            f"[Service]\nType=oneshot\nRemainAfterExit=yes\n"
            f"ExecStart={OMNIACLI_COMMAND} workspace-helper storage-check {storage}\n"
            f"ExecStop=-/bin/umount {target}\n"
            "[Install]\nWantedBy=multi-user.target\n")


def _identity(path: Path) -> str:
    st = os.stat(path)
    return f"{st.st_dev} {st.st_ino}"


def _id_file(target: Path) -> Path:
    return STORAGE_IDS / f"{target.name}.id"


def _recorded(target: Path) -> tuple[str, str] | None:
    """(identity, folder) kept for the storage bound on target, None: none."""
    try:
        identity, _, folder = _id_file(target).read_text().strip().partition("\n")
    except OSError:
        return None
    return identity.strip(), folder.strip()


def _root_only(path: Path) -> str | None:
    """Why path is not root's alone (nobody else may write it), None when it is."""
    st = os.stat(path)
    if st.st_uid != 0 or st.st_mode & 0o022:
        return f"{path} is not root's alone (owner uid {st.st_uid}, mode {stat.S_IMODE(st.st_mode):o})"
    return None


def _folder(storage: str) -> Path:
    """The folder the main configuration names for storage now."""
    roots = storage_roots()
    if storage not in roots:
        raise HelperError(f"{storage} is not a storage of workspaces.storage of {SYSTEM_CONFIG}")
    return Path(roots[storage])


def bound_problem(storage: str, target: Path) -> str | None:
    """Why what is bound on target is not the storage's folder, None when it is."""
    try:
        folder = _folder(storage)
    except HelperError as exc:
        return str(exc)
    if not _is_mount(target):
        return f"{storage} is not bound on {target}"
    problem = _root_only(target)
    if problem:
        return f"the storage {storage}: {problem}"
    kept = _recorded(target)
    if kept and kept[1] == str(folder) and kept[0] != _identity(target):
        return (f"the storage {storage} ({folder}) is not the folder set up there (it was moved or replaced): "
                f"unbound; if the change is wanted, run as root: omniacli workspace-helper storage-reset {storage}")
    if folder.is_dir() and _identity(folder) != _identity(target):
        return f"the storage {storage} bound is not its folder {folder} now (the main configuration changed)"
    return None


def _below(target: Path) -> list[str]:
    """What is mounted under target (the workspaces' disks)."""
    return [line for line in _run("findmnt", "--noheadings", "--list", "--output", "TARGET", check=False).split()
            if line.startswith(f"{target}/")]


def _bind_now(storage: str, target: Path) -> None:
    """The storage's folder bound on target (its unit's start)."""
    folder = _folder(storage)
    if not folder.is_dir():
        raise HelperError(f"the storage {storage}: {folder} is not a folder")
    if Path(os.path.realpath(folder)) != folder:
        raise HelperError(f"the storage {storage} ({folder}) goes through a symbolic link: name its real path")
    problem = _root_only(folder)
    if problem:
        raise HelperError(f"the storage {storage}: {problem}: make it root's (chown root: …; chmod go-w …)")
    kept = _recorded(target)
    if kept and kept[1] == str(folder) and kept[0] != _identity(folder):
        raise HelperError(f"the storage {storage} ({folder}) is not the folder set up there (it was moved or "
                          f"replaced): not bound; if the change is wanted, run as root: "
                          f"omniacli workspace-helper storage-reset {storage}")
    _run("mount", "--bind", str(folder), str(target))
    _run("mount", "-o", "remount,bind,nosuid,nodev", str(target))


def _unbind(target: Path) -> None:
    """The storage unbound: its unit stopped, so systemd knows it is."""
    _run("systemctl", "stop", check_unit_name(target), check=False)
    if _is_mount(target):
        _run("umount", str(target), check=False)


def bind_storage(storage: str) -> Path:
    """storage bound on its root path and checked, its unit written that does
    it again at every boot; the root path. A storage whose folder changed in
    the main configuration is bound again, unless a workspace's disk is in use."""
    folder = _folder(storage)
    target = bound(storage)
    unit = check_unit_name(target)
    if _is_mount(target):
        problem = bound_problem(storage, target)
        if not problem:
            _record(target, folder)
            return target
        if "main configuration changed" not in problem:
            raise HelperError(problem)
        busy = _below(target)
        if busy:
            raise HelperError(f"{problem}; workspace disks are mounted on it ({', '.join(busy)}): "
                              "stop them, or restart the machine")
        _unbind(target)
    if not folder.is_dir():
        raise HelperError(f"the storage {storage}: {folder} is not a folder")
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    _safe_folder(target.parent, "the storages' folder")
    if not target.exists():
        target.mkdir(mode=0o755)
    if not _is_mount(target) and not any(target.iterdir()):
        # nothing is written there while the storage is not bound
        _run("chattr", "+i", str(target))
    _write(SYSTEMD / unit, storage_unit_text(storage, target))
    _run("systemctl", "daemon-reload")
    _run("systemctl", "enable", unit)
    # stopped first: a unit systemd still takes for active (its folder
    # unbound outside it) would not bind again on start
    _run("systemctl", "stop", unit, check=False)
    started = _run("systemctl", "start", unit, check=False)
    problem = bound_problem(storage, target)
    if problem:
        _unbind(target)
        status = _run("systemctl", "status", "--no-pager", "-n", "5", unit, check=False)
        raise HelperError(problem if _is_mount(target) or "set up" in problem else
                          f"{unit} did not bind {storage} ({folder}) on {target}: {started or status}".strip())
    _record(target, folder)
    return target


def _record(target: Path, folder: Path) -> None:
    """The identity and the folder of the storage bound on target, kept when
    there is none yet or the folder is another (the main configuration changed)."""
    kept = _recorded(target)
    if kept is None or kept[1] != str(folder):
        STORAGE_IDS.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write(_id_file(target), f"{_identity(target)}\n{folder}\n", mode=0o600)


def storage_check(storage: str) -> dict:
    """What the storage's unit runs at every boot: its folder bound on its
    root path, the folder the main configuration names then; refused (and
    unbound, the root path beneath is immutable) when it is not the folder
    set up at that path, so neither the workspaces' disks nor docker's
    containers use it."""
    target = bound(storage)
    if not _is_mount(target):
        _bind_now(storage, target)
    problem = bound_problem(storage, target)
    if problem:
        if _is_mount(target):
            _run("umount", str(target), check=False)
        raise HelperError(problem)
    _record(target, _folder(storage))
    return {"storage": storage, "folder": str(_folder(storage)), "bound": str(target), "identity": _identity(target)}


def _stale_units(roots: dict[str, str]) -> list[Path]:
    """The units of storages this helper wrote that are none of roots now
    (another name, or the binds of before: a .mount of a storage's root path)."""
    found = []
    base = config.STORAGE_BASE
    for path in sorted(SYSTEMD.glob("omniacli-storage-*.service")):
        name = path.name[len("omniacli-storage-"):-len(".service")]
        if name not in roots and path.read_text(errors="replace").startswith("# written by omniacli workspace-helper"):
            found.append(path)
    for path in sorted(SYSTEMD.glob("*.mount")):
        text = path.read_text(errors="replace")
        where = next((line[6:] for line in text.splitlines() if line.startswith("Where=")), "")
        if not text.startswith("# written by omniacli workspace-helper") or not where.startswith(base + "/"):
            continue
        storage, _, below = where[len(base) + 1:].partition("/")
        # a storage's bind of before, or a workspace's disk on a storage no longer listed
        if (text.startswith("# written by omniacli workspace-helper: a storage") and not below) or (
                below and "/" not in below and storage not in roots):
            found.append(path)
    return found


def storage_setup() -> dict:
    """Every storage of workspaces.storage whose folder exists bound and
    checked (the package runs it); one that fails is told, the others are
    bound. The units of storages no longer there, and the binds of before,
    are taken away when nothing of theirs is mounted."""
    found, roots = {}, storage_roots()
    for path in _stale_units(roots):
        where = next((line[6:] for line in path.read_text().splitlines() if line.startswith("Where=")), "")
        target = Path(where) if where else bound(path.name[len("omniacli-storage-"):-len(".service")])
        if _below(target) or (path.suffix == ".mount" and _is_mount(target) and target.parent != Path(
                config.STORAGE_BASE)):
            found[path.name] = f"kept: {target} is in use (stop its workspace's instances, then again)"
            continue
        _run("systemctl", "disable", "--now", path.name, check=False)
        path.unlink(missing_ok=True)
        if _is_mount(target):
            _run("umount", str(target), check=False)
        found[path.name] = "removed"
    _run("systemctl", "daemon-reload", check=False)
    for storage, folder in roots.items():
        if not Path(folder).is_dir():
            found[storage] = f"{folder}: not a folder yet"
            continue
        try:
            found[storage] = f"{folder} bound on {bind_storage(storage)}"
        except HelperError as exc:
            found[storage] = f"refused: {exc}"
    return {"storages": found}


def storage_reset(storage: str) -> dict:
    """The folder now at storage's path taken as the storage (an admin moved
    it on purpose): by root by hand only, never through sudo, with no
    workspace disk mounted on it."""
    _, uid, _ = caller()
    if uid != 0:
        raise HelperError("storage-reset is run by root by hand, not through sudo")
    target = bound(storage)
    below = _below(target)
    if below:
        raise HelperError(f"workspace disks are mounted on {target}: {', '.join(below)}; stop them first")
    _unbind(target)
    _id_file(target).unlink(missing_ok=True)
    return {"storage": storage, "bound": str(bind_storage(storage))}


def loop_of(image: Path) -> str | None:
    """The loop device image is attached to, None: none."""
    out = _run("losetup", "--noheadings", "--output", "NAME", "--associated", str(image), check=False)
    return out.split()[0] if out.split() else None


def mounted(image: Path, mount: Path) -> bool:
    """mount is the image's file system (not the folder beneath it)."""
    if not os.path.ismount(mount):
        return False
    source = _run("findmnt", "--noheadings", "--output", "SOURCE", "--mountpoint", str(mount), check=False).strip()
    loop = loop_of(image)
    return bool(loop) and source == loop


# nosuid, nodev: what a container's root leaves on the disk (a setuid program, a
# device node) has no effect for the users of this machine (docs/workspaces.md)
MOUNT_OPTIONS = "loop,nosuid,nodev"


def unit_name(mount: Path) -> str:
    return _run("systemd-escape", "--path", "--suffix=mount", str(mount)).strip()


def unit_text(image: Path, mount: Path, name: str) -> str:
    # after its storage's check: never a disk of a storage that is not the one set up
    check = check_unit_name(mount.parent)
    return ("# written by omniacli workspace-helper: the virtual disk of workspace " + name + "\n"
            "[Unit]\nDescription=OmniaCLI workspace " + name + "\nBefore=docker.service\n"
            f"Requires={check}\nAfter={check}\n"
            f"[Mount]\nWhat={image}\nWhere={mount}\nType=ext4\nOptions={MOUNT_OPTIONS}\n"
            "[Install]\nWantedBy=multi-user.target\n")


def _write(path: Path, text: str, mode: int = 0o644, uid: int = 0, gid: int = 0) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chown(tmp, uid, gid)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def slice_name(name: str) -> str:
    """The workspace's slice: a - in a slice's name puts it in another
    (omniacli-acme-shop.slice in omniacli-acme.slice), hence _, which no
    workspace name holds."""
    return "omniacli_" + _name("workspace", name).replace("-", "_") + ".slice"


def slice_text(name: str, cpus: float, memory_gb: float) -> str:
    return ("# written by omniacli workspace-helper: the CPU and memory of workspace " + name + "\n"
            "[Unit]\nDescription=OmniaCLI workspace " + name + "\n[Slice]\n"
            + (f"CPUQuota={round(cpus * 100)}%\n" if cpus else "CPUQuota=\n")
            + (f"MemoryMax={int(memory_gb * GB)}\nMemorySwapMax=0\n" if memory_gb
               else "MemoryMax=infinity\nMemorySwapMax=infinity\n"))


def _amount(what: str, value: str, top: int) -> float:
    try:
        number = float(value)
    except ValueError:
        number = -1
    if not 0 <= number <= top:
        raise HelperError(f"the {what} is 0 (no limit) to {top}, not {value!r}")
    return number


def limits(name: str, cpus: str, memory_gb: str) -> dict:
    unit = slice_name(name)
    cores = _amount("CPU", cpus, MAX_CPUS)
    memory = _amount("memory", memory_gb, MAX_MEMORY_GB)
    _write(SYSTEMD / unit, slice_text(name, cores, memory))
    _run("systemctl", "daemon-reload")
    return {"workspace": name, "slice": unit, "cpus": cores, "memory_gb": memory}


def _close_mount(mount: Path) -> None:
    """The disk's top: nobody but root and the group of the user OmniaCLI runs
    as gets in, so no user of this machine reads a configuration or a
    filestore. Run by hand as root there is no such user: left as it is."""
    _, uid, _ = caller()
    if uid != 0:
        os.chown(mount, 0, pwd.getpwuid(uid).pw_gid)
        os.chmod(mount, 0o750)


def _harden_mount(mount: Path) -> None:
    """A disk mounted before MOUNT_OPTIONS: at once, without stopping its
    containers (the unit, rewritten, holds them for the next mounts)."""
    options = _run("findmnt", "--noheadings", "--output", "OPTIONS", "--mountpoint", str(mount)).strip().split(",")
    if "nosuid" not in options or "nodev" not in options:
        _run("mount", "-o", "remount,nosuid,nodev", str(mount))


def disk(name: str, storage: str, size_gb: str) -> dict:
    image, mount = workspace(name, storage)
    if not (size_gb.isdigit() and 1 <= int(size_gb) <= MAX_DISK_GB):
        raise HelperError(f"the size is 1 to {MAX_DISK_GB} GB, not {size_gb!r}")
    size = int(size_gb) * GB
    image.parent.mkdir(mode=0o700, exist_ok=True)
    os.chmod(image.parent, 0o700)
    grow = False
    if image.is_symlink():
        raise HelperError(f"{image} is a symbolic link")
    if not image.exists() or image.stat().st_size < size:
        _fits(image, size, storage)
    if not image.exists():
        fd = os.open(image, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.truncate(image, size)
        _run("mkfs.ext4", "-q", "-F", str(image))
    elif image.stat().st_size < size:
        os.truncate(image, size)
        grow = True
    elif image.stat().st_size > size:
        raise HelperError(f"the disk of {name} is {image.stat().st_size // GB} GB: a disk is not made smaller")
    if not mount.exists():
        mount.mkdir(mode=0o755)
    if not os.path.ismount(mount):
        if any(mount.iterdir()):
            raise HelperError(f"{mount} is not empty: a disk is mounted on an empty folder only")
        # nothing is written beneath the disk while it is not mounted
        _run("chattr", "+i", str(mount))
    unit = unit_name(mount)
    _write(SYSTEMD / unit, unit_text(image, mount, name))
    dropin = SYSTEMD / "docker.service.d"
    dropin.mkdir(mode=0o755, exist_ok=True)
    _write(dropin / f"omniacli-{name}.conf",
           f"# written by omniacli workspace-helper: docker after the disk of workspace {name}\n"
           f"[Unit]\nAfter={unit}\n")
    _run("systemctl", "daemon-reload")
    _run("systemctl", "enable", "--now", unit)
    if not mounted(image, mount):
        raise HelperError(f"{unit} did not mount {image} on {mount}")
    _harden_mount(mount)
    if grow:
        loop = loop_of(image)
        _run("losetup", "--set-capacity", loop)
        _run("resize2fs", loop)
    _close_mount(mount)
    return {"workspace": name, "image": str(image), "mount": str(mount), "unit": unit, "size_gb": int(size_gb)}


def drop(name: str, storage: str) -> dict:
    """A workspace's disk taken away, when nothing is on it (lost+found
    only): a New instance that failed, a workspace deleted before any
    instance or after its last one. Its mount unit, docker's drop-in and its
    slice go too (the slice stopped first: with its file gone, a slice that
    held containers stays loaded until it is), and the rest of the
    workspace's machine state: the customer's certificate
    (/etc/omniacli/certs/<workspace>, which a workspace made again with the
    name would find and serve), and its site, its lock and its Let's
    Encrypt certificate when the publication after its last instance did not
    take them away. A disk with an instance's folder is refused (its
    instance is removed first)."""
    image, mount = workspace(name, storage)
    if image.is_symlink():
        raise HelperError(f"{image} is a symbolic link")
    if os.path.ismount(mount):
        left = sorted(p.name for p in mount.iterdir() if p.name != "lost+found")
        if left:
            raise HelperError(f"the disk of {name} holds {', '.join(left)}: remove its instances first")
    made = {"workspace": name}
    # the site first: it names the certificate taken away below, and nginx refuses a site whose file is missing
    site = NGINX_SITES / f"omniacli-{name}.conf"
    if site.is_file():
        made["nginx"] = _put_site(site, None)
    (NGINX_SITES / f".omniacli-{name}.lock").unlink(missing_ok=True)
    if (LETSENCRYPT / f"omniacli-{name}").exists():
        _run("certbot", "delete", "--cert-name", f"omniacli-{name}", "--non-interactive", check=False)
    own = OWN_CERTS / name
    made["certificate"] = own.exists() or own.is_symlink()
    if own.is_symlink() or own.is_file():
        own.unlink()
    elif own.is_dir():
        shutil.rmtree(own)
    unit, slice_unit = unit_name(mount), slice_name(name)
    _run("systemctl", "disable", "--now", unit, check=False)
    if os.path.ismount(mount):
        _run("umount", str(mount))
    _run("systemctl", "stop", slice_unit, check=False)
    for path in (SYSTEMD / unit, SYSTEMD / "docker.service.d" / f"omniacli-{name}.conf", SYSTEMD / slice_unit):
        path.unlink(missing_ok=True)
    _run("systemctl", "daemon-reload")
    _run("systemctl", "reset-failed", slice_unit, check=False)
    if mount.is_dir() and not os.path.ismount(mount):
        _run("chattr", "-i", str(mount), check=False)
        if not any(mount.iterdir()):
            mount.rmdir()
    freed = image.stat().st_size // GB if image.exists() else 0
    image.unlink(missing_ok=True)
    if _mail_guard_file():
        mail_guard_apply()
    return {**made, "dropped": str(image), "size_gb": freed, "slice": slice_unit}


def _used(image: Path) -> int:
    """What the disk's image takes on the real disk (it grows as it is used)."""
    return image.stat().st_blocks * 512 if image.exists() else 0


def _fits(image: Path, size: int, storage: str) -> None:
    """A disk made or grown to size must fit, whole, in the room free on the
    real disk now; what other workspaces were promised is OmniaCLI's check
    (README, Workspaces: the space of the disks), which an admin may force."""
    # the storage's root path (the image is in its .disks), on its real disk
    vfs = os.statvfs(image.parent.parent)
    free, needed = vfs.f_bavail * vfs.f_frsize, size - _used(image)
    if needed > free:
        raise HelperError(f"a disk of {size // GB} GB needs {needed / GB:.1f} GB more on the real disk "
                          f"of {storage}, which has {free / GB:.1f} GB free")


def space() -> dict:
    """The room of each storage root's real disk and of the workspaces' disks
    on it, in bytes: read here, since the images are root's only."""
    roots = []
    for root, path in storage_roots().items():
        # its root path once bound (the images are there), the folder itself before
        folder = bound(root) if _is_mount(bound(root)) else Path(path)
        if not folder.is_dir():
            roots.append({"root": root, "folder": path, "missing": True})
            continue
        vfs = os.statvfs(folder)
        images = {}
        disks = folder / ".disks"
        if disks.is_dir() and not disks.is_symlink():
            for image in sorted(disks.glob("*.img")):
                if image.is_symlink() or not image.is_file() or not WORKSPACE_NAME.fullmatch(image.stem):
                    continue
                st = image.stat()
                images[image.stem] = {"size": st.st_size, "used": st.st_blocks * 512}
                fs = _filesystem(folder / image.stem)
                if fs is not None:
                    images[image.stem]["fs"] = fs
        roots.append({"root": root, "folder": path, "device": folder.stat().st_dev, "total": vfs.f_blocks * vfs.f_frsize,
                      "free": vfs.f_bavail * vfs.f_frsize, "images": images,
                      "networks": {name: _networks_of(folder / name) for name in images}})
    return {"roots": roots}


def _filesystem(mount: Path) -> dict | None:
    """The file system inside a workspace's disk, as df on its mount says it
    (size, used, free to its users: what the workspace has left), None when
    it is not mounted. The image's own used (its blocks on the real disk)
    stays as high as the most it ever held until its free blocks are
    discarded (trim): the host's concern, not the workspace's (23/11/2026:
    1.8 GB said free, 19 GB free inside after a large clone was deleted)."""
    if mount.is_symlink() or not os.path.ismount(mount):
        return None
    try:
        vfs = os.statvfs(mount)
    except OSError:
        return None
    return {"size": vfs.f_blocks * vfs.f_frsize, "used": (vfs.f_blocks - vfs.f_bfree) * vfs.f_frsize,
            "free": vfs.f_bavail * vfs.f_frsize}


def trim(image: Path, mount: Path) -> int | None:
    """The free blocks of a mounted workspace's disk given back to the real
    disk (fstrim: the loop device punches holes in the image): the bytes the
    image takes less on the real disk after it; None when fstrim could not
    (a file system or a loop that does not discard). The weekly
    fstrim.timer of the host does the same for every mount."""
    before = _used(image)
    try:
        _run("fstrim", str(mount))
    except HelperError:
        return None
    return max(before - _used(image), 0)


def _networks_of(mount: Path) -> list[str]:
    """The networks of a workspace's projects (SUBNET of their .env, root's
    only): its traffic is that of their bridges (metrics.py)."""
    found = []
    for env_file in sorted(mount.glob("*/*/.env")):
        try:
            value = _env(env_file.parent).get("SUBNET", "")
            found.append(str(ipaddress.ip_network(value)))
        except (OSError, ValueError):
            continue
    return found


def network_pool() -> ipaddress.IPv4Network:
    value = str((_system().get("workspaces") or {}).get("network") or NETWORK_POOL)
    try:
        pool = ipaddress.ip_network(value)
    except ValueError:
        raise HelperError(f"workspaces.network of {SYSTEM_CONFIG}: {value!r} is not a network (a.b.c.d/n)") from None
    if pool.version != 4 or not pool.is_private or pool.prefixlen > SUBNET_PREFIX:
        raise HelperError(f"workspaces.network of {SYSTEM_CONFIG}: {value} is not a private IPv4 network "
                          f"of /{SUBNET_PREFIX} or larger")
    return pool


def firewall_rules(pool: ipaddress.IPv4Network, remote: list[dict] = ()) -> dict[str, list[list[str]]]:
    """The rules of the two chains, as iptables arguments after -A <chain>.
    remote: the databases of other machines at private addresses, each let
    through from its project's network only, to its address and port only."""
    src = ["-s", str(pool)]
    replies = [*src, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "RETURN"]
    # local: a port of this machine itself, which docker forwards to a container: by the time the rule
    # is read the destination is that container's, so the one the connection was made to is matched
    reached = [["-s", r["subnet"], "-p", "tcp", "-m", "conntrack", "--ctorigdst", r["ip"], "--ctorigdstport", str(r["port"]),
                "-j", "RETURN"] if r.get("local") else
               ["-s", r["subnet"], "-d", r["ip"], "-p", "tcp", "--dport", str(r["port"]), "-j", "RETURN"]
               for r in remote]
    return {
        CHAIN_FORWARD: [replies, [*src, "-d", str(pool), "-j", "RETURN"], *reached,
                        *([*src, "-d", net, "-j", "DROP"] for net in PRIVATE)],
        CHAIN_INPUT: [replies, [*src, "-j", "DROP"]],
    }


def _iptables(*args: str, check: bool = True) -> str:
    return _run("iptables", "-w", *args, check=check)


def _jumps() -> list[tuple[str, str]]:
    return [("DOCKER-USER", CHAIN_FORWARD), ("DOCKER-USER", CHAIN_MAIL), ("INPUT", CHAIN_INPUT)]


def _has_jump(parent: str, chain: str) -> bool:
    return subprocess.run([_tool("iptables"), "-w", "-C", parent, "-j", chain], env=ENV,
                          capture_output=True).returncode == 0


def firewall_in_place() -> bool:
    return all(_has_jump(parent, chain) for parent, chain in _jumps())


def _host_networks() -> list[tuple[str, ipaddress.IPv4Network]]:
    found = []
    for line in _run("ip", "-4", "-o", "addr", "show").splitlines():
        parts = line.split()
        if len(parts) > 3 and parts[2] == "inet":
            found.append((parts[1], ipaddress.ip_interface(parts[3]).network))
    return found


def firewall_apply() -> dict:
    pool = network_pool()
    # the pool is the workspaces' own: on this machine only their bridges are in it
    clash = [f"{iface} {net}" for iface, net in _host_networks()
             if net.overlaps(pool) and not (iface.startswith("br-") and net.subnet_of(pool))]
    if clash:
        raise HelperError(f"workspaces.network {pool} overlaps networks of this machine: {', '.join(clash)}")
    guarded = mail_guarded()
    rules = {**firewall_rules(pool, list(db_remote_file().values())), CHAIN_MAIL: mail_guard_rules(guarded.values())}
    for chain, chain_rules in rules.items():
        _fill(chain, chain_rules)
    # docker makes DOCKER-USER, and keeps its rules; at boot this runs first
    _iptables("-N", "DOCKER-USER", check=False)
    for parent, chain in _jumps():
        if not _has_jump(parent, chain):
            _iptables("-I", parent, "1", "-j", chain)
    # the clusters that listen for other machines, as they were kept (at boot their storage comes after)
    listening = db_firewall_apply()
    return {"firewall": "on", "pool": str(pool), "chains": [CHAIN_FORWARD, CHAIN_MAIL, CHAIN_INPUT],
            "mail_guarded": sorted(guarded.values()), **({"listening": listening} if listening else {})}


def _fill(chain: str, rules: list[list[str]]) -> None:
    _iptables("-N", chain, check=False)
    _iptables("-F", chain)
    for rule in rules:
        _iptables("-A", chain, *rule)


def _fill_at_once(chains: dict[str, list[list[str]]]) -> None:
    """The chains emptied and filled in one transaction (iptables-restore
    --noflush): never a moment with the old rules gone and the new not there."""
    text = "*filter\n" + "".join(f":{chain} - [0:0]\n" for chain in chains)
    text += "".join(f"-A {chain} {' '.join(rule)}\n" for chain, rules in chains.items() for rule in rules)
    done = subprocess.run([_tool("iptables-restore"), "-w", "--noflush"], env=ENV, input=text + "COMMIT\n",
                          capture_output=True, text=True)
    if done.returncode:
        raise HelperError(f"iptables-restore: {(done.stderr or done.stdout).strip()}")


def db_listen_file() -> dict:
    """DB_LISTEN as it is on disk, only what is right: a cluster's bridge
    with IPv4 addresses, or the bridge of a replica hosted here with the
    address it is reached from and the address and port it goes out to."""
    try:
        data = json.loads(DB_LISTEN.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    found = {}
    for bridge, value in (data.items() if isinstance(data, dict) else ()):
        if machinedb.PUBLISHED.fullmatch(str(bridge)):
            # the dedicated database of an instance, published for its copy on another machine
            try:
                _port, coming, network = machinedb.published(str(bridge), value)
            except machinedb.ClusterError:
                continue
            found[str(bridge)] = {"from": coming, "subnet": network}
            continue
        if not machinedb.BRIDGE.fullmatch(str(bridge)) or not isinstance(value, (list, dict)):
            continue
        try:
            coming, going = machinedb.reached(value)
        except machinedb.ClusterError:
            continue
        if isinstance(value, dict):
            try:
                local = machinedb.local_networks(value)
            except machinedb.ClusterError:
                local = []
            found[str(bridge)] = {"from": coming, "to": [list(pair) for pair in going], **({"local": local} if local else {})}
        else:
            found[str(bridge)] = coming
    return found


def db_firewall_apply() -> dict:
    """The chains of the clusters that listen made again from DB_LISTEN and
    jumped first; with none listening, the chains and their jumps are gone."""
    state = db_listen_file()
    jumps = [("DOCKER-USER", CHAIN_DB), ("INPUT", CHAIN_DB_INPUT)]
    if not state:
        for parent, chain in jumps:
            while _has_jump(parent, chain):
                _iptables("-D", parent, "-j", chain)
            _iptables("-F", chain, check=False)
            _iptables("-X", chain, check=False)
        return state
    try:
        rules = machinedb.firewall_rules(state)
    except machinedb.ClusterError as exc:
        raise HelperError(str(exc)) from None
    _fill_at_once({CHAIN_DB: rules["forward"], CHAIN_DB_INPUT: rules["input"]})
    _iptables("-N", "DOCKER-USER", check=False)
    for parent, chain in jumps:
        if not _has_jump(parent, chain):
            _iptables("-I", parent, "1", "-j", chain)
    return state


def db_firewall() -> dict:
    """The clusters that listen read again (their grants, the machine of
    their replica) and the replicas hosted here, kept in DB_LISTEN for the
    boot, the chains made again; the unit that applies them at every boot
    written when it is not there yet."""
    try:
        state = {**machinedb.firewall_state(), **managedreplica.firewall_state(instance_subnet)}
    except machinedb.ClusterError as exc:
        raise HelperError(str(exc)) from None
    if state:
        DB_LISTEN.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        _write(DB_LISTEN, json.dumps(state, indent=1, sort_keys=True) + "\n", mode=0o600)
        if not (SYSTEMD / FIREWALL_UNIT).is_file():
            firewall("on")
    else:
        DB_LISTEN.unlink(missing_ok=True)
    return db_firewall_apply()


def instance_subnet(workspace: str, instance: str) -> str:
    """The network of the production of an instance of this machine (SUBNET
    of its .env), "" when there is no such instance."""
    if not WORKSPACE_NAME.fullmatch(workspace or "") or not WORKSPACE_NAME.fullmatch(instance or ""):
        return ""
    try:
        roots = storage_roots()
    except HelperError:
        return ""
    for storage in roots:
        project = bound(storage) / workspace / instance / "production"
        try:
            if (project / ".env").is_file() and not project.is_symlink():
                return str(ipaddress.ip_network(_env(project).get("SUBNET", "")))
        except (OSError, ValueError):
            continue
    return ""


def db_remote_file() -> dict[str, dict]:
    """DB_REMOTE as it is on disk, only the entries that are what they should be."""
    try:
        data = json.loads(DB_REMOTE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    found = {}
    for folder, value in (data.items() if isinstance(data, dict) else ()):
        try:
            subnet = str(ipaddress.ip_network(str(value["subnet"])))
            ip = ipaddress.ip_address(str(value["ip"]))
            port = int(value["port"])
        except (KeyError, TypeError, ValueError):
            continue
        if ip.version == 4 and 1 <= port <= 65535 and str(folder).startswith("/"):
            # local: an address of this machine itself (a copy hosted here), which docker forwards to a container
            found[str(folder)] = {"subnet": subnet, "ip": str(ip), "port": port, **({"local": True} if value.get("local") else {})}
    return found


def db_remote_set(folder: Path, entry: dict | None) -> None:
    """The exception of a project written (entry) or taken away (None), and
    the firewall made again when it is on."""
    kept = db_remote_file()
    before = dict(kept)
    if entry is None:
        kept.pop(str(folder), None)
    else:
        kept[str(folder)] = entry
    if kept == before:
        return
    DB_REMOTE.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    _write(DB_REMOTE, json.dumps(kept, indent=1, sort_keys=True) + "\n", mode=0o600)
    if firewall_in_place():
        firewall_apply()


def mail_guard_rules(subnets) -> list[list[str]]:
    """A capturing staging's network: SMTP refused but to itself (its mail
    service, on 1025, is in it), whatever server its Odoo is given."""
    ports = ",".join(str(p) for p in SMTP_PORTS)
    return [["-s", net, "!", "-d", net, "-p", "tcp", "-m", "multiport", "--dports", ports,
             "-j", "REJECT", "--reject-with", "tcp-reset"] for net in sorted(set(subnets))]


def _mail_guard_file() -> dict[str, str]:
    try:
        data = json.loads(MAIL_GUARD.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    found = {}
    for folder, value in (data.items() if isinstance(data, dict) else ()):
        try:
            found[str(folder)] = str(ipaddress.ip_network(str(value)))
        except ValueError:
            continue
    return found


def _capturing_projects(mount: Path) -> dict[str, str]:
    """{production folder: SUBNET} of the instances of a mounted disk whose mail is caught."""
    found = {}
    for meta_file in sorted(mount.glob(f"*/{INSTANCE_FILE}")):
        project = meta_file.parent / "production"
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            if isinstance(meta, dict) and captures_mail(meta, "production"):
                found[str(project)] = str(ipaddress.ip_network(_env(project).get("SUBNET", "")))
        except (OSError, ValueError):
            continue
    return found


def mail_guarded() -> dict[str, str]:
    """The projects whose SMTP is refused, kept in MAIL_GUARD: those of the
    mounted disks read again (a staging made or removed), those of a disk
    or a storage not mounted yet (at boot the firewall comes before them) as
    they were kept; a disk gone or a storage taken out, its entries gone
    (its subnet may be another project's tomorrow)."""
    kept = _mail_guard_file()
    try:
        roots = storage_roots()
    except HelperError:
        return kept
    found: dict[str, str] = {}
    for root in roots:
        base = bound(root)
        disks = base / ".disks"
        if (not disks.is_dir() or disks.is_symlink()) and not _is_mount(base):
            # the storage not bound yet: what was kept on it stays
            found.update({f: n for f, n in kept.items() if Path(f).parent.parent.parent == base})
            continue
        # bound: what is on its disks now, a disk gone taking its entries
        images = [] if disks.is_symlink() or not disks.is_dir() else sorted(disks.glob("*.img"))
        for image in images:
            mount = base / image.stem
            if image.is_symlink() or not WORKSPACE_NAME.fullmatch(image.stem):
                continue
            if mounted(image, mount):
                found.update(_capturing_projects(mount))
            else:
                found.update({f: n for f, n in kept.items() if Path(f).parent.parent == mount})
    if found != kept:
        MAIL_GUARD.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        _write(MAIL_GUARD, json.dumps(found, indent=1, sort_keys=True) + "\n", mode=0o600)
    return found


def mail_guard_apply() -> dict:
    """The SMTP rules of the capturing stagings made again, when one is made
    or removed; the jump only while the firewall is on (create puts it on)."""
    guarded = mail_guarded()
    _fill(CHAIN_MAIL, mail_guard_rules(guarded.values()))
    if _has_jump("DOCKER-USER", CHAIN_FORWARD) and not _has_jump("DOCKER-USER", CHAIN_MAIL):
        _iptables("-I", "DOCKER-USER", "1", "-j", CHAIN_MAIL)
    return {"mail_guarded": sorted(guarded.values())}


def firewall_unit_text() -> str:
    return ("# written by omniacli workspace-helper: the firewall of the workspaces' containers\n"
            "[Unit]\nDescription=OmniaCLI: the workspaces' containers reach the internet only\n"
            "Before=docker.service\n"
            f"[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart={OMNIACLI_COMMAND} workspace-helper firewall apply\n"
            "[Install]\nWantedBy=multi-user.target docker.service\n")


def firewall(mode: str) -> dict:
    if mode == "apply":
        return firewall_apply()
    unit = SYSTEMD / FIREWALL_UNIT
    if mode == "on":
        made = firewall_apply()
        _write(unit, firewall_unit_text())
        _run("systemctl", "daemon-reload")
        _run("systemctl", "enable", FIREWALL_UNIT)
        return {**made, "unit": FIREWALL_UNIT}
    if mode == "off":
        if db_listen_file():
            # its rules are applied by the same unit at every boot: without it the port is open to anyone
            raise HelperError("a cluster of this machine listens for other machines: close its port first "
                              "(Database tab), then the firewall goes off")
        for parent, chain in _jumps():
            while _has_jump(parent, chain):
                _iptables("-D", parent, "-j", chain)
            _iptables("-F", chain, check=False)
            _iptables("-X", chain, check=False)
        MAIL_GUARD.unlink(missing_ok=True)
        if unit.exists():
            _run("systemctl", "disable", FIREWALL_UNIT, check=False)
            unit.unlink()
            _run("systemctl", "daemon-reload")
        return {"firewall": "off"}
    raise HelperError("firewall on, apply or off")


def _taken_subnets() -> list[ipaddress.IPv4Network]:
    """The networks of the workspaces' projects (.env) and of docker."""
    taken = []
    for root in storage_roots():
        for env_file in bound(root).glob("*/*/*/.env"):
            try:
                value = _env(env_file.parent).get("SUBNET", "")
                taken.append(ipaddress.ip_network(value))
            except (OSError, ValueError):
                continue
    # a capturing staging's network, while its rules stand (its disk not mounted)
    taken += [ipaddress.ip_network(net) for net in _mail_guard_file().values()]
    ids = _run("docker", "network", "ls", "-q").split()
    if ids:
        out = _run("docker", "network", "inspect", "-f", "{{range .IPAM.Config}}{{.Subnet}} {{end}}", *ids)
        for value in out.split():
            try:
                taken.append(ipaddress.ip_network(value))
            except ValueError:
                continue
    return [net for net in taken if net.version == 4]


def pick_subnets(count: int) -> list[str]:
    pool = network_pool()
    taken = _taken_subnets() + [net for _, net in _host_networks()]
    found = []
    for subnet in pool.subnets(new_prefix=SUBNET_PREFIX):
        if not any(subnet.overlaps(net) for net in taken):
            found.append(str(subnet))
            taken.append(subnet)
            if len(found) == count:
                return found
    raise HelperError(f"no {count} free /{SUBNET_PREFIX} networks in {pool}")


def _taken_ports() -> set[int]:
    """The ports the instances of every workspace were given (their .env)."""
    taken = set()
    for root in storage_roots():
        for env_file in bound(root).glob("*/*/*/.env"):
            try:
                text = env_file.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                key, _, value = line.partition("=")
                if key in ("HTTP_PORT", "WEBSOCKET_PORT") and value.isdigit():
                    taken.add(int(value))
    return taken


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def pick_ports(count: int) -> list[int]:
    taken, found = _taken_ports(), []
    for port in PORTS:
        if port not in taken and _free(port):
            found.append(port)
            if len(found) == count:
                return found
    raise HelperError(f"no {count} free ports in {PORTS.start}-{PORTS.stop - 1}")


def _template_in(folder: Path) -> stacktemplates.Template:
    _safe_folder(folder, "the template")
    _, uid, groups = caller()
    for file_name in stacktemplates.FILES:
        st = (folder / file_name).lstat()
        if stat.S_ISLNK(st.st_mode) or (uid != 0 and composehelper._writable(st, uid, groups)):
            raise HelperError(f"the template's {file_name} is a link or writable by the caller")
    try:
        return stacktemplates.load_template(folder)
    except stacktemplates.TemplateError as exc:
        raise HelperError(str(exc)) from None


def template_named(name: str) -> stacktemplates.Template:
    """The package's template or the one installed from the download
    server (templatestore.py), the higher version."""
    if not stacktemplates.NAME.fullmatch(name):
        raise HelperError(f"{name!r} is not a template name")
    found = None
    for base in (TEMPLATES, templatestore.INSTALLED):
        folder = base / name
        if folder.is_symlink() or not folder.is_dir():
            continue
        template = _template_in(folder)
        if found is None or stacktemplates.version_key(template.version) > stacktemplates.version_key(found.version):
            found = template
    if found is None:
        raise HelperError(f"no template {name} in {TEMPLATES} or {templatestore.INSTALLED}")
    return found


def template_install(name: str) -> dict:
    try:
        return templatestore.install(name)
    except templatestore.StoreError as exc:
        raise HelperError(str(exc)) from None


def _owner(spec: str) -> tuple[int, int]:
    uid, _, gid = spec.partition(":")
    return int(uid), int(gid)


# the key of create's values that says which roles it makes, never a template's
ROLES_KEY = "_roles"
# the key that asks the mail of the production caught (a staging's,
# UX-008 §5.3), kept in instance.json: what conf and the page read there
MAIL_KEY = "_mail_capture"
# the database of another machine an instance of a remote template uses
# (AD-003): {"host", "port", "user", "password", "database", "ca"}
REMOTE_KEY = "_remote"
REMOTE_HOST = re.compile(r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")
# a digit first too: the name of an environment on a linked machine's cluster (machinedb.env_name)
REMOTE_NAME = re.compile(r"[a-z0-9_][a-z0-9_]{0,62}")
# no quote, blank, $ or backslash: it goes into .env as it is
REMOTE_PASSWORD = re.compile(r"[A-Za-z0-9_.~!@#%^&*+=:,/-]{8,128}")
REMOTE_CA_MAX = 64 * 1024
REMOTE_CA_FILE = "db-ca.pem"
# "_remote": "-" in the values: the connection is read on the input
REMOTE_STDIN = "-"
# the firewall exception of a project's read replica: its entry of DB_REMOTE, the project's folder and this
REPLICA_SUFFIX = "#replica"
REMOTE_INPUT = 128 * 1024
# an instance of the machine's cluster that reads from the cluster's replica on another machine
# (AD-004): instance.json's key (host:port; never shown to a workspace's user), the CA the
# replica's certificate is verified against, the override that gives it to Odoo's container
REPLICA_META = "replica_read"
REPLICA_CA_FILE = "db-replica-ca.pem"
REPLICA_OVERRIDE = managedreplica.OVERRIDE
REPLICA_OVERRIDE_MARK = managedreplica.OVERRIDE_MARK
ODOO_WITH_REPLICA = 18
# what an instance was before its cluster was replaced by its replica (a promotion): kept beside it
PROMOTED = ".before-promotion"


def remote_spec(value) -> dict:
    """The connection to a database of another machine, checked: a host that
    is not this machine, a port, a role and a database by their names, a
    password .env can hold, and the CA its certificate is verified against
    (PEM). HelperError says what is wrong, never the password."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise HelperError(f"{REMOTE_KEY} is not JSON") from None
    if not isinstance(value, dict):
        raise HelperError(f"{REMOTE_KEY}: the host, port, user, password, database and ca of the database")
    host = str(value.get("host") or "").strip().lower()
    if not REMOTE_HOST.fullmatch(host):
        raise HelperError(f"{host!r} is not a host name or an address")
    try:
        if ipaddress.ip_address(host).is_loopback:
            raise HelperError("a database of another machine is not on this one (127.0.0.1): use a dedicated one")
    except ValueError:
        if host == "localhost":
            raise HelperError("a database of another machine is not on this one (localhost): use a dedicated one") from None
    port = str(value.get("port") or "5432").strip()
    if not (port.isdigit() and 1 <= int(port) <= 65535):
        raise HelperError(f"{port!r} is not a port")
    user, database = str(value.get("user") or "").strip(), str(value.get("database") or "").strip()
    for what, name in (("role", user), ("database", database)):
        if not REMOTE_NAME.fullmatch(name):
            raise HelperError(f"{name!r} is not the name of a {what} (a-z, 0-9, _)")
    if user in ("postgres",):
        raise HelperError("the superuser postgres is not for Odoo: give a role of this instance only")
    password = str(value.get("password") or "")
    if not REMOTE_PASSWORD.fullmatch(password):
        raise HelperError("the password of the role: 8 to 128 of letters, digits and _.~!@#%^&*+=:,/- (no blank, quote or $)")
    ca = str(value.get("ca") or "").strip()
    lines = ca.splitlines()
    if (len(ca) > REMOTE_CA_MAX or not lines or lines[0] != "-----BEGIN CERTIFICATE-----"
            or lines[-1] != "-----END CERTIFICATE-----"
            or any(not re.fullmatch(r"[A-Za-z0-9+/=]{1,76}|-----(BEGIN|END) CERTIFICATE-----", line) for line in lines)):
        raise HelperError("the CA of the database's certificate: one or more certificates in PEM "
                          "(-----BEGIN CERTIFICATE----- ...), without it the server is not verified")
    spec = {"host": host, "port": int(port), "user": user, "password": password, "database": database, "ca": ca + "\n"}
    # its read replica (AD-004), when who runs the database gives one: reached as the database is, the
    # same role and CA; Odoo reads from it (18 and later)
    replica = str(value.get("replica_host") or "").strip().lower()
    if replica:
        if not REMOTE_HOST.fullmatch(replica) or replica in ("localhost", "127.0.0.1", "::1"):
            raise HelperError(f"{replica!r} is not the host of a replica on another machine")
        replica_port = str(value.get("replica_port") or port).strip()
        if not (replica_port.isdigit() and 1 <= int(replica_port) <= 65535):
            raise HelperError(f"{replica_port!r} is not a port")
        if (replica, int(replica_port)) == (host, int(port)):
            raise HelperError("the replica is the database itself: give the address of its replica, or none")
        spec["replica"] = (replica, int(replica_port))
        # as they were given too: a spec checked once (the app) is checked again by the helper, as it is
        spec["replica_host"], spec["replica_port"] = replica, int(replica_port)
    # the cluster of a linked OmniaCLI (the app asked it for this database): which link and which
    # PostgreSQL, for the pages and for the removal, which asks that machine to take it back
    link = str(value.get("link") or "").strip()
    if link:
        major = str(value.get("major") or "").strip()
        if not machinedb.LINK_NAME.fullmatch(link) or not re.fullmatch(r"\d{1,3}", major):
            raise HelperError("the link and the PostgreSQL of a linked machine's cluster are not what they should be")
        if replica:
            raise HelperError("a database on a linked machine's cluster has no read replica")
        spec["link"], spec["major"] = link, major
    return spec


def _private_address(host: str) -> str | None:
    """The IPv4 address of host when it is a private one (the firewall of the
    workspaces stops those): its exception names it. None: a public one, or
    a name with none."""
    try:
        found = sorted({info[4][0] for info in socket.getaddrinfo(host, None, socket.AF_INET)})
    except (OSError, UnicodeError):
        return None
    for address in found:
        ip = ipaddress.ip_address(address)
        if any(ip in ipaddress.ip_network(net) for net in PRIVATE):
            return address
    return None
MAIL_META = "mail_capture"
# and handed to mailboxes its Odoo reads (UX-008 §5.4): a staging made from a
# template with the inbox service; one of before catches the outgoing mail only
INBOX_META = "mail_inbox"


def captures_mail(meta: dict, role: str) -> bool:
    """The role's mail is caught in its project (a staging's production)."""
    return role == "production" and meta.get(MAIL_META) is True


def receives_mail(meta: dict, role: str) -> bool:
    """The role's mail is caught and handed to mailboxes its Odoo can read."""
    return captures_mail(meta, role) and meta.get(INBOX_META) is True


# the first Odoo with an alias domain (mail.alias.domain), which the mailboxes of a staging need
# (odoo/mail_inbox.py): the templates of an older one have no service inbox
INBOX_FROM = 17


def without_alias_domain(meta: dict) -> int | None:
    """The Odoo of an instance's template when it is older than INBOX_FROM
    (its staging catches the outgoing mail only), else None."""
    major = odoo_major(str(meta.get("template") or ""))
    return major if major is not None and major < INBOX_FROM else None


def replica_refused(template_name: str, remote: dict | None) -> str | None:
    """Why a read replica given with a database of another machine is
    refused for the template's Odoo (db_replica_host, Odoo 18 and later),
    or None."""
    major = odoo_major(template_name)
    # a template whose name says no Odoo (a downloaded one of its own) is left to what it says
    if remote and remote.get("replica") and major is not None and major < ODOO_WITH_REPLICA:
        return (f"Odoo {major} cannot read from a replica: it can from {ODOO_WITH_REPLICA} on "
                "(db_replica_host). Leave the replica empty")
    return None


def create(name: str, storage: str, instance: str, template_name: str, values_json: str) -> dict:
    image, mount = workspace(name, storage)
    _name("instance", instance)
    if not mounted(image, mount):
        raise HelperError(f"the disk of workspace {name} is not mounted: workspace-helper disk first")
    folder = mount / instance
    if folder.exists() or folder.is_symlink():
        raise HelperError(f"{folder} exists already")
    template = template_named(template_name)
    if not (SYSTEMD / slice_name(name)).is_file():
        raise HelperError(f"workspace {name} has no slice: workspace-helper limits first")
    try:
        given = json.loads(values_json)
    except ValueError:
        raise HelperError("the values are not JSON") from None
    if not isinstance(given, dict):
        raise HelperError("the values are not a JSON object")
    # the roles made: production alone (an instance with its stagings), or
    # both as before; not given, both (an OmniaCLI of before asking)
    roles = given.pop(ROLES_KEY, list(ROLES))
    if not isinstance(roles, list) or "production" not in roles or not set(roles) <= set(ROLES):
        raise HelperError(f"{ROLES_KEY} is a list of roles with production in it ({', '.join(ROLES)})")
    roles = [r for r in ROLES if r in roles]
    # asked of a template of before (no service to catch it): not caught, said in the answer
    remote = given.pop(REMOTE_KEY, None)
    if remote == REMOTE_STDIN:
        # its password and its CA come on the input: on no command line, in no log
        remote = sys.stdin.read(REMOTE_INPUT + 1)
        if len(remote) > REMOTE_INPUT:
            raise HelperError(f"{REMOTE_KEY} on the input is too long")
    if template.on_remote:
        remote = remote_spec(remote)
        if replica_refused(template.name, remote):
            raise HelperError(replica_refused(template.name, remote))
    elif remote is not None:
        raise HelperError(f"{template.name} has its own database: {REMOTE_KEY} is for a template of a database "
                          "of another machine")
    mail = given.pop(MAIL_KEY, False) in (True, "1", "true") and template.captures_mail()
    inbox = mail and template.receives_mail()
    # a staging of an Enterprise production is Enterprise from its first start (UX-012)
    odoo_enterprise = _enterprise_asked(template, given.pop(enterprise.CREATE_KEY, ""))
    try:
        values = template.values({str(k): str(v) for k, v in given.items()})
    except stacktemplates.TemplateError as exc:
        raise HelperError(str(exc)) from None
    # on the machine's cluster (AD-001): the production alone, a name unique
    # on the cluster, its socket; the cluster must be there already
    machine = None
    if template.on_machine:
        if roles != ["production"]:
            raise HelperError("an instance on the machine's cluster has its production only: its copies are stagings")
        try:
            machine = {"socket": str(machinedb.socket_folder(template.postgres)),
                       "role": machinedb.env_name(name, instance), "major": template.postgres}
        except machinedb.ClusterError as exc:
            raise HelperError(str(exc)) from None
    if remote and roles != ["production"]:
        raise HelperError("an instance on a database of another machine has its production only")
    _, caller_uid, _ = caller()
    ports = pick_ports(2 * len(roles))
    if not firewall_in_place():
        firewall("on")
    subnets = pick_subnets(len(roles))
    database = machine["role"] if machine else remote["database"] if remote else instance.replace("-", "_")
    made = {"workspace": name, "instance": instance, "template": template.name, "version": template.version,
            "database": database, "odoo_service": template.odoo_service, "db_service": template.db_service,
            "values": values, "roles": {}, "mail_capture": mail, "mail_inbox": inbox,
            "database_mode": template.database}
    meta = {"template": template.name, "version": template.version, "database": database,
            **({MAIL_META: True} if mail else {}), **({INBOX_META: True} if inbox else {}),
            # where its database is, for the pages (no password here: the file is read by OmniaCLI's user)
            **({"database_on": f"{remote['host']}:{remote['port']}"} if remote else {}),
            **({"replica_on": "%s:%d" % remote["replica"]} if remote and remote.get("replica") else {}),
            # the cluster of a linked OmniaCLI: which link and PostgreSQL (the pages name it, the removal asks it)
            **({"database_link": remote["link"], "database_major": remote["major"]}
               if remote and remote.get("link") else {}),
            **({enterprise.META: odoo_enterprise} if odoo_enterprise else {})}
    if odoo_enterprise:
        made["enterprise"] = odoo_enterprise["series"]
    folder.mkdir(mode=0o755)
    on_cluster = False
    try:
        _write(folder / INSTANCE_FILE, json.dumps(meta) + "\n")
        for index, role in enumerate(roles):
            project = folder / role
            project.mkdir(mode=0o755)
            # the customer's repositories of the role: OmniaCLI's user clones there
            addons = project / "addons"
            addons.mkdir(mode=0o755)
            os.chown(addons, caller_uid, -1)
            http_port, ws_port = ports[2 * index], ports[2 * index + 1]
            env = {**values, "PROJECT": f"{name}-{instance}-{role}", "HTTP_PORT": str(http_port),
                   "WEBSOCKET_PORT": str(ws_port), "DB_PASSWORD": stacktemplates.new_password(),
                   "DB_SUPER_PASSWORD": stacktemplates.new_password(),
                   "SUBNET": subnets[index], "SLICE": slice_name(name)}
            if machine:
                env.update({"DB_CLUSTER": machine["major"], "DB_USER": machine["role"], "DB_SOCKET": machine["socket"]})
            if remote:
                env.update({"DB_REMOTE": "1", "DB_HOST": remote["host"], "DB_PORT": str(remote["port"]),
                            "DB_USER": remote["user"], "DB_PASSWORD": remote["password"]})
            caught = captures_mail(meta, role)
            if caught:
                # read by docker compose in .env: the template's mail service runs
                env["COMPOSE_PROFILES"] = stacktemplates.MAIL_PROFILE
            _write(project / "compose.yaml", template.compose)
            if odoo_enterprise:
                _write_override(project, None, None)
            _write(project / ".env", stacktemplates.env_text(env), mode=0o600)
            # what postgres:17 runs at its first start, for a template that mounts it
            (project / "dbinit").mkdir(mode=0o755)
            _write(project / "dbinit" / "odoo-role.sh", DB_ROLE_SCRIPT)
            (project / "config").mkdir(mode=0o755)
            _write(project / "config" / "odoo.conf",
                   stacktemplates.conf_text(template, database, stacktemplates.new_password(), [],
                                            mail_capture=caught,
                                            db_template=machinedb.template_name(name) if machine else "",
                                            replica=remote.get("replica") if remote else None,
                                            odoo_enterprise=bool(odoo_enterprise)),
                   mode=0o644)
            if remote:
                # the CA Odoo verifies the database's certificate against (PGSSLROOTCERT)
                _write(project / "config" / REMOTE_CA_FILE, remote["ca"], mode=0o644)
                private = _private_address(remote["host"])
                if private:
                    db_remote_set(project, {"subnet": subnets[index], "ip": private, "port": remote["port"]})
                if remote.get("replica"):
                    behind = _private_address(remote["replica"][0])
                    if behind:
                        db_remote_set(Path(str(project) + REPLICA_SUFFIX),
                                      {"subnet": subnets[index], "ip": behind, "port": remote["replica"][1]})
                made["remote"] = {"host": remote["host"], "port": remote["port"], "user": remote["user"],
                                  "database": remote["database"], "private": private}
            for item in template.data:
                path = project / item["path"]
                path.mkdir(mode=0o755, parents=True)
                if item["owner"]:
                    os.chown(path, *_owner(item["owner"]))
            made["roles"][role] = {"folder": str(project), "http_port": http_port, "websocket_port": ws_port,
                                   "subnet": subnets[index]}
            if machine:
                # its role, its tablespace on this disk, its database, its line of pg_hba
                on_cluster = True
                made["machine"] = machinedb.env_add(machine["major"], name, mount, instance, env["DB_PASSWORD"])
    except BaseException:
        if on_cluster:
            try:
                machinedb.env_remove(machine["major"], machine["role"])
            except machinedb.ClusterError:
                pass
        for role in roles:
            db_remote_set(folder / role, None)
            db_remote_set(Path(str(folder / role) + REPLICA_SUFFIX), None)
        shutil.rmtree(folder, ignore_errors=True)
        raise
    if mail:
        # its network may not reach SMTP outside itself, from now and at every boot
        made["mail_guarded"] = mail_guard_apply()["mail_guarded"]
    return made


def _replica_of(meta: dict) -> tuple[str, int] | None:
    """(host, port) of the read replica an instance was given at its creation
    (instance.json), or of the replica of the machine's cluster it reads from
    (REPLICA_META), or None."""
    host, sep, port = str(meta.get("replica_on") or meta.get(REPLICA_META) or "").rpartition(":")
    if sep and REMOTE_HOST.fullmatch(host) and port.isdigit() and 1 <= int(port) <= 65535:
        return host, int(port)
    return None


def _admin_password(conf: str) -> str | None:
    for line in conf.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "admin_passwd" and value.strip():
            return value.strip()
    return None


def role_state(project: Path) -> dict:
    """What conf gave the role last: repositories, parameters, debug."""
    try:
        data = json.loads((project / ROLE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    return {"repositories": list(data.get("repositories") or []), "parameters": dict(data.get("parameters") or {}),
            "debug": bool(data.get("debug")), "extra": dict(data.get("extra") or {})}


def conf(name: str, storage: str, instance: str, role: str, spec_json: str) -> dict:
    image, mount = workspace(name, storage)
    _name("instance", instance)
    if role not in ROLES:
        raise HelperError(f"the role is {' or '.join(ROLES)}")
    if not mounted(image, mount):
        raise HelperError(f"the disk of workspace {name} is not mounted")
    folder = mount / instance
    try:
        meta = json.loads((folder / INSTANCE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HelperError(f"{folder} holds no {INSTANCE_FILE}: not an instance made by this helper") from None
    try:
        spec = json.loads(spec_json)
    except ValueError:
        raise HelperError("the configuration is not JSON") from None
    if not isinstance(spec, dict):
        raise HelperError("the configuration is a JSON object {repositories, parameters, debug, bounds}")
    project = folder / role
    state = role_state(project)
    repos = spec.get("repositories", state["repositories"])
    if not isinstance(repos, list) or not all(isinstance(r, str) and REPOSITORY.fullmatch(r) for r in repos):
        raise HelperError("the repositories are a list of names (a-z A-Z 0-9 . _ -)")
    repos = list(dict.fromkeys(repos))
    for repo in repos:
        path = project / "addons" / repo
        if path.is_symlink() or not path.is_dir():
            raise HelperError(f"no repository {repo} in {project / 'addons'}")
    template = template_named(str(meta.get("template") or ""))
    given = spec.get("parameters", state["parameters"])
    bounds = spec.get("bounds") or {}
    if not isinstance(given, dict) or not isinstance(bounds, dict):
        raise HelperError("the parameters and the bounds are JSON objects")
    try:
        bounds = {str(k): (int(v[0]), int(v[1])) for k, v in bounds.items()}
    except (TypeError, ValueError, IndexError):
        raise HelperError("the bounds are {name: [min, max]}") from None
    # the parameters given are the whole set (the others take their default);
    # not given, those saved before that this template still has
    saved = {k: v for k, v in state["parameters"].items() if template.parameter(k) is not None}
    try:
        values = template.parameter_values(given if "parameters" in spec else saved, bounds)
    except stacktemplates.TemplateError as exc:
        raise HelperError(str(exc)) from None
    debug = bool(spec.get("debug", state["debug"]))
    # the other options (the Extended view): the whole set when given, else
    # those saved before that this template still lets set
    try:
        extra = template.extra_options(spec["extra"]) if "extra" in spec else \
            {k: v for k, v in state["extra"].items() if k not in template.locked_options()}
    except stacktemplates.TemplateError as exc:
        raise HelperError(str(exc)) from None
    compose_changed = _ensure_pg_env(project)
    conf_file = project / "config" / "odoo.conf"
    try:
        password = _admin_password(conf_file.read_text(encoding="utf-8"))
    except OSError:
        password = None
    # a staging's production: its mail stays caught, whatever the parameters say
    caught = captures_mail(meta, role)
    text = stacktemplates.conf_text(template, str(meta.get("database") or ""),
                                    password or stacktemplates.new_password(), repos, values, debug, extra,
                                    mail_capture=caught,
                                    db_template=machinedb.template_name(name) if template.on_machine else "",
                                    replica=_replica_of(meta), replica_verified=bool(meta.get(REPLICA_META)),
                                    odoo_enterprise=enterprise.of_meta(meta) is not None)
    _write(conf_file, text, mode=0o644)
    kept = {k: v for k, v in values.items() if v != template.parameter(k).default
            and not (caught and k in stacktemplates.MAIL_PARAMETERS)}
    if caught:
        # not kept: its odoo.conf has the mail service's, and so has the page
        extra = {k: v for k, v in extra.items() if k not in stacktemplates.MAIL_PARAMETERS}
    _write(project / ROLE_FILE, json.dumps({"repositories": repos, "parameters": kept, "debug": debug,
                                            **({"extra": extra} if extra else {})}) + "\n")
    return {"workspace": name, "instance": instance, "role": role, "repositories": repos,
            "parameters": {k: ("" if template.parameter(k).type == "password" and v else v) for k, v in values.items()},
            "debug": debug, "extra": sorted(extra), "template": template.name, "version": template.version,
            "mail_capture": caught, "enterprise": (enterprise.of_meta(meta) or {}).get("series", ""),
            "compose": "the database connection for commands typed in the container added" if compose_changed else ""}


# an instance made before the templates gave PG* to the Odoo service: a command
# typed in its container (odoo shell, psql) looked for a database on a local socket
_ENTRYPOINT_DB = re.compile(r"(?m)^(?P<indent>[ \t]+)HOST: db\n(?P=indent)PORT: \"5432\"\n(?P=indent)USER: odoo\n"
                            r"(?P=indent)PASSWORD: \$\{DB_PASSWORD\}\n")


def _ensure_pg_env(project: Path) -> bool:
    """PGHOST, PGPORT, PGUSER and PGPASSWORD beside the HOST... the image's
    entrypoint reads, in the project's compose.yaml, when it has none (taken
    by the containers at the next restart, which runs up -d); True: added."""
    path = project / "compose.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    found = _ENTRYPOINT_DB.search(text)
    if "PGHOST:" in text or found is None:
        return False
    indent = found["indent"]
    added = "".join(f"{indent}{line}\n" for line in ("PGHOST: db", 'PGPORT: "5432"', "PGUSER: odoo",
                                                     "PGPASSWORD: ${DB_PASSWORD}"))
    info = path.stat()
    _write(path, text[:found.end()] + added + text[found.end():], mode=info.st_mode & 0o777,
           uid=info.st_uid, gid=info.st_gid)
    return True


def remove(name: str, storage: str, instance: str) -> dict:
    image, mount = workspace(name, storage)
    _name("instance", instance)
    if not mounted(image, mount):
        raise HelperError(f"the disk of workspace {name} is not mounted")
    folder = mount / instance
    if folder.is_symlink() or not folder.is_dir():
        raise HelperError(f"no instance {instance} in workspace {name}")
    dropped = []
    coming = folder / "production" / machinedb.DEDICATED_FOLDER / managedreplica.BACK_FOLDER
    returning = (coming / "compose.yaml").is_file() and not coming.is_symlink()
    if returning:
        # its database was coming back from its promoted copy (UX-011 §A): the PostgreSQL that followed it goes first
        code = subprocess.run(machinedb.compose(coming, "down", "-v", "--remove-orphans"), env=ENV, cwd=coming).returncode
        if code:
            raise HelperError(f"docker compose down of {coming} failed ({code}): nothing removed")
    for role in ROLES:
        project = folder / role
        if any((project / f).is_file() for f in composehelper.COMPOSE_FILES):
            code = subprocess.run(composehelper.compose(project, "down", "-v", "--remove-orphans"),
                                  env=ENV, cwd=project).returncode
            if code:
                raise HelperError(f"docker compose down of {project} failed ({code}): nothing removed")
        # on the machine's cluster: its database, its role, its line of pg_hba
        env = composehelper.on_cluster(project)
        if env is not None:
            try:
                dropped.append(machinedb.env_remove(env["major"], env["role"]))
            except machinedb.ClusterError as exc:
                raise HelperError(f"{exc}: the instance stays, its containers stopped; remove it again "
                                  "once the machine's cluster answers") from None
    # a database of another machine is not this machine's to drop: it stays there, said in the answer
    left = [f"{r['host']}:{r['port']}" for r in (composehelper.on_remote(folder / role) for role in ROLES) if r]
    for role in ROLES:
        db_remote_set(folder / role, None)
        db_remote_set(Path(str(folder / role) + REPLICA_SUFFIX), None)
    shutil.rmtree(folder)
    # what it held goes back to the real disk now, not at the host's next weekly trim
    trimmed = trim(image, mount)
    # its network is free again: no SMTP rule left on it
    if _mail_guard_file():
        mail_guard_apply()
    if returning:
        # the network its database came back on is gone: the firewall forgets it
        db_firewall()
    return {"workspace": name, "instance": instance, "removed": str(folder), "trimmed": trimmed,
            **({"machine": dropped} if dropped else {}), **({"database_left_on": left} if left else {})}


def _env(project: Path) -> dict[str, str]:
    values = {}
    for line in (project / ".env").read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


def instances_on_disk(mount: Path) -> list[str]:
    """The instances of a mounted workspace: <instance>/production/.env (its
    test, <instance>/test/.env, is there or not: roles_on_disk)."""
    return sorted(p.name for p in mount.iterdir()
                  if WORKSPACE_NAME.fullmatch(p.name) and not p.is_symlink() and p.is_dir()
                  and (p / "production" / ".env").is_file())


def roles_on_disk(mount: Path, instances: list[str]) -> dict[str, tuple[str, ...]]:
    return {i: tuple(role for role in ROLES if (mount / i / role / ".env").is_file()) for i in instances}


def web_spec(spec_json: str, instances: list[str]) -> dict:
    """The names asked for, checked against what is on the disk."""
    try:
        spec = json.loads(spec_json)
    except ValueError:
        raise HelperError("the names are not JSON") from None
    if not isinstance(spec, dict):
        raise HelperError("the names are not a JSON object")
    domain = str(spec.get("domain") or "")
    if domain and not DOMAIN_NAME.fullmatch(domain):
        raise HelperError(f"{domain!r} is not a domain name")
    main = str(spec.get("main") or "")
    if main and main not in instances:
        raise HelperError(f"{main!r} is not an instance of the workspace")
    subdomains = spec.get("subdomains") or {}
    if not isinstance(subdomains, dict):
        raise HelperError("the subdomains are not a JSON object")
    for sub, target in subdomains.items():
        if not SUBDOMAIN.fullmatch(str(sub)) or sub == TEST_LABEL or sub in instances:
            raise HelperError(f"{sub!r} is not a subdomain the workspace may have")
        if target not in instances:
            raise HelperError(f"subdomain {sub}: {target!r} is not an instance of the workspace")
    certificate = str(spec.get("certificate") or "letsencrypt")
    if certificate not in CERTIFICATES:
        raise HelperError(f"the certificate is one of {', '.join(CERTIFICATES)}")
    # the other names of an environment (UX-008 §6.1): host names only, no
    # wildcard, checked as the service checks them
    try:
        aliases, redirects = config.parse_addresses("names", spec.get("aliases"), spec.get("redirects"), instances)
    except ConfigError as exc:
        raise HelperError(str(exc)) from None
    # the environments published through a relay (relay.py): {instance: [roles]}
    relayed = spec.get("relayed") or {}
    if not isinstance(relayed, dict) or any(not isinstance(r, list) or any(x not in ROLES for x in r)
                                            for r in relayed.values()):
        raise HelperError("relayed: {instance: [production, test]} of the workspace's instances")
    return {"domain": domain, "main": main, "subdomains": {str(k): str(v) for k, v in subdomains.items()},
            "certificate": certificate, "aliases": aliases, "redirects": redirects,
            # an instance not on the disk (being made, or gone) has no site: left out
            "relayed": {str(i): [str(x) for x in r] for i, r in relayed.items() if r and i in instances}}


def _addresses(sites: list[dict], spec: dict) -> tuple[list[dict], list[str]]:
    """The aliases put in their sites (site["aliases"]), and the redirects
    grouped by the name they go to: ([{names, to}], notes). An environment
    not on the disk has none; a redirect to a name its environment no longer
    has goes to its first name, and a note says so."""
    generated = {n for site in sites for n in site["names"]}
    groups: dict[str, list[str]] = {}
    notes = []
    for site in sites:
        instance, role = site["instance"], site["role"]
        site["aliases"] = list(spec["aliases"].get(instance, {}).get(role, []))
        for name in site["aliases"]:
            if name in generated:
                raise HelperError(f"{name} is a name of the workspace already: it is no other name")
        own = [*site["names"], *site["aliases"]]
        for name, to in spec["redirects"].get(instance, {}).get(role, {}).items():
            if name in generated:
                raise HelperError(f"{name} is a name of the workspace already: it is no redirect")
            if to not in own:
                notes.append(f"the redirect {name} went to {to}, no name of {config.environment_label(instance, role)} now: it goes to {own[0]}")
                to = own[0]
            groups.setdefault(to, []).append(name)
    taken = {}
    for site in sites:
        for name in site["aliases"]:
            if name in taken:
                raise HelperError(f"{name} is a name of {taken[name]} and of {config.environment_label(site['instance'], site['role'])}")
            taken[name] = config.environment_label(site['instance'], site['role'])
    for names in groups.values():
        for name in names:
            if name in taken:
                raise HelperError(f"{name} is a name of {taken[name]} and a redirect")
            taken[name] = "a redirect"
    return [{"names": names, "to": to} for to, names in groups.items()], notes


def _main_own_names(sites: list[dict], spec: dict, redirects: list[dict]) -> list[dict]:
    """The main instance's own names of before (<main>.<domain>,
    <main>.test.<domain>: what it answered on until the workspace's domain
    became its address): they send to its address with a 301, so a bookmark,
    a DNS record or an integration made then keeps working. Not a name in use."""
    main = spec.get("main") or ""
    used = {n for site in sites for n in (*site["names"], *site.get("aliases", ()))}
    used |= {n for r in redirects for n in r["names"]}
    out = []
    for site in sites:
        if site["instance"] != main or not site["names"]:
            continue
        own = f"{main}.{site['names'][0]}"
        if own not in used and len(own) <= 253:
            out.append({"names": [own], "to": site["names"][0]})
    return out


def _sites(mount: Path, spec: dict, instances: list[str]) -> list[dict]:
    try:
        sites = web_names(spec["domain"], instances, spec["main"], spec["subdomains"], roles_on_disk(mount, instances))
    except ConfigError as exc:
        raise HelperError(str(exc)) from None
    for site in sites:
        project = mount / site["instance"] / site["role"]
        env = _env(project)
        try:
            conf = (project / "config" / "odoo.conf").read_text(encoding="utf-8")
        except OSError:
            conf = ""
        if not (env.get("HTTP_PORT", "").isdigit() and env.get("WEBSOCKET_PORT", "").isdigit()):
            raise HelperError(f"{project}/.env has no HTTP_PORT or WEBSOCKET_PORT")
        site["http_port"] = int(env["HTTP_PORT"])
        site["websocket_port"] = int(env["WEBSOCKET_PORT"]) if workspaceweb.workers(conf) else None
        # an update without stopping serves the environment from another container for its time: kept
        switched = _upstream_of(project)
        if switched:
            site["http_port"] = switched["http_port"]
            if site["websocket_port"] is not None:
                site["websocket_port"] = switched.get("websocket_port") or site["websocket_port"]
    relayed = spec.get("relayed") or {}
    if any(relayed.values()):
        port = relay_local_port(_name("workspace", spec.get("workspace") or ""))
        for site in sites:
            if site["role"] in relayed.get(site["instance"], ()):
                site["relay_port"] = port
    return sites


def _upstream_of(project: Path) -> dict | None:
    """The ports an update without stopping switched the environment's nginx
    to (upstream): {"http_port", "websocket_port"}, or None."""
    path = project / workspaceweb.UPSTREAM_FILE
    if path.is_symlink() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ports = [data.get("http_port"), data.get("websocket_port")] if isinstance(data, dict) else []
    if not ports or not _a_port(ports[0]) or (ports[1] is not None and not _a_port(ports[1])):
        return None
    return {"http_port": ports[0], "websocket_port": ports[1]}


def _a_port(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1024 <= value <= 65535


def _listens(port: int) -> bool:
    """Something answers TCP on 127.0.0.1:port: nginx is never sent to a port where nothing listens."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return True
    except OSError:
        return False


def upstream(name: str, storage: str, instance: str, role: str, http_port: str, websocket_port: str) -> dict:
    """The environment's server block of the workspace's site proxying to
    other ports of this machine (a container of an update without stopping,
    zerodowntime.py), or back to its own (-): nginx -t and a reload, which
    lets the requests under way end where they are; the ports kept in
    <project>/upstream.json, so the site written again keeps them. A port
    where nothing answers is refused."""
    with _site_lock(name):
        return _upstream(name, storage, instance, role, http_port, websocket_port)


def _upstream(name: str, storage: str, instance: str, role: str, http_port: str, websocket_port: str) -> dict:
    image, mount = workspace(name, storage)
    _name("instance", instance)
    if role not in ROLES:
        raise HelperError(f"{role!r} is not production or test")
    if not mounted(image, mount):
        raise HelperError(f"the disk of workspace {name} is not mounted")
    project = mount / instance / role
    if project.is_symlink() or not (project / ".env").is_file():
        raise HelperError(f"no environment {instance} {role} in workspace {name}")
    path = NGINX_SITES / f"omniacli-{name}.conf"
    if path.is_symlink() or not path.is_file():
        raise HelperError(f"workspace {name} has no site: its names are not published")
    env = _env(project)
    if not (env.get("HTTP_PORT", "").isdigit() and env.get("WEBSOCKET_PORT", "").isdigit()):
        raise HelperError(f"{project}/.env has no HTTP_PORT or WEBSOCKET_PORT")
    own = {"http_port": int(env["HTTP_PORT"]), "websocket_port": int(env["WEBSOCKET_PORT"])}
    now = _upstream_of(project) or own
    if http_port == "-":
        then = own
    else:
        if not http_port.isdigit() or not _a_port(int(http_port)) \
                or not (websocket_port == "-" or (websocket_port.isdigit() and _a_port(int(websocket_port)))):
            raise HelperError("usage: upstream <workspace> <storage> <instance> <role> <http port>|- <websocket port>|-")
        then = {"http_port": int(http_port), "websocket_port": int(websocket_port) if websocket_port != "-" else None}
        if then["http_port"] in own.values() or then["websocket_port"] in own.values():
            raise HelperError("those are the environment's own ports: - goes back to them")
    # the websocket port is the site's with workers only (threaded, /websocket goes to the http port)
    try:
        threaded = not workspaceweb.workers((project / "config" / "odoo.conf").read_text(encoding="utf-8"))
    except OSError:
        threaded = True
    for port in {then["http_port"], None if threaded else then["websocket_port"]} - {None}:
        if not _listens(port):
            raise HelperError(f"nothing answers on 127.0.0.1:{port}: nginx is not sent there")
    ports = {now["http_port"]: then["http_port"]}
    if now.get("websocket_port") and then.get("websocket_port"):
        ports[now["websocket_port"]] = then["websocket_port"]
    before = path.read_text(encoding="utf-8")
    try:
        text = workspaceweb.switch_upstream(before, instance, role, ports)
    except ValueError as exc:
        raise HelperError(f"{path}: {exc}") from None
    record = project / workspaceweb.UPSTREAM_FILE
    kept = record.read_text(encoding="utf-8") if record.is_file() and not record.is_symlink() else None
    if then == own:
        record.unlink(missing_ok=True)
    else:
        _write(record, json.dumps(then) + "\n")
    try:
        said = _put_site(path, text)
    except BaseException:
        if kept is None:
            record.unlink(missing_ok=True)
        else:
            _write(record, kept)
        raise
    return {"workspace": name, "instance": instance, "role": role, "site": str(path), **then,
            "from": now, "own": then == own, "nginx": said}


def _tls(name: str, certificate: str) -> tuple[str, str] | None:
    folder = OWN_CERTS / name if certificate == "own" else LETSENCRYPT / f"omniacli-{name}"
    chain, key = folder / "fullchain.pem", folder / "privkey.pem"
    return (str(chain), str(key)) if chain.is_file() and key.is_file() else None


def _nginx_reload() -> str:
    if _run("systemctl", "is-active", "nginx", check=False).strip() != "active":
        return "nginx is not running: the site is written, it is read when nginx starts"
    _run("systemctl", "reload", "nginx")
    return "nginx reloaded"


def _put_site(path: Path, text: str | None) -> str:
    """The site written (None: removed), nginx -t, or the file before back."""
    before = path.read_text(encoding="utf-8") if path.is_file() else None
    if text == before:
        return "the site is unchanged"
    if text is None:
        path.unlink()
    else:
        _write(path, text)
    test = subprocess.run([_tool("nginx"), "-t"], env=ENV, capture_output=True, text=True)
    if test.returncode:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            _write(path, before)
        raise HelperError(f"nginx -t refused the site, the one before is back: {test.stderr.strip()}")
    return _nginx_reload()


def _resolves(host: str) -> bool:
    """host has an address in DNS: Let's Encrypt can reach it."""
    try:
        return bool(socket.getaddrinfo(host, 80, proto=socket.IPPROTO_TCP))
    except (OSError, UnicodeError):
        return False


PROBE_TRIES, PROBE_WAIT = 5, 1.0


def _reaches_here(host: str) -> bool:
    """http://host reaches this nginx: it answers a token put in the
    challenges' folder, as Let's Encrypt will ask (behind a proxy or a
    router too). host is a checked host name (config.address_name)."""
    folder = Path(workspaceweb.ACME_ROOT) / ".well-known" / "acme-challenge"
    folder.mkdir(mode=0o755, parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    probe = folder / token
    _write(probe, token)
    request = urllib.request.Request(f"http://{host}/.well-known/acme-challenge/{token}",
                                     headers={"User-Agent": "omniacli-workspace-helper"})
    try:
        # the site was just reloaded: nginx's workers take the new names a
        # moment later, and until then the name falls to another server (on
        # odoo.ber.cat, 21/11/2026: a name answered here was said elsewhere)
        for attempt in range(PROBE_TRIES):
            try:
                with urllib.request.urlopen(request, timeout=8) as answer:
                    if answer.read(256).decode("ascii", "replace").strip() == token:
                        return True
            except (OSError, ValueError):
                pass
            if attempt + 1 < PROBE_TRIES:
                time.sleep(PROBE_WAIT)
        return False
    finally:
        probe.unlink(missing_ok=True)


def _certbot(name: str, names: list[str]) -> str | None:
    """Let's Encrypt's certificate for names: None, or why it failed."""
    email = str((_system().get("workspaces") or {}).get("acme_email") or "")
    if email and not EMAIL.fullmatch(email):
        raise HelperError(f"workspaces.acme_email of {SYSTEM_CONFIG} is not an email address")
    cmd = [_tool("certbot"), "certonly", "--webroot", "-w", workspaceweb.ACME_ROOT,
           "--cert-name", f"omniacli-{name}", "--non-interactive", "--agree-tos", "--keep-until-expiring",
           "--deploy-hook", "systemctl reload nginx",
           *(["-m", email] if email else ["--register-unsafely-without-email"])]
    for host in names:
        cmd += ["-d", host]
    done = subprocess.run(cmd, env=ENV, capture_output=True, text=True)
    return (done.stderr or done.stdout).strip() or f"certbot failed ({done.returncode})" if done.returncode else None


def _hash_bucket(dump: str) -> None:
    """The names of a workspace are longer than nginx's default (64) holds:
    omniacli.conf sets more, once, unless the machine's nginx sets it (a
    second time is an error)."""
    path = NGINX_SITES / "omniacli.conf"
    if path.exists() or re.search(r"^\s*server_names_hash_bucket_size\s", dump, re.MULTILINE):
        return
    _write(path, "# written by omniacli workspace-helper: room for the long names of the workspaces\n"
                 f"server_names_hash_bucket_size {workspaceweb.HASH_BUCKET};\n")


def omniacli_address(defaults: Path = SERVICE_DEFAULTS) -> str:
    """host:port of the OmniaCLI service, from its settings (root's file):
    127.0.0.1:8090 unless they say otherwise; a service listening on every
    address (0.0.0.0, ::) is reached on 127.0.0.1."""
    values = {}
    try:
        for line in defaults.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key in ("OMNIACLI_HOST", "OMNIACLI_PORT"):
                values[key] = value.strip().strip("\"'")
    except OSError:
        pass
    host = values.get("OMNIACLI_HOST") or "127.0.0.1"
    port = values.get("OMNIACLI_PORT") or "8090"
    if host in ("0.0.0.0", "::", "[::]") or not re.fullmatch(r"[A-Za-z0-9.:\[\]-]{1,64}", host):
        host = "127.0.0.1"
    if not (port.isdigit() and 1 <= int(port) <= 65535):
        port = "8090"
    return f"{host}:{port}"


@contextlib.contextmanager
def _site_lock(name: str):
    """The workspace's site read and written by one action at a time: an
    update without stopping of one instance (upstream) and of another, or a
    publication (web), would otherwise write over each other's change. A
    dot-file beside the sites, which nginx (conf.d/*.conf) does not read."""
    NGINX_SITES.mkdir(parents=True, exist_ok=True)
    fd = os.open(NGINX_SITES / f".omniacli-{_name('workspace', name)}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def web(name: str, storage: str, spec_json: str) -> dict:
    with _site_lock(name):
        return _web(name, storage, spec_json)


def _web(name: str, storage: str, spec_json: str) -> dict:
    image, mount = workspace(name, storage)
    path = NGINX_SITES / f"omniacli-{name}.conf"
    instances = instances_on_disk(mount) if mounted(image, mount) else []
    spec = {**web_spec(spec_json, instances), "workspace": name}
    made = {"workspace": name, "site": str(path), "names": [], "https": False, "certificate_error": None,
            "relayed_names": []}
    if not spec["domain"] or not instances:
        made["nginx"] = _put_site(path, None) if path.exists() else "no site"
        # no site, nothing to switch: its lock goes too (an action waiting on it makes the site anew)
        (NGINX_SITES / f".omniacli-{name}.lock").unlink(missing_ok=True)
        lineage = LETSENCRYPT / f"omniacli-{name}"
        if lineage.exists():
            _run("certbot", "delete", "--cert-name", f"omniacli-{name}", "--non-interactive")
        return made
    sites = _sites(mount, spec, instances)
    redirects, made["redirects_moved"] = _addresses(sites, spec)
    # only where a record of it still leads somewhere: without one there is nothing to keep working,
    # and a name out of DNS would be said at every publication as left out of the certificate
    redirects += [r for r in _main_own_names(sites, spec, redirects) if _resolves(r["names"][0])]
    names = [n for site in sites for n in site["names"]]
    # the other names (aliases, redirects): after the workspace's, in the certificate too
    extra = [n for site in sites for n in site["aliases"]] + [n for r in redirects for n in r["names"]]
    made["names"] = names + [n for site in sites for n in site["aliases"]]
    made["redirects"] = {n: r["to"] for r in redirects for n in r["names"]}
    # the names of the environments published through a relay lead to the relay, which holds their
    # certificate: they stay out of this machine's (Let's Encrypt would reach the relay, not here)
    relayed = {n for site in sites if site.get("relay_port") for n in (*site["names"], *site["aliases"])}
    relayed |= {n for r in redirects if r["to"] in relayed for n in r["names"]}
    made["relayed_names"] = sorted(relayed)
    dump = subprocess.run([_tool("nginx"), "-T"], env=ENV, capture_output=True, text=True)
    if dump.returncode:
        raise HelperError(f"nginx -T: {dump.stderr.strip()}")
    taken = workspaceweb.clashes(names + extra, workspaceweb.names_elsewhere(dump.stdout, str(path)))
    if taken:
        raise HelperError("names other sites of nginx answer on: " + ", ".join(taken))
    _hash_bucket(dump.stdout)
    acme = Path(workspaceweb.ACME_ROOT)
    acme.mkdir(mode=0o755, exist_ok=True)
    ipv6 = Path("/proc/net/if_inet6").exists()
    tls = _tls(name, spec["certificate"])
    omniacli = omniacli_address()
    made["nginx"] = _put_site(path, workspaceweb.conf_text(name, sites, tls, ipv6, omniacli, redirects))
    if spec["certificate"] == "letsencrypt":
        # one name with no DNS record would fail the whole certificate: it is
        # asked for the names that resolve, the others are said (on http until
        # their record is there and the site is published again). An other
        # name (www.customer.com) often still points to the customer's old
        # site: it goes in only when this nginx answers its challenge
        own, other = [n for n in names if n not in relayed], [n for n in extra if n not in relayed]
        known = [n for n in own if _resolves(n)]
        reached = [n for n in other if _resolves(n) and _reaches_here(n)]
        made["without_dns"] = [n for n in own + other if not _resolves(n)]
        made["elsewhere"] = [n for n in other if n not in reached and n not in made["without_dns"]]
        known += reached
        made["certificate_error"] = (_certbot(name, known) if known else None if not own + other
                                     else "no name of the workspace is in DNS yet")
        if made["certificate_error"] is None and tls is None and known:
            tls = _tls(name, spec["certificate"])
            made["nginx"] = _put_site(path, workspaceweb.conf_text(name, sites, tls, ipv6, omniacli, redirects))
    elif tls is None:
        made["certificate_error"] = "no certificate of the customer's yet: upload it"
    made["https"] = tls is not None
    return made


def web_taken(name: str) -> dict:
    """The names the other sites of this machine's nginx answer on (nginx -T),
    but the workspace's own site: what its Addresses dialog may not take
    (UX-008 §6.1). It reads only."""
    path = NGINX_SITES / f"omniacli-{_name('workspace', name)}.conf"
    dump = subprocess.run([_tool("nginx"), "-T"], env=ENV, capture_output=True, text=True)
    if dump.returncode:
        raise HelperError(f"nginx -T: {dump.stderr.strip()}")
    return {"elsewhere": workspaceweb.names_elsewhere(dump.stdout, str(path))}


# --- the customers' portal (Options › Customers' portal): OmniaCLI's own
# panel on a name of this machine, written as the workspaces' sites are

# why the portal is not written here: OmniaCLI went on a machine that already
# had its Odoo (provision.sh existing, no workspaces.storage): its nginx is
# whoever administers the machine's, as the Addresses dialog says
NOT_OUR_NGINX = ("This machine's nginx is kept by whoever administers it (OmniaCLI went beside an Odoo of before, "
                 "without workspaces): OmniaCLI does not write a site here")
# days a self-signed certificate of the portal is made for, and the days
# before its end it is made again
SELF_SIGNED_DAYS, SELF_SIGNED_RENEW = 90, 14


def _portal_site() -> Path:
    return NGINX_SITES / f"omniacli-{workspaceweb.PORTAL}.conf"


def _self_signed(host: str) -> tuple[str, str] | None:
    """A certificate of the portal's own for host, while Let's Encrypt cannot
    give one (no DNS yet, a machine on a private network): the browser warns,
    the traffic is encrypted. Made again for another name or near its end;
    None when openssl cannot make it."""
    folder = OWN_CERTS / workspaceweb.PORTAL
    chain, key, name = folder / "fullchain.pem", folder / "privkey.pem", folder / "name"
    fresh = chain.is_file() and subprocess.run(
        [_tool("openssl"), "x509", "-in", str(chain), "-noout", "-checkend", str(SELF_SIGNED_RENEW * 86400)],
        env=ENV, capture_output=True, stdin=subprocess.DEVNULL).returncode == 0
    if fresh and key.is_file() and name.is_file() and name.read_text(encoding="utf-8").strip() == host:
        return str(chain), str(key)
    OWN_CERTS.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    new_chain, new_key = folder / "fullchain.pem.new", folder / "privkey.pem.new"
    try:
        done = subprocess.run([_tool("openssl"), "req", "-x509", "-newkey", "ec", "-pkeyopt",
                               "ec_paramgen_curve:prime256v1", "-nodes", "-days", str(SELF_SIGNED_DAYS),
                               "-subj", f"/CN={host}", "-addext", f"subjectAltName=DNS:{host}",
                               "-keyout", str(new_key), "-out", str(new_chain)],
                              env=ENV, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if done.returncode or not new_chain.is_file() or not new_key.is_file():
            return None
        os.chmod(new_key, 0o600)
        os.replace(new_chain, chain)
        os.replace(new_key, key)
        _write(name, host + "\n", mode=0o600)
    finally:
        new_chain.unlink(missing_ok=True)
        new_key.unlink(missing_ok=True)
    return str(chain), str(key)


def _portal_removed(path: Path) -> str:
    """The portal's site gone, with its certificates."""
    said = _put_site(path, None) if path.exists() else "no site"
    if (LETSENCRYPT / f"omniacli-{workspaceweb.PORTAL}").exists():
        _run("certbot", "delete", "--cert-name", f"omniacli-{workspaceweb.PORTAL}", "--non-interactive")
    folder = OWN_CERTS / workspaceweb.PORTAL
    for name in ("fullchain.pem", "privkey.pem", "name"):
        (folder / name).unlink(missing_ok=True)
    if folder.is_dir():
        folder.rmdir()
    return said


def portal(host: str) -> dict:
    """The customers' portal on host, "-": none. Only where OmniaCLI keeps the
    machine's nginx (workspaces.storage of the main configuration); host a
    host name no other site of nginx answers on (nginx -T). The site, nginx
    -t or the one before back; then, when host leads to this nginx, Let's
    Encrypt's certificate, else a self-signed one (said in the result)."""
    if not storage_roots():
        raise HelperError(NOT_OUR_NGINX)
    path = _portal_site()
    made = {"site": str(path), "address": "", "https": False, "certificate": "", "certificate_error": None}
    if host == "-":
        made["nginx"] = _portal_removed(path)
        return made
    try:
        host = config.address_name("address", host)
    except ConfigError as exc:
        raise HelperError(str(exc)) from None
    made["address"] = host
    dump = subprocess.run([_tool("nginx"), "-T"], env=ENV, capture_output=True, text=True)
    if dump.returncode:
        raise HelperError(f"nginx -T: {dump.stderr.strip()}")
    taken = workspaceweb.clashes([host], workspaceweb.names_elsewhere(dump.stdout, str(path)))
    if taken:
        raise HelperError("a name another site of nginx answers on: " + ", ".join(taken))
    _hash_bucket(dump.stdout)
    Path(workspaceweb.ACME_ROOT).mkdir(mode=0o755, exist_ok=True)
    before = workspaceweb.portal_name_in(path.read_text(encoding="utf-8")) if path.is_file() else ""
    lineage = LETSENCRYPT / f"omniacli-{workspaceweb.PORTAL}"
    if before and before != host and lineage.exists():
        # the certificate of the name of before: another name gets its own
        _run("certbot", "delete", "--cert-name", f"omniacli-{workspaceweb.PORTAL}", "--non-interactive", check=False)
    ipv6 = Path("/proc/net/if_inet6").exists()
    omniacli = omniacli_address()
    tls = _tls(workspaceweb.PORTAL, "letsencrypt") if before == host else None
    kind = "letsencrypt" if tls else ""
    if tls is None:
        tls = _self_signed(host)
        kind = "self-signed" if tls else ""
    made["nginx"] = _put_site(path, workspaceweb.portal_conf_text(host, tls, ipv6, omniacli))
    if not _resolves(host):
        made["certificate_error"] = f"{host} is not in DNS yet"
    elif kind != "letsencrypt" and not _reaches_here(host):
        made["certificate_error"] = f"{host} does not lead to this machine's nginx"
    else:
        made["certificate_error"] = _certbot(workspaceweb.PORTAL, [host])
        found = _tls(workspaceweb.PORTAL, "letsencrypt") if made["certificate_error"] is None else None
        if found and found != tls:
            tls, kind = found, "letsencrypt"
            made["nginx"] = _put_site(path, workspaceweb.portal_conf_text(host, tls, ipv6, omniacli))
    made["https"], made["certificate"] = tls is not None, kind
    return made


def portal_taken() -> dict:
    """The names the other sites of nginx answer on, but the portal's own:
    what the portal's address may not take (ours: OmniaCLI keeps this nginx).
    It reads only."""
    ours = bool(storage_roots())
    if not ours:
        return {"elsewhere": {}, "ours": False}
    dump = subprocess.run([_tool("nginx"), "-T"], env=ENV, capture_output=True, text=True)
    if dump.returncode:
        raise HelperError(f"nginx -T: {dump.stderr.strip()}")
    return {"elsewhere": workspaceweb.names_elsewhere(dump.stdout, str(_portal_site())), "ours": True}


def _openssl(*args: str) -> str:
    done = subprocess.run([_tool("openssl"), *args], env=ENV, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)
    if done.returncode:
        raise HelperError(f"openssl {args[0]}: {(done.stderr or done.stdout).strip()}")
    return done.stdout


def owncert(name: str, storage: str, given: str | None = None) -> dict:
    workspace(name, storage)
    text = sys.stdin.read(MAX_PEM + 1) if given is None else given
    if len(text) > MAX_PEM:
        raise HelperError(f"more than {MAX_PEM // 1024} KB on the standard input")
    try:
        data = json.loads(text)
    except ValueError:
        raise HelperError("the standard input is not JSON {certificate, key}") from None
    if not isinstance(data, dict):
        raise HelperError("the standard input is not JSON {certificate, key}")
    chain, key = str(data.get("certificate") or ""), str(data.get("key") or "")
    if "-----BEGIN CERTIFICATE-----" not in chain or "PRIVATE KEY-----" not in key:
        raise HelperError("a certificate (PEM, with its chain) and its private key (PEM) are needed")
    OWN_CERTS.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder = OWN_CERTS / name
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    new_chain, new_key = folder / "fullchain.pem.new", folder / "privkey.pem.new"
    try:
        _write(new_chain, chain.strip() + "\n", mode=0o600)
        _write(new_key, key.strip() + "\n", mode=0o600)
        if _openssl("x509", "-in", str(new_chain), "-noout", "-pubkey") != \
                _openssl("pkey", "-in", str(new_key), "-pubout", "-passin", "pass:"):
            raise HelperError("the key is not the certificate's")
        if subprocess.run([_tool("openssl"), "x509", "-in", str(new_chain), "-noout", "-checkend", "0"],
                          env=ENV, capture_output=True, stdin=subprocess.DEVNULL).returncode:
            raise HelperError("the certificate has expired")
        not_after = _openssl("x509", "-in", str(new_chain), "-noout", "-enddate").strip().partition("=")[2]
        alt = subprocess.run([_tool("openssl"), "x509", "-in", str(new_chain), "-noout", "-ext", "subjectAltName"],
                             env=ENV, capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
        os.replace(new_chain, folder / "fullchain.pem")
        os.replace(new_key, folder / "privkey.pem")
    finally:
        new_chain.unlink(missing_ok=True)
        new_key.unlink(missing_ok=True)
    return {"workspace": name, "not_after": not_after,
            "names": [part.strip()[4:] for part in alt.replace("\n", ",").split(",") if part.strip().startswith("DNS:")]}


# --- Log in as on a site of another nginx (a hosting panel's): its server
# block of 443 for the instance's name gets one line, an include of the two
# paths OmniaCLI answers there (Log in as, deploy on push). A site cannot be
# given a location from outside it, so the line is in the site; a timer of
# root's keeps it there (login-path keep on): a site the panel writes again,
# or a new instance's, has it within a minute.

PATHS_SNIPPET = Path("/etc/nginx/omniacli-paths.conf")
PATHS_MARK = "# omniacli: Log in as, deploy on push"
BEFORE_SUFFIX = ".omniacli-before"
KEEP_UNIT = "omniacli-login-path"
KEEP_EVERY = 60
_SERVER_OPEN = re.compile(r"(?m)^[ \t]*server\s*\{")


def _server_blocks(text: str) -> list[tuple[int, int]]:
    """(index after its {, index of its }) of each server block; comments skipped."""
    blocks = []
    for match in _SERVER_OPEN.finditer(text):
        depth, i = 1, match.end()
        while i < len(text) and depth:
            char = text[i]
            if char == "#":
                i = text.find("\n", i)
                if i < 0:
                    break
                continue
            depth += {"{": 1, "}": -1}.get(char, 0)
            i += 1
        if depth == 0:
            blocks.append((match.end(), i - 1))
    return blocks


def _https_names(block: str) -> set[str]:
    """The names a server block answers on, when it listens on 443."""
    body = "\n".join(line.split("#", 1)[0] for line in block.splitlines())
    if not re.search(r"(?:^|[;\s])listen\s+[^;]*\b443\b", body):
        return set()
    return {n for m in re.finditer(r"(?:^|[;\s])server_name\s+([^;]*);", body) for n in m.group(1).split()}


def _answers_https(block: str, host: str) -> bool:
    return host in _https_names(block)


def _nginx_files() -> dict[Path, str]:
    """The files of nginx's configuration (nginx -T names them), but our snippet."""
    dump = _run(_tool("nginx"), "-T", check=False)
    found = {}
    for line in dump.splitlines():
        if line.startswith("# configuration file ") and line.endswith(":"):
            path = Path(line[len("# configuration file "):-1])
            if path != PATHS_SNIPPET and path.is_file():
                found[path.resolve()] = path.read_text(encoding="utf-8")
    return found


def _login_hosts() -> list[str]:
    """The names the OmniaCLI service says its instances answer on: its file
    next to the servers file (workspaceweb.LOGIN_HOSTS_FILE). Written by its
    user, so read as data: a regular file, each entry a domain name."""
    folder = Path(str(_system().get("servers_file") or "/var/lib/omniacli/servers.yaml")).parent
    try:
        fd = os.open(folder / workspaceweb.LOGIN_HOSTS_FILE, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return []
    with os.fdopen(fd, encoding="utf-8") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            return []
        text = fh.read(1 << 20)
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return sorted({h for h in data if isinstance(h, str) and config.DOMAIN_NAME.fullmatch(h)})


def _paths_snippet() -> str:
    address = omniacli_address()
    lines = ["# written by omniacli workspace-helper login-path: the paths OmniaCLI answers on",
             "# the names of its instances, included in their server block of 443", ""]
    for location in (f"location = {workspaceweb.LOGIN_PATH}", f"location ^~ {workspaceweb.HOOKS_PATH}"):
        lines += [f"{location} {{", f"    proxy_pass http://{address};", *workspaceweb._headers("    "), "}"]
    return "\n".join(lines) + "\n"


def _keep_units() -> tuple[Path, Path]:
    return SYSTEMD / f"{KEEP_UNIT}.service", SYSTEMD / f"{KEEP_UNIT}.timer"


def _login_path_keep(mode: str) -> dict:
    """on: a timer of root's runs login-path sync all every minute; off: no more
    (the lines stay: login-path remove all takes them away)."""
    service, timer = _keep_units()
    if mode == "on":
        _write(service, "# written by omniacli workspace-helper login-path keep on\n"
                        "[Unit]\nDescription=OmniaCLI: the Log in as path in the nginx sites of its instances\n"
                        f"[Service]\nType=oneshot\nExecStart={OMNIACLI_COMMAND} workspace-helper login-path sync all\n")
        _write(timer, "# written by omniacli workspace-helper login-path keep on\n"
                      "[Unit]\nDescription=OmniaCLI: the Log in as path kept in the nginx sites, every minute\n"
                      f"[Timer]\nOnBootSec={KEEP_EVERY}\nOnUnitActiveSec={KEEP_EVERY}\nAccuracySec=5\n"
                      "[Install]\nWantedBy=timers.target\n")
        _run("systemctl", "daemon-reload")
        _run("systemctl", "enable", "--now", f"{KEEP_UNIT}.timer")
        return {**login_path("sync", "all"), "keep": True, "every_seconds": KEEP_EVERY,
                "undo": "omniacli workspace-helper login-path keep off; "
                        "omniacli workspace-helper login-path remove all"}
    if mode == "off":
        _run("systemctl", "disable", "--now", f"{KEEP_UNIT}.timer", check=False)
        for unit in (service, timer):
            unit.unlink(missing_ok=True)
        _run("systemctl", "daemon-reload", check=False)
        return {"keep": False, "note": "the lines stay in the sites: login-path remove all takes them away"}
    raise HelperError("login-path keep takes on or off")


def login_path(action: str, target: str) -> dict:
    """check | add | remove | sync the include of PATHS_SNIPPET in the server
    blocks of 443 of a name (target: a domain name, or all: the names of the
    service's instances; remove all: every line of ours), or keep on | off
    (the timer). add keeps each file as it was in <file>.omniacli-before
    (once); sync, run every minute, does not. Whatever is changed is checked
    by nginx -t, and put back when it refuses."""
    if action == "keep":
        return _login_path_keep(target)
    if action not in ("check", "add", "remove", "sync"):
        raise HelperError("login-path takes check, add, remove, sync or keep")
    everything = target == "all"
    if not everything and not config.DOMAIN_NAME.fullmatch(target):
        raise HelperError(f"{target!r} is not a domain name (or all)")
    hosts = _login_hosts() if everything else [target]
    files = _nginx_files()

    def ours(block: str) -> bool:
        names = _https_names(block)
        return bool(names) and (PATHS_MARK in block if everything and action == "remove"
                                else any(h in names for h in hosts))

    found = {path: [(a, b) for a, b in _server_blocks(text) if ours(text[a:b])] for path, text in files.items()}
    found = {path: blocks for path, blocks in found.items() if blocks}
    served = {name for path, blocks in found.items() for a, b in blocks for name in _https_names(files[path][a:b])}
    no_site = [h for h in hosts if h not in served]
    if not everything and no_site:
        raise HelperError(f"no server block of nginx answers https on {target} here")

    def has_line(block: str) -> bool:
        return PATHS_MARK in block or workspaceweb.LOGIN_PATH in block

    ready = {str(path): all(has_line(files[path][a:b]) for a, b in blocks) for path, blocks in found.items()}
    answer = {"host": target, "files": ready, "ready": all(ready.values()) and not no_site, "no_site": no_site}
    if action == "check":
        return answer
    changed: dict[Path, str] = {}
    snippet = None
    for path, blocks in found.items():
        text = files[path]
        for a, b in reversed(blocks):  # from the last: the indexes before it stay good
            block = text[a:b]
            if action == "remove":
                kept = "".join(line for line in block.splitlines(keepends=True) if PATHS_MARK not in line)
                text = text[:a] + kept + text[b:]
            elif not has_line(block):
                text = text[:a] + f"\n    include {PATHS_SNIPPET};  {PATHS_MARK}" + text[a:]
        if text != files[path]:
            changed[path] = text
    if action != "remove":
        snippet = _paths_snippet()
        if any(PATHS_MARK in text for text in (*changed.values(), *files.values())):
            before_snippet = PATHS_SNIPPET.read_text(encoding="utf-8") if PATHS_SNIPPET.is_file() else None
            if before_snippet != snippet:
                _write(PATHS_SNIPPET, snippet)
            else:
                snippet = None
        else:
            snippet = None
    for path, text in changed.items():
        before = path.with_name(path.name + BEFORE_SUFFIX)
        if action == "add" and not before.exists():
            shutil.copy2(path, before)
        info = path.stat()
        _write(path, text, mode=info.st_mode & 0o777, uid=info.st_uid, gid=info.st_gid)
    if changed or snippet is not None:
        test = subprocess.run([_tool("nginx"), "-t"], env=ENV, capture_output=True, text=True)
        if test.returncode:
            for path in changed:
                path.write_text(files[path], encoding="utf-8")
            raise HelperError(f"nginx -t refused it, the files are as before: {test.stderr.strip()[-400:]}")
        reloaded = _nginx_reload()
    else:
        reloaded = "nothing to change"
    if action == "remove" and everything:
        PATHS_SNIPPET.unlink(missing_ok=True)
    if action != "remove":
        # what is there now: every site found has the line
        answer = {**answer, "files": {path: True for path in ready}, "ready": not no_site}
    return {**answer, "changed": [str(p) for p in changed], "nginx": reloaded,
            "undo": f"omniacli workspace-helper login-path remove {target}"}


def _project(name: str, storage: str, instance: str, role: str) -> Path:
    """The compose project of a role of an instance of the workspace (mounted)."""
    image, mount = workspace(name, storage)
    _name("instance", instance)
    if role not in ROLES:
        raise HelperError(f"the role is {' or '.join(ROLES)}")
    if not mounted(image, mount):
        raise HelperError(f"the disk of workspace {name} is not mounted")
    project = mount / instance / role
    if project.is_symlink() or not (project / "compose.yaml").is_file():
        raise HelperError(f"no project {instance}/{role} in workspace {name}")
    return project


# point-in-time recovery: an instance's own on a dedicated database
# (pitrhelper.py); on the machine's cluster it is the cluster's (AD-006,
# clusterpitr.py): switched on, and its base backups made, by the machine's
# admins only, in the Database tab of the machine
PITR_ON_CLUSTER = ("this instance is on the machine's PostgreSQL, whose recovery at a minute is one for all its "
                   "instances: an admin of the machine switches it on and makes its full copies, in the Database "
                   "tab of the machine")


PITR_ON_REMOTE = ("point-in-time recovery is of the machine that has the database: this instance's database is "
                  "on another machine, and it is not offered from here")


def _dedicated(project: Path) -> Path:
    if composehelper.on_cluster(project) is not None:
        raise HelperError(PITR_ON_CLUSTER)
    if composehelper.on_remote(project) is not None:
        raise HelperError(PITR_ON_REMOTE)
    return project


def pitr_enable(name: str, storage: str, instance: str, role: str) -> dict:
    return pitrhelper.enable(_dedicated(_project(name, storage, instance, role)))


def pitr_base(name: str, storage: str, instance: str, role: str) -> dict:
    return pitrhelper.base(_dedicated(_project(name, storage, instance, role)))


def pitr_state(name: str, storage: str, instance: str, role: str) -> dict:
    project = _project(name, storage, instance, role)
    env = composehelper.on_cluster(project)
    if env is not None:
        # the cluster's archive, and what a recovery of this database would take
        found = _cluster(clusterpitr.state, env["major"], env["role"])
        return {"machine": True, **{k: found[k] for k in (
            "major", "enabled", "bases", "segments", "archive_size", "first", "last", "failed", "last_failed",
            "failing", "room", "seconds", "recovering")}}
    if composehelper.on_remote(project) is not None:
        return {"enabled": False, "remote": True, "bases": [], "segments": 0, "archive_size": 0,
                "first": None, "last": None, "failed": 0}
    return pitrhelper.state(project)


def pitr_restore(name: str, storage: str, source: str, target: str, when: str, source_db: str,
                 target_db: str) -> dict:
    """source's production database, as it was at when, into target's production."""
    if source == target:
        raise HelperError("the source and the target are one instance")
    return pitrhelper.restore(_dedicated(_project(name, storage, source, "production")),
                              _dedicated(_project(name, storage, target, "production")), when, source_db, target_db)


# --- the machine's PostgreSQL cluster (machinedb.py; docs/adr/postgresql-della-macchina.md)

def _cluster(call, *args):
    try:
        return call(*args)
    except machinedb.ClusterError as exc:
        raise HelperError(str(exc)) from None


def machine_db_create(storage: str, major: str) -> dict:
    """machine-db-create <storage> <major>: the cluster of PostgreSQL major,
    its data on storage, the storages of the workspaces mounted in it."""
    roots = storage_roots()
    if storage not in roots:
        raise HelperError(f"{storage!r} is not a storage of workspaces.storage ({', '.join(roots) or 'none'})")
    bound_roots = [str(bind_storage(name)) for name in roots]
    return _cluster(machinedb.create, storage, bind_storage(storage), major, bound_roots, str(OMNIACLI_COMMAND))


def machine_db_remove(major: str) -> dict:
    done = _cluster(machinedb.remove, major)
    if db_listen_file():
        # a cluster that listened: its firewall rules go with it
        db_firewall()
    return done


def machine_db_state() -> dict:
    return _cluster(machinedb.state)


def machine_db_mounts() -> dict:
    return _cluster(machinedb.mounts)


# a grant's values on the input: at most these bytes
GRANT_INPUT = 16 * 1024


def machine_db_grant(major: str) -> dict:
    """machine-db-grant <major>, its values as JSON on the input
    (machinedb.grant_add): a database of the cluster for an instance of
    another machine. The firewall closes the cluster's network before its
    port opens, and lets that machine's address in after."""
    raw = sys.stdin.read(GRANT_INPUT + 1)
    if len(raw) > GRANT_INPUT:
        raise HelperError("the values of a grant on the input are too long")
    try:
        spec = json.loads(raw)
    except ValueError:
        raise HelperError("the values of a grant are not JSON") from None
    if not isinstance(spec, dict):
        raise HelperError("the values of a grant are not a JSON object")
    try:
        bridge = machinedb.bridge_name(major)
    except machinedb.ClusterError as exc:
        raise HelperError(str(exc)) from None
    kept = db_listen_file()
    if bridge not in kept:
        DB_LISTEN.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        _write(DB_LISTEN, json.dumps({**kept, bridge: []}, indent=1, sort_keys=True) + "\n", mode=0o600)
        if not (SYSTEMD / FIREWALL_UNIT).is_file():
            firewall("on")
        else:
            db_firewall_apply()
    try:
        made = _cluster(machinedb.grant_add, major, spec)
    finally:
        # as the cluster is now: its port open or not, the addresses of its grants
        allowed = db_firewall()
    return {**made, "firewall": allowed.get(bridge, [])}


def machine_db_ungrant(major: str, role: str, mode: str) -> dict:
    """machine-db-ungrant <major> <role> revoke | allow | drop: the other
    machine no longer comes in (its database stays), comes in again, or its
    database and role are dropped; the firewall follows."""
    if mode not in ("revoke", "allow", "drop"):
        raise HelperError("machine-db-ungrant <major> <role> revoke, allow or drop")
    try:
        if mode == "drop":
            done = _cluster(machinedb.grant_remove, major, role)
        else:
            done = _cluster(machinedb.grant_set, major, role, mode == "revoke")
    finally:
        allowed = db_firewall()
    return {**done, "firewall": allowed.get(machinedb.bridge_name(major), [])}


def machine_db_port(major: str, mode: str) -> dict:
    """machine-db-port <major> close: the cluster back to its socket only,
    when no database of another machine is allowed in; its firewall rules go."""
    if mode != "close":
        raise HelperError("machine-db-port <major> close")
    try:
        done = _cluster(machinedb.listen_off, major)
    finally:
        db_firewall()
    return done


# --- recovery at a minute of the machine's cluster (clusterpitr.py, AD-006)

def machine_db_archive_state(major: str) -> dict:
    return _cluster(clusterpitr.state, major)


def machine_db_archive_on(major: str, budget_gb: str, keep: str, reserve_gb: str, restart: str) -> dict:
    """machine-db-archive-on <major> <budget in GB> <bases kept> <GB left
    free> restart|no-restart: the archive of the cluster on, or its amounts
    changed; no-restart refuses a cluster that would have to restart."""
    if restart not in ("restart", "no-restart"):
        raise HelperError("machine-db-archive-on <major> <budget> <keep> <reserve> restart|no-restart")
    return _cluster(clusterpitr.enable, major, budget_gb, keep, reserve_gb, restart)


def machine_db_archive_off(major: str) -> dict:
    return _cluster(clusterpitr.disable, major)


def machine_db_archive_base(major: str) -> dict:
    return _cluster(clusterpitr.base, major)


def machine_db_recover(name: str, storage: str, source: str, target: str, when: str) -> dict:
    """machine-db-recover <workspace> <storage> <source> <target> <time>:
    the production database of source as it was at time, into the
    production of target (a staging of the same workspace, both on the
    machine's cluster of one major). target's containers are stopped before
    its database is written and stay stopped: whoever asked neutralizes it
    and starts it."""
    if source == target:
        raise HelperError("the source and the target are one instance")
    src, dst = _project(name, storage, source, "production"), _project(name, storage, target, "production")
    env_src, env_dst = composehelper.on_cluster(src), composehelper.on_cluster(dst)
    if env_src is None or env_dst is None:
        raise HelperError("machine-db-recover is for two instances on the machine's PostgreSQL: one with a "
                          "dedicated database has its own recovery at a minute (pitr-restore)")
    if env_src["major"] != env_dst["major"]:
        raise HelperError("the staging is on another PostgreSQL than its production")
    try:
        return clusterpitr.restore(env_src["major"], name, env_src["role"], env_dst["role"], when,
                                   stop=lambda: _dc(dst, "stop", timeout=600))
    except clusterpitr.Interrupted:
        raise HelperError("stopped: the temporary copy is deleted, the staging holds no copy of the production") from None
    except (HelperError, machinedb.ClusterError) as exc:
        raise HelperError(str(exc)) from None
    except Exception as exc:  # noqa: BLE001 - whatever it says may name the temporary cluster or another customer
        # the reason goes to root's recovery.log; a workspace's user reads this
        try:
            clusterpitr._log(machinedb.project_of(env_src["major"]),
                             f"recovery of {env_src['role']} at {when}: {type(exc).__name__}: {exc}")
        except machinedb.ClusterError:
            pass
        raise HelperError("the recovery failed: the temporary copy is deleted and the production was not touched. "
                          "The reason is in the recovery log of the machine's PostgreSQL, which the machine's "
                          "admin reads") from None


def machine_db_recover_sweep() -> dict:
    return _cluster(clusterpitr.sweep)


# --- a read replica of the machine's cluster on another machine (managedreplica.py, AD-004)

REPLICA_INPUT = 256 * 1024


def _input_json(what: str, optional: bool = False) -> dict:
    raw = sys.stdin.read(REPLICA_INPUT + 1)
    if len(raw) > REPLICA_INPUT:
        raise HelperError(f"the values of {what} on the input are too long")
    if optional and not raw.strip():
        return {}
    try:
        spec = json.loads(raw)
    except ValueError:
        raise HelperError(f"the values of {what} are not JSON") from None
    if not isinstance(spec, dict):
        raise HelperError(f"the values of {what} are not a JSON object")
    return spec


def _dedicated_project(workspace_name: str, storage: str, instance: str) -> Path:
    """The project of the production of an instance, as managedreplica asks it: this helper's own check."""
    try:
        return _project(workspace_name, storage, instance, "production")
    except HelperError as exc:
        raise machinedb.ClusterError(str(exc)) from None


managedreplica.dedicated_project = _dedicated_project


def _published_for(made_port, allowed: dict) -> list:
    """The addresses the firewall lets reach the published port of a dedicated database."""
    return (allowed.get(f"port{made_port}") or {}).get("from", []) if made_port else []


def machine_db_replica_facts(major: str) -> dict:
    return _cluster(managedreplica.facts, major)


def machine_db_replica_allow(major: str) -> dict:
    """machine-db-replica-allow <major>, its values as JSON on the input
    (managedreplica.allow): the machine that will host the replica let in.
    The firewall closes the cluster's network before its port opens, and
    lets that machine's address in after."""
    spec = _input_json("a replica")
    if managedreplica.is_dedicated(major):
        # the dedicated database of an instance: its port opens with a pg_hba.conf that lets no other
        # machine in, then the firewall lets the one address of the copy's machine reach it
        try:
            made = _cluster(managedreplica.allow, major, spec)
        finally:
            allowed = db_firewall()
        return {**made, "firewall": _published_for(made.get("port"), allowed)}
    try:
        bridge = machinedb.bridge_name(major)
    except machinedb.ClusterError as exc:
        raise HelperError(str(exc)) from None
    kept = db_listen_file()
    if bridge not in kept:
        DB_LISTEN.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        _write(DB_LISTEN, json.dumps({**kept, bridge: []}, indent=1, sort_keys=True) + "\n", mode=0o600)
        if not (SYSTEMD / FIREWALL_UNIT).is_file():
            firewall("on")
        else:
            db_firewall_apply()
    try:
        made = _cluster(managedreplica.allow, major, spec)
    finally:
        allowed = db_firewall()
    return {**made, "firewall": allowed.get(bridge, [])}


def machine_db_replica_ready(major: str) -> dict:
    return _cluster(managedreplica.ready, major, _input_json("a replica"))


def machine_db_replica_state(major: str) -> dict:
    return _cluster(managedreplica.state, major)


def machine_db_replica_cap(major: str, value: str) -> dict:
    return _cluster(managedreplica.set_cap, major, value)


def machine_db_replica_drop(major: str) -> dict:
    try:
        done = _cluster(managedreplica.drop, major)
    finally:
        allowed = db_firewall()
    if managedreplica.is_dedicated(major):
        listen = _cluster(lambda: managedreplica.source(major).listening()) or {}
        return {**done, "firewall": _published_for(listen.get("port"), allowed)}
    return {**done, "firewall": allowed.get(machinedb.bridge_name(major), [])}


def machine_db_replica_unlisten(key: str) -> dict:
    """machine-db-replica-unlisten <workspace>.<storage>.<instance>: the
    dedicated database of an instance that listened for a replica it no
    longer has goes back to what its compose.yaml says: the override and
    its certificate gone, its container made again (the seconds it did not
    answer), the firewall's entry gone."""
    if not managedreplica.is_dedicated(key):
        raise HelperError("machine-db-replica-unlisten <workspace>.<storage>.<instance>")
    try:
        return _cluster(lambda: managedreplica.source(key).listen_off())
    finally:
        db_firewall()


def machine_db_replica_replaced(major: str) -> dict:
    return _cluster(managedreplica.replaced, major)


def machine_db_replica_make(major: str, step: str) -> dict:
    """machine-db-replica-make <major> prepare | copy | start: the replica of
    the cluster of a linked machine made here, a step at a time (the job says
    each). prepare takes its values as JSON on the input, the password of
    the replication's role among them; what fails at any step is taken away
    by machine-db-replica-remove."""
    if step == "prepare":
        # d<n>: the copy of the dedicated database of an instance, its PostgreSQL's major among its values
        given = _input_json("a replica")
        spec = {**given, "key": major} if machinedb.HOSTED_DEDICATED.fullmatch(major) else {**given, "major": major}
        storage = str(spec.get("storage") or "")
        roots = storage_roots()
        if storage not in roots:
            raise HelperError(f"{storage!r} is not a storage of workspaces.storage ({', '.join(roots) or 'none'})")
        made = _cluster(managedreplica.prepare, spec, bind_storage(storage), str(OMNIACLI_COMMAND))
        # its network reached from the primary's machine only, going out to the primary's port only
        db_firewall()
        return made
    if step == "copy":
        return _cluster(managedreplica.copy, major)
    if step == "start":
        return _cluster(managedreplica.start, major)
    raise HelperError("machine-db-replica-make <major> prepare, copy or start")


def machine_db_replica_hosted(major: str) -> dict:
    return _cluster(managedreplica.hosted_state, major)


def machine_db_replica_sync(major: str) -> dict:
    return _cluster(managedreplica.sync, major, _input_json("a replica"))


def machine_db_replica_promote(major: str) -> dict:
    """machine-db-replica-promote <major>, optionally {"grants": {...}} as
    JSON on the input: the databases the primary had given to other
    machines, as it said them when it was stopped."""
    lent = _input_json("a promotion", optional=True).get("grants")
    try:
        return _cluster(managedreplica.promote, major, lent if isinstance(lent, dict) else None)
    finally:
        # promoted, it follows nothing: its network goes out nowhere; it is reached from where the
        # primary's grants came in from
        db_firewall()


def machine_db_replica_grant(major: str, role: str, mode: str) -> dict:
    """machine-db-replica-grant <major> <role> revoke | allow | drop: a
    database of the promoted copy another machine uses, as machine-db-ungrant
    on a cluster; the firewall follows."""
    if mode not in ("revoke", "allow", "drop"):
        raise HelperError("machine-db-replica-grant <major> <role> revoke, allow or drop")
    try:
        if mode == "drop":
            done = _cluster(managedreplica.hosted_grant_remove, major, role)
        else:
            done = _cluster(managedreplica.hosted_grant_set, major, role, mode == "revoke")
    finally:
        allowed = db_firewall()
    return {**done, "firewall": (allowed.get(managedreplica.bridge_name(major)) or {}).get("from", [])}


def machine_db_replica_lent(major: str) -> dict:
    """machine-db-replica-lent <major>, {"grants": {...}} as JSON on the
    input: a copy promoted before it took the databases its primary had
    given to other machines takes them now (managedreplica.adopt)."""
    lent = _input_json("the databases of other machines").get("grants")
    try:
        return _cluster(managedreplica.adopt, major, lent if isinstance(lent, dict) else {})
    finally:
        db_firewall()


# what an instance whose database moved keeps of where it is now (its record), beside database_on
MOVED_META = "database_copy"


def machine_db_moved(name: str, storage: str, instance: str) -> dict:
    """machine-db-moved <workspace> <storage> <instance>, {"host", "port",
    "ca", "machine"} as JSON on the input: the database of an instance on
    the cluster of a linked machine is somewhere else now (the promoted copy
    of that cluster, UX-011 §B; or that cluster again, after its return):
    the instance is pointed there, over TLS with the certificate verified
    against that CA, with the role, the password and the database it has,
    and started again. machine: the machine that hosts the copy, "" when
    the database is on its own cluster again."""
    project = _project(name, storage, instance, "production")
    folder = project.parent
    meta = _meta(folder)
    if composehelper.on_remote(project) is None or not meta.get("database_link") or meta.get("promoted_from"):
        raise HelperError(f"{instance} has no database on the cluster of a linked machine: it follows nothing")
    spec = _input_json("a database that moved")
    values = _env(project)
    remote = remote_spec({"host": spec.get("host"), "port": spec.get("port"), "user": values.get("DB_USER", ""),
                          "password": values.get("DB_PASSWORD", ""), "database": str(meta.get("database") or ""),
                          "ca": spec.get("ca"), "link": meta.get("database_link"), "major": meta.get("database_major")})
    machine = str(spec.get("machine") or "")
    if machine and not machinedb.LINK_NAME.fullmatch(machine):
        raise HelperError("the machine that hosts the copy has not a name of a machine")
    was = str(meta.get("database_on") or "")
    now = f"{remote['host']}:{remote['port']}"
    if was == now:
        return {"already": True, "database": remote["database"]}
    before = {path: path.read_bytes() for path in (project / ".env", folder / INSTANCE_FILE, project / "config" / REMOTE_CA_FILE)
              if path.is_file()}
    exception = db_remote_file().get(str(project))
    private = _private_address(remote["host"])
    try:
        _write(project / ".env", stacktemplates.env_text({**values, "DB_HOST": remote["host"], "DB_PORT": str(remote["port"])}),
               mode=0o600)
        _write(project / "config" / REMOTE_CA_FILE, remote["ca"], mode=0o644)
        # a copy hosted on this very machine: its port is this machine's, forwarded to its container
        db_remote_set(project, {"subnet": values.get("SUBNET", ""), "ip": private, "port": remote["port"],
                                **({"local": True} if managedreplica.own_address(private) else {})} if private else None)
        copy = {"machine": machine, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "from": was} if machine else None
        _write(folder / INSTANCE_FILE, json.dumps({
            **{k: v for k, v in meta.items() if k != MOVED_META}, "database_on": now,
            **({MOVED_META: copy} if copy else {})}) + "\n")
        conf(name, storage, instance, "production", "{}")
        # its Odoo has had no database for a minute: it is not waited for when it is stopped to be made again
        _dc(project, "up", "-d", "--remove-orphans", "--timeout", "2", timeout=600)
    except BaseException:
        for path, data in before.items():
            path.write_bytes(data)
        db_remote_set(project, exception)
        raise
    # the copy hosted here lets this instance's network in (its firewall names the networks of its grants)
    db_firewall()
    return {"already": False, "database": remote["database"], "from": was, "local": bool(private and managedreplica.own_address(private))}


def machine_db_replica_remove(major: str) -> dict:
    try:
        return _cluster(managedreplica.remove, major)
    finally:
        db_firewall()


def odoo_major(template_name: str) -> int | None:
    """The Odoo a template is of, from its name (odoo-18-machine: 18;
    odooplm-20-machine: 20, the Odoo its label declares); None for a name
    that says none."""
    found = re.match(r"[a-z]+-(\d{1,3})(?:-|$)", template_name or "")
    return int(found.group(1)) if found else None


def replica_override(service: str) -> str:
    return managedreplica.override_text(service, None)


def _write_override(project: Path, reads: str | None, listen: dict | None) -> None:
    """OmniaCLI's override of a project written again (managedreplica.override_text), or gone with nothing to
    say; Odoo Enterprise as the instance's record says (UX-012), whatever the replica's part becomes."""
    text = managedreplica.override_text(reads, listen, enterprise.of_project(project))
    if text:
        _write(project / REPLICA_OVERRIDE, text)
    else:
        (project / REPLICA_OVERRIDE).unlink(missing_ok=True)


def _replica_source(project: Path, name: str, storage: str, instance: str) -> tuple[str, bool] | None:
    """(the key of the PostgreSQL the production of an instance is on, it is
    a dedicated one), None for one on another machine."""
    env = composehelper.on_cluster(project)
    if env is not None:
        return env["major"], False
    if composehelper.on_remote(project) is not None:
        return None
    return managedreplica.dedicated_key(name, storage, instance), True


def machine_db_replica_read(name: str, storage: str, instance: str, mode: str) -> dict:
    """machine-db-replica-read <workspace> <storage> <instance> on | off: the
    production of an instance reads from the replica of its PostgreSQL (the
    machine's cluster, or its own dedicated database: UX-011 §D; Odoo 18 and
    later), or from its database only again. Its files are written; whoever
    asked restarts its Odoo. With a database of its own Odoo reaches it over
    the project's network, where db_sslmode holds too: its certificate names
    it db, and the CA given to Odoo is the database's and the replica's."""
    if mode not in ("on", "off"):
        raise HelperError("machine-db-replica-read <workspace> <storage> <instance> on or off")
    project = _project(name, storage, instance, "production")
    folder = project.parent
    meta = _meta(folder)
    found = _replica_source(project, name, storage, instance)
    if found is None:
        raise HelperError(f"{instance} has its database on another machine: only an instance on the machine's cluster "
                          "or with a database of its own reads from its replica")
    key, dedicated = found
    if dedicated and mode == "on" and _cluster(machinedb.replica, project / machinedb.DEDICATED_FOLDER) is None:
        raise HelperError(f"{instance} has a database of its own with no read replica: it is made from the machine "
                          "that hosts it")
    template = template_named(str(meta.get("template") or ""))
    override, ca_file = project / REPLICA_OVERRIDE, project / "config" / REPLICA_CA_FILE
    exception = Path(str(project) + REPLICA_SUFFIX)
    if override.exists() and not override.read_text(encoding="utf-8", errors="replace").startswith(REPLICA_OVERRIDE_MARK):
        raise HelperError(f"{override} is not OmniaCLI's: it is not written over")
    # the database's own part of the override (its port for the copy's machine) stays as it is
    listen = _cluster(lambda: managedreplica.source(key).listening()) if dedicated else None
    if mode == "off":
        was = str(meta.pop(REPLICA_META, "") or "")
        _write_override(project, None, listen)
        ca_file.unlink(missing_ok=True)
        _write(folder / INSTANCE_FILE, json.dumps(meta) + "\n")
        db_remote_set(exception, None)
        conf(name, storage, instance, "production", "{}")
        return {"reads": False, "was": bool(was), "odoo_service": template.odoo_service}
    version = odoo_major(template.name)
    if version is None or version < ODOO_WITH_REPLICA:
        raise HelperError(f"Odoo {version or '?'} cannot read from a replica: it can from {ODOO_WITH_REPLICA} on "
                          "(db_replica_host)")
    target = _cluster(managedreplica.target, key)
    if target["state"] != "ready":
        raise HelperError("the replica took the cluster's place: there is nothing to read beside it")
    try:
        spec_host = machinedb.reached_as([target["host"]])[0]
        own_ca = _cluster(lambda: managedreplica.source(key).ca()) if dedicated else ""
    except machinedb.ClusterError as exc:
        raise HelperError(str(exc)) from None
    _write(ca_file, target["ca"] + own_ca, mode=0o644)
    _write_override(project, template.odoo_service, listen)
    _write(folder / INSTANCE_FILE, json.dumps({**meta, REPLICA_META: f"{spec_host}:{target['port']}"}) + "\n")
    private = _private_address(spec_host)
    if private:
        # the workspaces' firewall stops private addresses: this one address and port pass, from this project's network
        db_remote_set(exception, {"subnet": _env(project).get("SUBNET", ""), "ip": private, "port": target["port"]})
    conf(name, storage, instance, "production", "{}")
    return {"reads": True, "host": spec_host, "port": target["port"], "private": bool(private),
            "odoo_service": template.odoo_service}


def remote_twin(template: stacktemplates.Template) -> stacktemplates.Template:
    """The template on a database of another machine of the same Odoo as a
    template on the machine's cluster (odoo-18-machine: odoo-18-remote)."""
    # of a template with a database of its own, the twin of its own name
    name = f"{template.dedicated or template.name}-remote"
    found = [t for t in stacktemplates.available([TEMPLATES, templatestore.INSTALLED]).values()
             if t.on_remote and t.name == name]
    if not found:
        raise HelperError(f"no template {name} here: {template.name} cannot follow its database to another machine")
    return max(found, key=lambda t: stacktemplates.version_key(t.version))


def machine_db_replica_follow(name: str, storage: str, instance: str) -> dict:
    """machine-db-replica-follow <workspace> <storage> <instance>: after the
    replica of the machine's cluster was promoted and the cluster stopped,
    the instance takes the promoted copy as its database: its project
    written again as one on a database of another machine (the template's
    remote twin: TLS, the certificate verified against the replica's CA),
    with the role, the password and the database it had. What it was is kept
    beside it (PROMOTED). Asked again, it answers that it is done. An
    instance with a database of its own whose replica was promoted (UX-011
    §D) the same: its database's service is no longer in its project (its
    data stays in data/pgdata until machine-db-purge), and the override
    that published its port goes."""
    project = _project(name, storage, instance, "production")
    folder = project.parent
    meta = _meta(folder)
    env = composehelper.on_cluster(project)
    if env is None and composehelper.on_remote(project):
        if meta.get("promoted_from"):
            return {"already": True, "database": str(meta.get("database") or ""), "db_service": stacktemplates.REMOTE_DB_SERVICE}
        raise HelperError(f"{instance} is not on the machine's cluster")
    dedicated = env is None
    key = managedreplica.dedicated_key(name, storage, instance) if dedicated else env["major"]
    if dedicated and _cluster(machinedb.replica, project / machinedb.DEDICATED_FOLDER) is None:
        raise HelperError(f"{instance} is not on the machine's cluster")
    target = _cluster(managedreplica.target, key)
    if target["state"] != "replaced":
        raise HelperError("the database of this instance was not replaced by its replica: the instance stays on it"
                          if dedicated else
                          "the machine's cluster was not replaced by its replica: its instances stay on it")
    template = template_named(str(meta.get("template") or ""))
    twin = remote_twin(template)
    values = _env(project)
    role = managedreplica.DEDICATED_ROLE if dedicated else env["role"]
    major = _cluster(lambda: managedreplica.source(key).major) if dedicated else env["major"]
    database = str(meta.get("database") or role)
    remote = remote_spec({"host": target["host"], "port": target["port"], "user": role,
                          "password": values.get("DB_PASSWORD", ""), "database": database, "ca": target["ca"],
                          "link": target["link"], "major": major})
    kept = project / PROMOTED
    _keep_files(project, folder, kept)
    override = project / REPLICA_OVERRIDE
    override_was = override.read_bytes() if dedicated and override.is_file() and not override.is_symlink() else None
    try:
        _dc(project, "down", "--remove-orphans", timeout=600)
        _write(project / "compose.yaml", twin.compose)
        _write(project / ".env", stacktemplates.env_text({
            **_env_without(values, CLUSTER_KEYS), "DB_REMOTE": "1", "DB_HOST": remote["host"],
            "DB_PORT": str(remote["port"]), "DB_USER": remote["user"]}), mode=0o600)
        # the replica's part goes; Odoo Enterprise stays (UX-012)
        _write_override(project, None, None)
        (project / "config" / REPLICA_CA_FILE).unlink(missing_ok=True)
        db_remote_set(Path(str(project) + REPLICA_SUFFIX), None)
        _write(project / "config" / REMOTE_CA_FILE, remote["ca"], mode=0o644)
        private = _private_address(remote["host"])
        if private:
            db_remote_set(project, {"subnet": values.get("SUBNET", ""), "ip": private, "port": remote["port"]})
        moved = {"template": template.name, "version": template.version, "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                 **({"dedicated": True} if dedicated else {})}
        _write(folder / INSTANCE_FILE, json.dumps({
            **{k: v for k, v in meta.items() if k != REPLICA_META}, "template": twin.name, "version": twin.version,
            "database_on": f"{remote['host']}:{remote['port']}", "database_link": remote["link"],
            "database_major": remote["major"], "promoted_from": moved}) + "\n")
        conf(name, storage, instance, "production", "{}")
        _dc(project, "up", "-d", "--remove-orphans", timeout=600)
    except BaseException:
        # as it was: on a cluster that is stopped it does not work either, but nothing is half written
        _dc(project, "down", "--remove-orphans", check=False, timeout=600)
        _put_back(project, folder, kept)
        if override_was is not None:
            override.write_bytes(override_was)
        (project / "config" / REMOTE_CA_FILE).unlink(missing_ok=True)
        db_remote_set(project, None)
        raise
    if dedicated:
        # its database's port is no longer published: the firewall forgets it
        db_firewall()
    return {"already": False, "database": database, "db_service": stacktemplates.REMOTE_DB_SERVICE,
            "host": remote["host"], "port": remote["port"], "template": twin.name, "kept": str(kept)}


# what machine-db-replica-follow wrote in .env for the promoted copy, taken away when the instance goes back
PROMOTED_KEYS = ("DB_REMOTE", "DB_HOST", "DB_PORT", "DB_USER")
PROMOTED_META = ("database_on", "database_link", "database_major", "promoted_from")


def machine_db_replica_back(name: str, storage: str, instance: str) -> dict:
    """machine-db-replica-back <workspace> <storage> <instance>: after the
    machine's cluster came back from its promoted replica (UX-011 §A), the
    instance goes back on it: its project written again as one on the
    machine's cluster, by its socket, with the template and the cluster's
    values it had before the promotion (kept beside it, PROMOTED); what
    changed since (its options, its other values) stays. What the promotion
    kept is deleted. Asked again, it answers that it is done."""
    project = _project(name, storage, instance, "production")
    folder = project.parent
    meta = _meta(folder)
    moved = meta.get("promoted_from")
    if isinstance(moved, dict) and moved.get("dedicated") and composehelper.on_remote(project) is not None:
        return _dedicated_back(name, storage, instance, project, folder, meta, moved)
    if composehelper.on_cluster(project) is not None:
        return {"already": True, "database": str(meta.get("database") or ""), "db_service": machinedb.DB_SERVICE}
    if composehelper.on_remote(project) is None and not moved:
        # a database of its own again: done already
        return {"already": True, "database": str(meta.get("database") or ""), "db_service": managedreplica.DEDICATED_SERVICE}
    if not isinstance(moved, dict) or composehelper.on_remote(project) is None:
        raise HelperError(f"{instance} was not pointed at a promoted replica: it does not go back on the machine's cluster")
    kept = project / PROMOTED
    try:
        was = _env(kept)
        compose = (kept / "compose.yaml").read_text(encoding="utf-8")
    except OSError:
        was, compose = {}, ""
    if not compose or not all(was.get(key) for key in CLUSTER_KEYS):
        raise HelperError(f"what {instance} was before the promotion is not beside it any more: it goes back with "
                          "Move into the machine's cluster…")
    major = was["DB_CLUSTER"]
    replaced = _cluster(machinedb.replica, _cluster(machinedb.project_of, major)) or {}
    if replaced.get("state") == "replaced":
        raise HelperError("the machine's cluster has not come back: its instances stay on the promoted copy")
    template = template_named(str(moved.get("template") or ""))
    values = _env(project)
    before = {path: path.read_bytes() for path in (project / "compose.yaml", project / ".env", folder / INSTANCE_FILE,
                                                   project / "config" / REMOTE_CA_FILE) if path.is_file()}
    exception = db_remote_file().get(str(project))
    try:
        # its database, the promoted copy, is stopped: its Odoo has nothing to end, and is not waited for
        _dc(project, "down", "--remove-orphans", "--timeout", "2", timeout=600)
        _write(project / "compose.yaml", compose)
        _write(project / ".env", stacktemplates.env_text({
            **_env_without(values, PROMOTED_KEYS), **{key: was[key] for key in CLUSTER_KEYS}}), mode=0o600)
        (project / "config" / REMOTE_CA_FILE).unlink(missing_ok=True)
        db_remote_set(project, None)
        _write(folder / INSTANCE_FILE, json.dumps({
            **{k: v for k, v in meta.items() if k not in PROMOTED_META}, "template": template.name,
            "version": str(moved.get("version") or template.version)}) + "\n")
        conf(name, storage, instance, "production", "{}")
        _dc(project, "up", "-d", "--remove-orphans", timeout=600)
    except BaseException:
        # as it was, on the promoted copy: nothing is half written
        _dc(project, "down", "--remove-orphans", check=False, timeout=600)
        for path, data in before.items():
            path.write_bytes(data)
        if exception:
            db_remote_set(project, exception)
        raise
    shutil.rmtree(kept, ignore_errors=True)
    return {"already": False, "database": str(meta.get("database") or was["DB_USER"]), "db_service": machinedb.DB_SERVICE,
            "template": template.name}


def _dedicated_back(name: str, storage: str, instance: str, project: Path, folder: Path, meta: dict, moved: dict) -> dict:
    """After the dedicated database of an instance came back from its
    promoted replica and took the writes (UX-011 §A for one instance): the
    project it came back in is stopped clean and taken away, and the
    instance's own project is written again as one with a database of its
    own, on the data that came back: the compose.yaml it had before the
    promotion (kept beside it, PROMOTED), its .env without what pointed it
    at the copy, its database listening over TLS as before the promotion,
    for no other machine (the next replica restarts nothing; Close the
    port… closes it). Odoo and its database start once. What the promotion
    kept is deleted."""
    key = managedreplica.dedicated_key(name, storage, instance)
    back = _cluster(machinedb.returning, project / machinedb.DEDICATED_FOLDER) or {}
    if back.get("state") != "done":
        raise HelperError(f"the database of {instance} has not come back: {instance} stays on the promoted copy")
    kept = project / PROMOTED
    try:
        compose = (kept / "compose.yaml").read_text(encoding="utf-8")
    except OSError:
        compose = ""
    template = template_named(str(moved.get("template") or ""))
    if not compose:
        # what it was is not beside it any more: the template it had, as this machine has it now
        compose = template.compose
    if not re.search(rf"(?m)^  {managedreplica.DEDICATED_SERVICE}:\s*$", compose):
        raise HelperError(f"what {instance} was before the promotion has no database service: it is not written back")
    values = _env(project)
    before = {path: path.read_bytes() for path in (project / "compose.yaml", project / ".env", folder / INSTANCE_FILE,
                                                   project / "config" / REMOTE_CA_FILE) if path.is_file()}
    exception = db_remote_file().get(str(project))
    # its Odoo has had no database since the promoted copy stopped: it is not waited for
    _dc(project, "down", "--remove-orphans", "--timeout", "2", timeout=600)
    # the PostgreSQL that came back is stopped clean, and the project it came back in goes: never two on one data
    _cluster(managedreplica.back_release, key)
    try:
        listen = _cluster(lambda: managedreplica.source(key).listening())
        _write(project / "compose.yaml", compose)
        _write(project / ".env", stacktemplates.env_text(_env_without(values, PROMOTED_KEYS)), mode=0o600)
        (project / "config" / REMOTE_CA_FILE).unlink(missing_ok=True)
        db_remote_set(project, None)
        if listen:
            # as it listened before the promotion: its pg_hba.conf lets the project's network in, and no machine
            _cluster(lambda: managedreplica.source(key).took_back())
        _write_override(project, None, listen)
        _write(folder / INSTANCE_FILE, json.dumps({
            **{k: v for k, v in meta.items() if k not in PROMOTED_META}, "template": template.name,
            "version": str(moved.get("version") or template.version)}) + "\n")
        conf(name, storage, instance, "production", "{}")
        _dc(project, "up", "-d", "--remove-orphans", timeout=600)
    except BaseException:
        # as it was, pointed at the promoted copy (which is stopped): nothing is half written, and asked
        # again the instance is written back
        _dc(project, "down", "--remove-orphans", check=False, timeout=600)
        for path, data in before.items():
            path.write_bytes(data)
        _write_override(project, None, None)
        if exception:
            db_remote_set(project, exception)
        raise
    shutil.rmtree(kept, ignore_errors=True)
    # its port is this machine's again, for no address: the firewall says so
    db_firewall()
    return {"already": False, "database": str(meta.get("database") or managedreplica.DEDICATED_ROLE),
            "db_service": managedreplica.DEDICATED_SERVICE, "template": template.name, "listens": bool(listen)}


# the return after a promotion (managedreplica.py, UX-011 §A): on the machine that hosts the promoted
# copy (machine-db-replica-return) and on the one whose cluster comes back (machine-db-return)

def machine_db_replica_return(major: str, step: str) -> dict:
    """machine-db-replica-return <major> facts | allow | state | stop | resume
    | done | cancel: the promoted copy this machine hosts and the cluster it
    replaced, which comes back. allow and cancel take their values as JSON
    on the input (allow: the verifier of the replication's password)."""
    if step == "facts":
        return _cluster(managedreplica.return_facts, major)
    if step == "allow":
        return _cluster(managedreplica.return_allow, major, _input_json("a return"))
    if step == "state":
        return _cluster(managedreplica.return_state, major)
    if step == "stop":
        return _cluster(managedreplica.return_stop, major)
    if step == "resume":
        return _cluster(managedreplica.return_resume, major)
    if step == "done":
        try:
            return _cluster(managedreplica.return_done, major)
        finally:
            # stopped for good: no one reaches its network any more
            db_firewall()
    if step == "cancel":
        return _cluster(managedreplica.return_cancel, major, str(_input_json("a return").get("by") or ""))
    raise HelperError("machine-db-replica-return <major> facts, allow, state, stop, resume, done or cancel")


def machine_db_return(major: str, step: str) -> dict:
    """machine-db-return <major> facts | prepare | copy | start | state | wait |
    promote | note | purge | cancel | forget: the machine's cluster comes
    back from the replica that replaced it; or, <major> the key of an
    instance (<workspace>.<storage>.<instance>), the dedicated database of
    that instance does. prepare takes its values as JSON
    on the input, the password of the replication's role among them; wait
    the position the stopped copy ended at; note what the application keeps
    of a return that is done. The firewall follows: while it comes back the
    cluster goes out to the promoted copy's port, and nowhere else."""
    if step == "facts":
        return _cluster(managedreplica.back_facts, major)
    if step == "prepare":
        try:
            return _cluster(managedreplica.back_prepare, major, _input_json("a return"))
        finally:
            db_firewall()
    if step == "copy":
        return _cluster(managedreplica.back_copy, major)
    if step == "start":
        return _cluster(managedreplica.back_start, major)
    if step == "state":
        return _cluster(managedreplica.back_state, major)
    if step == "wait":
        return _cluster(managedreplica.back_wait, major, str(_input_json("a return").get("checkpoint") or ""))
    if step == "promote":
        try:
            return _cluster(managedreplica.back_promote, major)
        finally:
            db_firewall()
    if step == "note":
        return _cluster(managedreplica.back_note, major, _input_json("a return"))
    if step == "purge":
        return _cluster(managedreplica.back_purge, major)
    if step == "cancel":
        try:
            return _cluster(managedreplica.back_cancel, major)
        finally:
            db_firewall()
    if step == "forget":
        return _cluster(managedreplica.back_forget, major)
    raise HelperError("machine-db-return <major> facts, prepare, copy, start, state, wait, promote, note, purge, cancel "
                      "or forget")


# a move (AD-007): what the project was is kept beside it, the undo of the
# move; what the dumps of a move are, read by the cluster's user
KEPT = ".dedicated"
MOVES = ".move"
ODOO_DATA_FILESTORE = Path("data") / "filestore" / "filestore"
# a project's files a move rewrites, kept as they were
MOVED_FILES = ("compose.yaml", ".env", "config/odoo.conf", ROLE_FILE)
CLUSTER_KEYS = ("DB_CLUSTER", "DB_USER", "DB_SOCKET")


def machine_twin(template: stacktemplates.Template) -> stacktemplates.Template:
    """The template on the machine's cluster whose dedicated twin is template."""
    found = [t for t in stacktemplates.available([TEMPLATES, templatestore.INSTALLED]).values()
             if t.on_machine and t.dedicated == template.name]
    if not found:
        raise HelperError(f"no template on the machine's cluster for {template.name} here")
    return max(found, key=lambda t: stacktemplates.version_key(t.version))


def _meta(folder: Path) -> dict:
    try:
        meta = json.loads((folder / INSTANCE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HelperError(f"{folder} holds no {INSTANCE_FILE}: not an instance made by this helper") from None
    if not isinstance(meta, dict):
        raise HelperError(f"{folder / INSTANCE_FILE} is not an object")
    return meta


def _dc(project: Path, *args: str, input=None, stdout=None, check: bool = True,
        timeout: float | None = None) -> subprocess.CompletedProcess:
    text = stdout is None and not isinstance(input, bytes)
    done = subprocess.run(composehelper.compose(project, *args), env=ENV, cwd=project, input=input, timeout=timeout,
                          stdout=stdout if stdout is not None else subprocess.PIPE, stderr=subprocess.PIPE, text=text)
    if check and done.returncode:
        err = done.stderr if isinstance(done.stderr, str) else (done.stderr or b"").decode(errors="replace")
        raise HelperError(f"docker compose {' '.join(args[:4])} in {project.name} failed: {err.strip()[-400:]}")
    return done


def _dedicated_sql(project: Path, database: str, sql: str) -> str:
    """SQL as the postgres of the project's database service (its socket's trust)."""
    done = _dc(project, "exec", "-T", "--user", "postgres", "db", "psql", "-X", "-At", "-q", "-v", "ON_ERROR_STOP=1",
               "-U", "postgres", "-d", database, input=sql, check=False)
    if done.returncode:
        raise HelperError(f"the dedicated database refused: {(done.stderr or done.stdout).strip()[-400:]}")
    return done.stdout


def _dedicated_ready(project: Path, timeout: float = 180) -> None:
    end = time.monotonic() + timeout
    while _dc(project, "exec", "-T", "db", "pg_isready", "-q", "-h", "127.0.0.1", check=False).returncode:
        if time.monotonic() > end:
            raise HelperError(f"the dedicated database of {project} does not answer within {timeout:.0f} s")
        time.sleep(2)


def _keep_files(project: Path, folder: Path, into: Path) -> None:
    """The files a move rewrites, as they are, into into (root's only)."""
    machinedb._folder(into, 0o700)
    for name in MOVED_FILES:
        source = project / name
        if source.is_file() and not source.is_symlink():
            target = into / name.replace("/", "__")
            shutil.copy2(source, target)
    shutil.copy2(folder / INSTANCE_FILE, into / INSTANCE_FILE)


def _put_back(project: Path, folder: Path, kept: Path) -> None:
    for name in MOVED_FILES:
        source = kept / name.replace("/", "__")
        if source.is_file():
            shutil.copy2(source, project / name)
    if (kept / INSTANCE_FILE).is_file():
        shutil.copy2(kept / INSTANCE_FILE, folder / INSTANCE_FILE)


def _rename_filestore(project: Path, old: str, new: str) -> bool:
    store = project / ODOO_DATA_FILESTORE
    source, target = store / old, store / new
    if old == new or not source.is_dir() or source.is_symlink():
        return False
    if target.exists():
        raise HelperError(f"{target} exists already: the filestore of {old} is not renamed over it")
    os.rename(source, target)
    return True


def _env_without(values: dict, keys) -> dict:
    return {k: v for k, v in values.items() if k not in keys}


def _counts_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


def machine_db_move(name: str, storage: str, instance: str, to: str) -> dict:
    """machine-db-move <workspace> <storage> <instance> machine|dedicated:
    the production of instance moved to the machine's cluster or back to a
    database of its own (AD-007). Odoo stopped, pg_dump -Fc, the database
    made on the other side, pg_restore, the installed modules, users and
    partners counted on both sides, then the project switched and started.
    Whatever fails before the switch, the instance runs as it was; the data
    of the side left stays (its pgdata on a move to the cluster: the undo)
    until machine-db-purge."""
    if to not in ("machine", "dedicated"):
        raise HelperError("machine-db-move takes machine or dedicated")
    image, mount = workspace(name, storage)
    _name("instance", instance)
    if not mounted(image, mount):
        raise HelperError(f"the disk of workspace {name} is not mounted")
    folder = mount / instance
    project = folder / "production"
    if project.is_symlink() or not (project / "compose.yaml").is_file():
        raise HelperError(f"no project {instance}/production in workspace {name}")
    if (folder / "test" / ".env").is_file():
        raise HelperError(f"{instance} has a test of its own: on the machine's cluster an instance has its "
                          "production only (its copies are stagings)")
    meta = _meta(folder)
    template = template_named(str(meta.get("template") or ""))
    if to == "machine":
        return _cluster(_to_machine, name, storage, mount, instance, folder, project, meta, template)
    return _cluster(_to_dedicated, name, storage, mount, instance, folder, project, meta, template)


def _to_machine(name, storage, mount, instance, folder, project, meta, template) -> dict:
    if template.on_machine or composehelper.on_cluster(project) is not None:
        raise HelperError(f"{instance} is on the machine's cluster already")
    followed = machinedb.replica(project / machinedb.DEDICATED_FOLDER)
    if followed:
        raise HelperError(f"the database of {instance} has a read replica on {followed.get('link')}: drop the replica "
                          "first (Database tab)")
    twin = machine_twin(template)
    major = twin.postgres
    cluster = machinedb.project_of(major)
    values = _env(project)
    password, old = values.get("DB_PASSWORD", ""), str(meta.get("database") or "")
    role = machinedb.env_name(name, instance)
    uid, gid = machinedb.pg_user()
    moves = machinedb._folder(project / MOVES, 0o700, uid, gid)
    dump = moves / "to-machine.dump"
    size = int(_dedicated_sql(project, old, "SELECT pg_database_size(current_database());\n").strip() or 0)
    before = _counts_line(_dedicated_sql(project, old, machinedb.COUNTS))
    pitr = pitrhelper.enabled(project)
    started = time.monotonic()
    _dc(project, "stop", template.odoo_service, timeout=300)
    added = False
    try:
        with open(dump, "wb") as fh:
            _dc(project, "exec", "-T", "--user", "postgres", "db", "pg_dump", "-Fc", "-U", "postgres", "-d", old,
                stdout=fh, timeout=6 * 3600)
        os.chown(dump, uid, gid)
        os.chmod(dump, 0o600)
        machinedb.env_add(major, name, mount, instance, password)
        added = True
        listing = machinedb._run(cluster, "exec", "-T", machinedb.SERVICE, "pg_restore", "-l", str(dump)).stdout
        extensions, kept_list = machinedb.restore_list(listing)
        list_file = moves / "to-machine.list"
        machinedb.write(list_file, kept_list, mode=0o600, uid=uid, gid=gid)
        machinedb.make_extensions(lambda db, q: machinedb.sql(cluster, db, q), role, extensions)
        jobs = str(max(1, min(os.cpu_count() or 1, 8)))
        done = machinedb._run(cluster, "exec", "-T", machinedb.SERVICE, "pg_restore", "-j", jobs, "--no-owner",
                              "--no-acl", f"--role={role}", "-U", "postgres", "-d", role, "-L", str(list_file),
                              str(dump), check=False, timeout=6 * 3600)
        if done.returncode:
            raise HelperError(f"pg_restore into the machine's cluster failed: {(done.stderr or '').strip()[-600:]}")
        machinedb.close(cluster, role, role)
        after = _counts_line(machinedb.sql(cluster, role, machinedb.COUNTS))
        if after != before:
            raise HelperError(f"the copy does not hold what the database holds (installed modules|users|partners: "
                              f"{before} there, {after} on the cluster)")
    except BaseException:
        if added:
            try:
                machinedb.env_remove(major, role)
            except machinedb.ClusterError:
                pass
        _dc(project, "up", "-d", check=False, timeout=600)
        raise
    kept = project / KEPT
    _keep_files(project, folder, kept)
    renamed = False
    try:
        _dc(project, "down", "--remove-orphans", timeout=600)
        _write(project / "compose.yaml", twin.compose)
        _write(project / ".env", stacktemplates.env_text({**values, "DB_CLUSTER": major, "DB_USER": role,
                                                         "DB_SOCKET": str(machinedb.socket_folder(major))}),
               mode=0o600)
        moved = {"template": template.name, "version": template.version, "database": old, "pitr": pitr,
                 "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        _write(folder / INSTANCE_FILE, json.dumps({**meta, "template": twin.name, "version": twin.version,
                                                   "database": role, "moved_from": moved}) + "\n")
        renamed = _rename_filestore(project, old, role)
        conf(name, storage, instance, "production", "{}")
        _dc(project, "up", "-d", "--remove-orphans", timeout=600)
    except BaseException:
        _dc(project, "down", "--remove-orphans", check=False, timeout=600)
        _put_back(project, folder, kept)
        if renamed:
            _rename_filestore(project, role, old)
        _dc(project, "up", "-d", check=False, timeout=600)
        try:
            machinedb.env_remove(major, role)
        except machinedb.ClusterError:
            pass
        raise
    return {"to": "machine", "database": role, "before": old, "size": size, "counts": after,
            "seconds_down": round(time.monotonic() - started, 1), "template": twin.name, "pitr_was_on": pitr,
            "kept": [str(kept), str(project / "data" / "pgdata")],
            "undo": f"omniacli workspace-helper machine-db-move {name} {storage} {instance} dedicated"}


def _to_dedicated(name, storage, mount, instance, folder, project, meta, template) -> dict:
    if meta.get(REPLICA_META):
        raise HelperError(f"{instance} reads from the replica of the machine's cluster: stop reading from it first")
    env = composehelper.on_cluster(project)
    if not template.on_machine or env is None:
        raise HelperError(f"{instance} has a dedicated database already")
    twin = template_named(template.dedicated)
    major, role = env["major"], env["role"]
    cluster = machinedb.project_of(major)
    moved = meta.get("moved_from") if isinstance(meta.get("moved_from"), dict) else {}
    old = str(moved.get("database") or instance.replace("-", "_"))
    kept = project / KEPT
    values = _env(project)
    uid, gid = machinedb.pg_user()
    moves = machinedb._folder(project / MOVES, 0o700, uid, gid)
    dump = moves / "to-dedicated.dump"
    size = machinedb.database_size(major, role)
    before = _counts_line(machinedb.sql(cluster, role, machinedb.COUNTS))
    started = time.monotonic()
    _dc(project, "stop", timeout=300)
    machine_files = moves / "machine"
    try:
        with open(dump, "wb") as fh:
            machinedb._run(cluster, "exec", "-T", machinedb.SERVICE, "pg_dump", "-Fc", "-U", "postgres", "-d", role,
                           stdout=fh, timeout=6 * 3600)
        os.chown(dump, uid, gid)
        os.chmod(dump, 0o600)
        listing = machinedb._run(cluster, "exec", "-T", machinedb.SERVICE, "pg_restore", "-l", str(dump)).stdout
        extensions, kept_list = machinedb.restore_list(listing)
    except BaseException:
        _dc(project, "up", "-d", check=False, timeout=600)
        raise
    _keep_files(project, folder, machine_files)
    renamed = False
    try:
        _dc(project, "down", "--remove-orphans", timeout=600)
        # the compose.yaml of before when kept (with its archive of the recovery
        # at a minute), else the twin's; .env of now without the cluster's
        if (kept / "compose.yaml").is_file():
            shutil.copy2(kept / "compose.yaml", project / "compose.yaml")
        else:
            _write(project / "compose.yaml", twin.compose)
        dedicated_env = {**_env_without(values, CLUSTER_KEYS),
                         "DB_SUPER_PASSWORD": values.get("DB_SUPER_PASSWORD") or stacktemplates.new_password()}
        _write(project / ".env", stacktemplates.env_text(dedicated_env), mode=0o600)
        for item in twin.data:
            path = project / item["path"]
            if not path.exists():
                path.mkdir(mode=0o755, parents=True)
                if item["owner"]:
                    os.chown(path, *_owner(item["owner"]))
        if not (project / "dbinit" / "odoo-role.sh").is_file():
            (project / "dbinit").mkdir(mode=0o755, exist_ok=True)
            _write(project / "dbinit" / "odoo-role.sh", DB_ROLE_SCRIPT)
        _dc(project, "up", "-d", "db", timeout=600)
        _dedicated_ready(project)
        _dedicated_sql(project, "postgres", f"DROP DATABASE IF EXISTS {machinedb.ident(old)} WITH (FORCE);\n")
        _dedicated_sql(project, "postgres", f"CREATE DATABASE {machinedb.ident(old)} OWNER odoo TEMPLATE template0 "
                                            "ENCODING 'UTF8' LC_COLLATE 'C';\n")
        machinedb.make_extensions(lambda db, q: _dedicated_sql(project, db, q), old, extensions)
        _dc(project, "exec", "-T", "--user", "postgres", "db", "sh", "-c", "cat > /tmp/omniacli-restore.list",
            input=kept_list)
        with open(dump, "rb") as fh:
            done = subprocess.run(composehelper.compose(project, "exec", "-T", "--user", "postgres", "db",
                                                        "pg_restore", "--no-owner", "--no-acl", "--role=odoo",
                                                        "-U", "postgres", "-d", old, "-L",
                                                        "/tmp/omniacli-restore.list"),
                                  env=ENV, cwd=project, stdin=fh, capture_output=True, text=True, timeout=6 * 3600)
        if done.returncode:
            raise HelperError(f"pg_restore into the dedicated database failed: {done.stderr.strip()[-600:]}")
        after = _counts_line(_dedicated_sql(project, old, machinedb.COUNTS))
        if after != before:
            raise HelperError(f"the copy does not hold what the database holds (installed modules|users|partners: "
                              f"{before} on the cluster, {after} there)")
        restored = {k: v for k, v in meta.items() if k != "moved_from"}
        _write(folder / INSTANCE_FILE, json.dumps({**restored, "template": twin.name, "version": twin.version,
                                                   "database": old}) + "\n")
        renamed = _rename_filestore(project, role, old)
        conf(name, storage, instance, "production", "{}")
        _dc(project, "up", "-d", "--remove-orphans", timeout=600)
    except BaseException:
        _dc(project, "down", "--remove-orphans", check=False, timeout=600)
        _put_back(project, folder, machine_files)
        if renamed:
            _rename_filestore(project, old, role)
        _dc(project, "up", "-d", check=False, timeout=600)
        raise
    machinedb.env_remove(major, role)
    # the files of before the move to the cluster are the project's again
    shutil.rmtree(machine_files, ignore_errors=True)
    shutil.rmtree(kept, ignore_errors=True)
    pitr = pitrhelper.enabled(project)
    return {"to": "dedicated", "database": old, "before": role, "size": size, "counts": after,
            "seconds_down": round(time.monotonic() - started, 1), "template": twin.name, "pitr": pitr,
            "kept": [str(dump)],
            "undo": f"omniacli workspace-helper machine-db-move {name} {storage} {instance} machine"}


def _service_users() -> set[str]:
    """The user OmniaCLI runs as (who called sudo) and the package's: no key opens them."""
    return {caller()[0], "omniacli"}


def _ssh(call, *args):
    try:
        return call(*args, _service_users())
    except (sshkeys.KeyRefused, OSError) as exc:
        raise HelperError(str(exc)) from None


def ssh_keys() -> dict:
    """ssh-keys: the accounts a key may open, the keys OmniaCLI added to each
    and the others there; it reads only."""
    return _ssh(sshkeys.state)


def ssh_key_add(account: str) -> dict:
    """ssh-key-add <account>: {name, key, by} on the input, the key written at
    the end of the account's authorized_keys (sshkeys.add)."""
    raw = sys.stdin.read(sshkeys.INPUT_MAX + 1)
    if len(raw) > sshkeys.INPUT_MAX:
        raise HelperError("the key on the input is too long")
    try:
        given = json.loads(raw)
    except ValueError:
        raise HelperError("the key on the input is not JSON {name, key, by}") from None
    if not isinstance(given, dict):
        raise HelperError("the key on the input is JSON {name, key, by}")
    return _ssh(sshkeys.add, account, given)


def ssh_key_remove(account: str, fingerprint: str) -> dict:
    """ssh-key-remove <account> <SHA256:...>: the lines OmniaCLI wrote with that key taken out."""
    return _ssh(sshkeys.remove, account, fingerprint)


def machine_db_purge(name: str, storage: str, instance: str) -> dict:
    """machine-db-purge <workspace> <storage> <instance>: what a move left
    (the undo) taken away once its admin confirms: on the cluster, the old
    database folder, its archive of the recovery at a minute and the files
    of before; dedicated, the dumps of the move."""
    image, mount = workspace(name, storage)
    _name("instance", instance)
    if not mounted(image, mount):
        raise HelperError(f"the disk of workspace {name} is not mounted")
    project = mount / instance / "production"
    if project.is_symlink() or not (project / "compose.yaml").is_file():
        raise HelperError(f"no project {instance}/production in workspace {name}")
    gone = []
    paths = [project / MOVES]
    back = _cluster(machinedb.returning, project / machinedb.DEDICATED_FOLDER) or {}
    if back.get("state") in ("copying", "ready"):
        # data/pgdata is the copy that follows the promoted one, and the data of before is set aside for it
        raise HelperError(f"the database of {instance} comes back from its promoted copy: the data of before is set "
                          "aside for the return, and is freed when it is complete or cancelled")
    if composehelper.on_cluster(project) is not None:
        paths += [project / KEPT, project / "data" / "pgdata", project / "data" / "wal"]
    elif composehelper.on_remote(project) is not None and (_meta(project.parent).get("promoted_from") or {}).get("dedicated"):
        # its dedicated database was replaced by its promoted replica (UX-011 D): the data of before
        paths += [project / "data" / "pgdata", project / "data" / "wal"]
    for path in paths:
        if path.exists() and not path.is_symlink():
            shutil.rmtree(path)
            gone.append(str(path))
    return {"workspace": name, "instance": instance, "removed": gone}


# --- Odoo Enterprise (UX-012) ------------------------------------------------

def _enterprise_series(value: str) -> str:
    if not enterprise.SERIES.fullmatch(value or ""):
        raise HelperError(f"{value!r} is not a version of Odoo (19.0)")
    return value


def _enterprise_asked(template: stacktemplates.Template, series) -> dict | None:
    """{series, service} create gives a new instance that is to be Enterprise
    (a staging of an Enterprise production), None when not asked."""
    if not series:
        return None
    series = _enterprise_series(str(series))
    if enterprise.series_of_template(template.name) != series:
        raise HelperError(f"{template.name} is not Odoo {series}: its Enterprise is another version")
    if enterprise.state(series) is None:
        raise HelperError(f"no Odoo Enterprise {series} on this machine: it is fetched on Settings › Odoo Enterprise")
    return {"series": series, "service": template.odoo_service}


def _as_caller() -> dict:
    """What subprocess takes to run a command as the user who called sudo
    (nothing when the helper is not root: a test)."""
    if os.geteuid() != 0:
        return {}
    name, uid, groups = caller()
    return {"user": uid, "group": pwd.getpwnam(name).pw_gid, "extra_groups": groups}


def _enterprise_read(source: Path, into: Path) -> None:
    """source's tree, without .git, read by tar as the caller and extracted
    here by root with the data filter (no name or link out of into, no
    device, no set-id bit, no owner of the archive's)."""
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen([_tool("tar"), "-C", str(source), "--exclude=.git", "-cf", "-", "."],
                                stdout=subprocess.PIPE, stderr=errors, stdin=subprocess.DEVNULL, env=ENV, cwd="/",
                                **_as_caller())
        try:
            with tarfile.open(fileobj=proc.stdout, mode="r|") as archive:
                archive.extractall(into, filter="data")
        except (tarfile.TarError, OSError) as exc:
            proc.kill()
            proc.wait()
            raise HelperError(f"the copy of {source} was refused: {exc}") from None
        finally:
            proc.stdout.close()
        code = proc.wait()
        errors.seek(0)
        said = errors.read().decode(errors="replace").strip()
    if code:
        raise HelperError(f"{source} could not be read as the user of OmniaCLI: {said or code}")


def _root_tree(top: Path) -> None:
    """Every folder and file of top root's, read only for the others (a
    file the archive said executable stays executable)."""
    for dirpath, dirnames, filenames in os.walk(top):
        for name in [*dirnames, *filenames]:
            path = Path(dirpath) / name
            info = path.lstat()
            if os.geteuid() == 0:
                os.lchown(path, 0, 0)
            if stat.S_ISLNK(info.st_mode):
                continue
            os.chmod(path, 0o755 if stat.S_ISDIR(info.st_mode) or info.st_mode & 0o100 else 0o644)
    if os.geteuid() == 0:
        os.lchown(top, 0, 0)
    os.chmod(top, 0o755)


def _same(a: os.stat_result, b: os.stat_result) -> bool:
    return (stat.S_IFMT(a.st_mode) == stat.S_IFMT(b.st_mode) and a.st_size == b.st_size
            and int(a.st_mtime) == int(b.st_mtime) and stat.S_IMODE(a.st_mode) == stat.S_IMODE(b.st_mode))


def _sync_tree(new: Path, live: Path) -> dict:
    """live made equal to new in place, file by file (each one replaced at
    once): the folder the instances mount stays the same folder. What
    changed, as counts."""
    counts = {"added": 0, "changed": 0, "removed": 0}
    live.mkdir(mode=0o755, exist_ok=True)
    seen = {Path(".")}
    for dirpath, dirnames, filenames in os.walk(new):
        here = Path(dirpath)
        rel = here.relative_to(new)
        target = live / rel
        links = [d for d in dirnames if (here / d).is_symlink()]
        dirnames[:] = [d for d in dirnames if d not in links]
        for name in dirnames:
            path = target / name
            seen.add(rel / name)
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                path.unlink()
            if not path.exists():
                path.mkdir(mode=0o755)
                counts["added"] += 1
        for name in [*filenames, *links]:
            source, path = here / name, target / name
            seen.add(rel / name)
            try:
                before = path.lstat()
            except FileNotFoundError:
                before = None
            if before is not None and stat.S_ISDIR(before.st_mode):
                shutil.rmtree(path)
                before = None
            if before is not None and _same(source.lstat(), before):
                continue
            os.replace(source, path)
            counts["added" if before is None else "changed"] += 1
    for dirpath, dirnames, filenames in os.walk(live, topdown=False):
        here = Path(dirpath)
        rel = here.relative_to(live)
        for name in [*filenames, *dirnames]:
            path = here / name
            if rel / name in seen:
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            counts["removed"] += 1
    return counts


def enterprise_sync(series: str) -> dict:
    series = _enterprise_series(series)
    jobs_dir = _system().get("jobs_dir")
    if not jobs_dir:
        raise HelperError(f"no jobs_dir in {SYSTEM_CONFIG}: OmniaCLI's copies of Odoo Enterprise have no place")
    source = enterprise.checkouts_of(Path(str(jobs_dir))) / series
    if source.is_symlink() or not source.is_dir():
        raise HelperError(f"no copy of Odoo Enterprise {series} in {source.parent}: OmniaCLI fetches it first")
    root = enterprise.ROOT
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    if root.is_symlink() or (os.geteuid() == 0 and root.lstat().st_uid != 0):
        raise HelperError(f"{root} is not root's folder")
    os.chmod(root, 0o755)
    (root / enterprise.STATE).mkdir(mode=0o755, exist_ok=True)
    new = root / f".{series}.new"
    shutil.rmtree(new, ignore_errors=True)
    new.mkdir(mode=0o700)
    try:
        _enterprise_read(source, new)
        modules = enterprise.modules_in(new)
        if not modules:
            raise HelperError(f"{source} holds no Odoo module: it is not Odoo Enterprise (Odoo would refuse the "
                              "folder in its addons_path)")
        _root_tree(new)
        os.chmod(new, 0o700)
        counts = _sync_tree(new, root / series)
        _root_tree(root / series)
    finally:
        shutil.rmtree(new, ignore_errors=True)
    done = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, env=ENV,
                          cwd="/", **_as_caller())
    head = done.stdout.strip()
    found = {"series": series, "commit": head if done.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", head) else "",
             "fetched": time.strftime("%Y-%m-%dT%H:%M:%S"), "modules": len(modules), "folder": str(root / series),
             **counts}
    _write(root / enterprise.STATE / f"{series}.json", json.dumps(found) + "\n")
    return found


def _enterprise_override(project: Path) -> None:
    """The project's override written again: the replica's part as it is, Odoo Enterprise as its record says."""
    try:
        reads, listen = managedreplica.override_reads(project), managedreplica.override_listen(project)
    except machinedb.ClusterError as exc:
        raise HelperError(str(exc)) from None
    _write_override(project, reads, listen)


def enterprise_switch(name: str, storage: str, instance: str, mode: str) -> dict:
    if mode not in ("on", "off"):
        raise HelperError("enterprise <workspace> <storage> <instance> on or off")
    image, mount = workspace(name, storage)
    _name("instance", instance)
    if not mounted(image, mount):
        raise HelperError(f"the disk of workspace {name} is not mounted")
    folder = mount / instance
    meta = _meta(folder)
    template = template_named(str(meta.get("template") or ""))
    series = enterprise.series_of_template(template.name)
    roles = [r for r in ROLES if (folder / r / "compose.yaml").is_file() and not (folder / r).is_symlink()]
    found = None
    if mode == "on":
        if series is None:
            raise HelperError(f"{template.name} names no version of Odoo: it has no Enterprise")
        found = enterprise.state(series)
        if found is None:
            raise HelperError(f"no Odoo Enterprise {series} on this machine: it is fetched on Settings › Odoo Enterprise")
        changed = {**meta, enterprise.META: {"series": series, "service": template.odoo_service}}
    else:
        changed = {k: v for k, v in meta.items() if k != enterprise.META}
    paths = [folder / INSTANCE_FILE, *(folder / r / p for r in roles for p in (REPLICA_OVERRIDE, "config/odoo.conf"))]
    before = {path: path.read_bytes() for path in paths if path.is_file() and not path.is_symlink()}
    try:
        _write(folder / INSTANCE_FILE, json.dumps(changed) + "\n")
        for role in roles:
            _enterprise_override(folder / role)
            conf(name, storage, instance, role, "{}")
    except BaseException:
        for path in paths:
            if path in before:
                path.write_bytes(before[path])
            elif path.name == REPLICA_OVERRIDE:
                path.unlink(missing_ok=True)
        raise
    return {"workspace": name, "instance": instance, "enterprise": mode == "on", "series": series or "",
            "roles": roles, "odoo_service": template.odoo_service, "modules": (found or {}).get("modules", 0),
            "commit": (found or {}).get("commit", "")}



# --- published through another machine (relay.py; README, Published through another machine) ---------
# On the relay: relay-facts, relay-host <key> (its values on the input), relay-unhost <key>, relay-hosted.
# On the machine behind NAT: relay-key <workspace>, relay-tunnel <workspace> on (its values on the input) | off.

HOSTED = Path(relay.HOSTED_FILE)
RELAY_HOME = Path(relay.RELAY_HOME)
TUNNELS = Path(relay.TUNNEL_ROOT)
SSHD_CONFIG = Path("/etc/ssh/sshd_config")
SSH_HOST_KEYS = (Path("/etc/ssh/ssh_host_ed25519_key.pub"), Path("/etc/ssh/ssh_host_ecdsa_key.pub"),
                 Path("/etc/ssh/ssh_host_rsa_key.pub"))
PROC_TCP = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))


@contextlib.contextmanager
def _relay_lock():
    """One relay action at a time: the hosted tunnels, authorized_keys and sshd_config are shared."""
    HOSTED.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(HOSTED.with_name(".relay.lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _hosted() -> dict:
    if HOSTED.is_symlink() or not HOSTED.is_file():
        return {}
    try:
        data = json.loads(HOSTED.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HelperError(f"{HOSTED} cannot be read") from None
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and relay.KEY.fullmatch(k)}


def _listening() -> set[int]:
    texts = []
    for path in PROC_TCP:
        try:
            texts.append(path.read_text(encoding="ascii"))
        except OSError:
            continue
    return relay.listening_ports(texts)


def _sshd_port() -> int:
    """The port this sshd listens on (sshd -T), 22 when it cannot be read."""
    done = subprocess.run([_tool("sshd"), "-T"], env=ENV, capture_output=True, text=True)
    for line in done.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key == "port" and value.strip().isdigit():
            return int(value.strip())
    return 22


def _host_key() -> str:
    for path in SSH_HOST_KEYS:
        try:
            return relay.check_host_key(path.read_text(encoding="utf-8"))
        except (OSError, relay.RelayError):
            continue
    raise HelperError("this machine's sshd has no host key in /etc/ssh")


def _sshd_reload() -> None:
    for unit in ("ssh", "sshd"):
        if _run("systemctl", "is-active", unit, check=False).strip() == "active":
            _run("systemctl", "reload", unit)
            return


def _sshd_config(text: str) -> bool:
    """sshd_config written (True when it changed), sshd -t, or the one before back."""
    before = SSHD_CONFIG.read_text(encoding="utf-8")
    if text == before:
        return False
    mode = stat.S_IMODE(SSHD_CONFIG.stat().st_mode)
    _write(SSHD_CONFIG, text, mode=mode)
    test = subprocess.run([_tool("sshd"), "-t"], env=ENV, capture_output=True, text=True)
    if test.returncode:
        _write(SSHD_CONFIG, before, mode=mode)
        raise HelperError(f"sshd -t refused the relay's Match block, sshd_config is back: {test.stderr.strip()}")
    _sshd_reload()
    return True


def _relay_user() -> None:
    """The relay's user: a system user without a shell nor a password (not
    locked: sshd refuses the key of a locked account without PAM), its home
    and authorized_keys root's."""
    try:
        pwd.getpwnam(relay.RELAY_USER)
    except KeyError:
        _run("useradd", "--system", "--user-group", "--home-dir", str(RELAY_HOME), "--no-create-home",
             "--shell", "/usr/sbin/nologin", "--comment", "OmniaCLI relay, tunnels only", relay.RELAY_USER)
    _run("usermod", "-p", "*", relay.RELAY_USER)
    for folder in (RELAY_HOME, RELAY_HOME / ".ssh"):
        if folder.is_symlink():
            raise HelperError(f"{folder} is a link: nothing is written")
        folder.mkdir(mode=0o755, exist_ok=True)
        os.chown(folder, 0, 0)
        os.chmod(folder, 0o755)


def _relay_site(key: str) -> Path:
    return NGINX_SITES / f"omniacli-relay-{relay.check_key(key)}.conf"


def _relay_self_signed(key: str, names: list[str]) -> tuple[str, str] | None:
    """A certificate of the relay's own for the names of a tunnel, while
    Let's Encrypt cannot give one (no DNS yet, a lab): the browser warns, the
    traffic is encrypted. Made again when the names change or near its end."""
    folder = OWN_CERTS / f"relay-{key}"
    chain, key_file, listed = folder / "fullchain.pem", folder / "privkey.pem", folder / "names"
    wanted = "\n".join(names) + "\n"
    fresh = chain.is_file() and subprocess.run(
        [_tool("openssl"), "x509", "-in", str(chain), "-noout", "-checkend", str(SELF_SIGNED_RENEW * 86400)],
        env=ENV, capture_output=True, stdin=subprocess.DEVNULL).returncode == 0
    if fresh and key_file.is_file() and listed.is_file() and listed.read_text(encoding="utf-8") == wanted:
        return str(chain), str(key_file)
    OWN_CERTS.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    new_chain, new_key = folder / "fullchain.pem.new", folder / "privkey.pem.new"
    try:
        done = subprocess.run([_tool("openssl"), "req", "-x509", "-newkey", "ec", "-pkeyopt",
                               "ec_paramgen_curve:prime256v1", "-nodes", "-days", str(SELF_SIGNED_DAYS),
                               "-subj", f"/CN={names[0]}", "-addext",
                               "subjectAltName=" + ",".join(f"DNS:{n}" for n in names),
                               "-keyout", str(new_key), "-out", str(new_chain)],
                              env=ENV, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if done.returncode or not new_chain.is_file() or not new_key.is_file():
            return None
        os.chmod(new_key, 0o600)
        os.replace(new_chain, chain)
        os.replace(new_key, key_file)
        _write(listed, wanted, mode=0o600)
    finally:
        new_chain.unlink(missing_ok=True)
        new_key.unlink(missing_ok=True)
    return str(chain), str(key_file)


def _relay_certs_removed(key: str) -> list[str]:
    removed = []
    if (LETSENCRYPT / f"omniacli-relay-{key}").exists():
        _run("certbot", "delete", "--cert-name", f"omniacli-relay-{key}", "--non-interactive", check=False)
        removed.append(f"the certificate omniacli-relay-{key} of Let's Encrypt")
    folder = OWN_CERTS / f"relay-{key}"
    for name in ("fullchain.pem", "privkey.pem", "names"):
        (folder / name).unlink(missing_ok=True)
    if folder.is_dir():
        folder.rmdir()
        removed.append(str(folder))
    return removed


def relay_facts() -> dict:
    """Whether this machine can be a relay, as another machine asks before
    choosing it: OmniaCLI keeps its nginx (workspaces.storage), its public
    addresses, its sshd's port and host key, the tunnels it hosts."""
    ours = bool(storage_roots())
    nginx = shutil.which("nginx", path=ENV["PATH"]) is not None
    addresses = sorted(workspaceweb.machine_addresses(shutil.which("ip", path=ENV["PATH"])))
    return {"ours": ours, "nginx": nginx, "addresses": addresses, "ssh_port": _sshd_port(),
            "host_key": _host_key(), "hosted": len(_hosted())}


def _relay_spec() -> dict:
    spec = _input_json("the publication")
    try:
        public_key = relay.check_public_key(spec.get("public_key"))
    except relay.RelayError as exc:
        raise HelperError(str(exc)) from None
    names, redirects = spec.get("names"), spec.get("redirects") or {}
    if not isinstance(names, list) or not isinstance(redirects, dict) or not names \
            or len(names) + len(redirects) > relay.MAX_NAMES:
        raise HelperError(f"names: a list, redirects: {{name: to}}, {relay.MAX_NAMES} at most in all, one name at least")
    try:
        served = list(dict.fromkeys(config.address_name("name", n) for n in names))
        sent = {config.address_name("name", k): config.address_name("name", v) for k, v in redirects.items()}
    except ConfigError as exc:
        raise HelperError(str(exc).removeprefix("name: ")) from None
    if set(served) & set(sent) or any(to not in served for to in sent.values()):
        raise HelperError("a redirect goes to a name the publication serves, and is none of them")
    link, workspace_name = str(spec.get("link") or ""), str(spec.get("workspace") or "")
    if not re.fullmatch(r"[A-Za-z0-9._@-]{1,64}", link) or not WORKSPACE_NAME.fullmatch(workspace_name):
        raise HelperError("link and workspace: the names of the machine behind and of its workspace")
    return {"public_key": public_key, "names": served, "redirects": sent, "link": link, "workspace": workspace_name}


def relay_host(key: str) -> dict:
    """A tunnel of a machine behind NAT hosted here: its key may listen on
    one port of this loopback (authorized_keys of omniacli-relay, the Match
    block of sshd_config), and this nginx serves its names there
    (omniacli-relay-<key>.conf), with Let's Encrypt's certificate when every
    name leads here, a self-signed one until then. Again with the same key:
    the same port, its names and key as given now."""
    try:
        relay.check_key(key)
    except relay.RelayError as exc:
        raise HelperError(str(exc)) from None
    if not storage_roots():
        raise HelperError(NOT_OUR_NGINX)
    spec = _relay_spec()
    with _relay_lock():
        hosted = _hosted()
        before = hosted.get(key)
        taken = {int(r.get("port") or 0) for k, r in hosted.items() if k != key}
        if before and int(before.get("port") or 0) in relay.RELAY_PORTS:
            port = int(before["port"])
        else:
            busy = _listening()
            port = next((p for p in relay.RELAY_PORTS if p not in taken and p not in busy and _free(p)), None)
        if port is None:
            raise HelperError(f"no port free in {relay.RELAY_PORTS.start}-{relay.RELAY_PORTS.stop - 1}")
        path = _relay_site(key)
        dump = subprocess.run([_tool("nginx"), "-T"], env=ENV, capture_output=True, text=True)
        if dump.returncode:
            raise HelperError(f"nginx -T: {dump.stderr.strip()}")
        every = [*spec["names"], *spec["redirects"]]
        clash = workspaceweb.clashes(every, workspaceweb.names_elsewhere(dump.stdout, str(path)))
        if clash:
            raise HelperError("names other sites of nginx answer on: " + ", ".join(clash))
        _hash_bucket(dump.stdout)
        _relay_user()
        hosted[key] = {**{k: spec[k] for k in ("link", "workspace", "public_key", "names", "redirects")}, "port": port,
                       "created": (before or {}).get("created") or time.strftime("%Y-%m-%dT%H:%M:%S")}
        keys_file = RELAY_HOME / ".ssh" / "authorized_keys"
        kept = keys_file.read_text(encoding="utf-8") if keys_file.is_file() else None
        ipv6 = Path("/proc/net/if_inet6").exists()

        def site(found):
            return relay.relay_site_text(key, spec["names"], spec["redirects"], port, found, ipv6,
                                         workspaceweb.ACME_ROOT, workspaceweb.MAX_BODY, workspaceweb.TIMEOUT)
        sshd = False
        try:
            _write(keys_file, relay.authorized_keys(hosted), mode=0o644)
            sshd = _sshd_config(relay.with_block(SSHD_CONFIG.read_text(encoding="utf-8")))
            Path(workspaceweb.ACME_ROOT).mkdir(mode=0o755, exist_ok=True)
            tls = _tls(f"relay-{key}", "letsencrypt")
            kind = "letsencrypt" if tls else ""
            if tls is None:
                tls = _relay_self_signed(key, every)
                kind = "self-signed" if tls else ""
            said = _put_site(path, site(tls))
        except BaseException:
            if before is None:
                # nothing of a new tunnel stays when its site cannot be written
                hosted.pop(key, None)
                if kept is None:
                    keys_file.unlink(missing_ok=True)
                else:
                    _write(keys_file, kept, mode=0o644)
                if sshd and not hosted:
                    # the Match block this first tunnel added goes with it
                    _sshd_config(relay.without_block(SSHD_CONFIG.read_text(encoding="utf-8")))
            raise
        _write(HOSTED, json.dumps(hosted, indent=1, sort_keys=True) + "\n", mode=0o600)
        # Let's Encrypt for the names that lead here (HTTP-01 on this nginx); the others are said
        reached = [n for n in every if _resolves(n) and _reaches_here(n)]
        missing = [n for n in every if n not in reached]
        error = ("not leading to this machine yet, so not in Let's Encrypt's certificate: " + ", ".join(missing)
                 if missing else None)
        if reached:
            failed = _certbot(f"relay-{key}", reached)
            # certbot's own lines (its log, where to ask for help) out: its reason only
            failed = " ".join(line.strip() for line in (failed or "").splitlines() if line.strip() and not line.startswith(
                ("Saving debug log", "Ask for help", "An unexpected error occurred"))) or failed
            found = _tls(f"relay-{key}", "letsencrypt") if failed is None else None
            if found and not missing:
                tls, kind = found, "letsencrypt"
                said = _put_site(path, site(tls))
            error = failed or error
    return {"key": key, "port": port, "user": relay.RELAY_USER, "host_key": _host_key(), "ssh_port": _sshd_port(),
            "addresses": sorted(workspaceweb.machine_addresses(shutil.which("ip", path=ENV["PATH"]))),
            "names": spec["names"], "redirects": spec["redirects"], "site": str(path), "nginx": said,
            "sshd": "the Match block of omniacli-relay added, sshd reloaded" if sshd else "sshd unchanged",
            "certificate": kind, "certificate_error": error, "new": before is None}


def relay_unhost(key: str) -> dict:
    """A tunnel hosted here taken away: its line of authorized_keys, its
    site and certificates; with the last one the relay's user, its home and
    the Match block of sshd_config too. One no longer here: nothing to do."""
    try:
        relay.check_key(key)
    except relay.RelayError as exc:
        raise HelperError(str(exc)) from None
    with _relay_lock():
        hosted = _hosted()
        found = hosted.pop(key, None)
        path = _relay_site(key)
        done = {"key": key, "was": bool(found), "site": str(path), "removed": []}
        if path.exists():
            done["nginx"] = _put_site(path, None)
            done["removed"].append(str(path))
        else:
            done["nginx"] = "no site"
        done["removed"] += _relay_certs_removed(key)
        keys_file = RELAY_HOME / ".ssh" / "authorized_keys"
        if hosted:
            _write(keys_file, relay.authorized_keys(hosted), mode=0o644)
            _write(HOSTED, json.dumps(hosted, indent=1, sort_keys=True) + "\n", mode=0o600)
            if found:
                done["removed"].append(f"its line of {keys_file}")
            done["left"] = len(hosted)
            return done
        # the last one: nothing of the relay stays
        if SSHD_CONFIG.is_file() and _sshd_config(relay.without_block(SSHD_CONFIG.read_text(encoding="utf-8"))):
            done["removed"].append("the Match block of omniacli-relay in sshd_config")
        try:
            pwd.getpwnam(relay.RELAY_USER)
        except KeyError:
            pass
        else:
            # its sessions end first: a tunnel still connected keeps the user busy
            _run("pkill", "-KILL", "-u", relay.RELAY_USER, check=False)
            _run("userdel", relay.RELAY_USER)
            done["removed"].append(f"the user {relay.RELAY_USER}")
        if keys_file.exists():
            keys_file.unlink()
            done["removed"].append(str(keys_file))
        for folder in (RELAY_HOME / ".ssh", RELAY_HOME):
            if folder.is_dir() and not folder.is_symlink():
                folder.rmdir()
                done["removed"].append(str(folder))
        if HOSTED.exists():
            HOSTED.unlink()
            done["removed"].append(str(HOSTED))
        HOSTED.with_name(".relay.lock").unlink(missing_ok=True)
        done["left"] = 0
        return done


def relay_hosted() -> dict:
    """The tunnels hosted here: whose (the link, its workspace), the port,
    the names, and whether the tunnel listens now. It reads only."""
    listening = _listening()
    return {"hosted": {k: {"link": r.get("link"), "workspace": r.get("workspace"), "port": r.get("port"),
                           "names": r.get("names") or [], "redirects": r.get("redirects") or {},
                           "created": r.get("created"), "listening": int(r.get("port") or 0) in listening}
                       for k, r in sorted(_hosted().items())}}


def relay_local_port(name: str, make: bool = True) -> int:
    """The port of this loopback where the workspace's nginx takes its
    tunnel's requests, kept in /etc/omniacli/relay/<workspace>/port."""
    _name("workspace", name)
    path = TUNNELS / name / "port"
    if path.is_file() and not path.is_symlink():
        try:
            return relay.check_port(path.read_text(encoding="ascii").strip(), relay.LOCAL_PORTS)
        except relay.RelayError:
            pass
    if not make:
        raise HelperError(f"workspace {name} has no tunnel yet: its key and port are made first (relay-key)")
    taken = set()
    for other in TUNNELS.glob("*/port") if TUNNELS.is_dir() else ():
        try:
            taken.add(int(other.read_text(encoding="ascii").strip()))
        except (OSError, ValueError):
            continue
    busy = _listening()
    port = next((p for p in relay.LOCAL_PORTS if p not in taken and p not in busy and _free(p)), None)
    if port is None:
        raise HelperError(f"no port free in {relay.LOCAL_PORTS.start}-{relay.LOCAL_PORTS.stop - 1}")
    _write(_tunnel_folder(name) / "port", f"{port}\n")
    return port


def _tunnel_folder(name: str) -> Path:
    _name("workspace", name)
    for folder in (TUNNELS.parent, TUNNELS, TUNNELS / name):
        if folder.is_symlink():
            raise HelperError(f"{folder} is a link: nothing is written")
        folder.mkdir(mode=0o755, exist_ok=True)
    for folder in (TUNNELS, TUNNELS / name):
        os.chown(folder, 0, 0)
        os.chmod(folder, 0o755)
    return TUNNELS / name


def relay_key(name: str) -> dict:
    """The tunnel's key of the workspace (made the first time: ed25519, root's
    600, never sent: its public half only) and the port of this loopback its
    requests come in on."""
    folder = _tunnel_folder(name)
    key = folder / "id_ed25519"
    if not key.is_file() or not key.with_suffix(".pub").is_file():
        key.unlink(missing_ok=True)
        key.with_suffix(".pub").unlink(missing_ok=True)
        _run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"omniacli-relay@{socket.gethostname()}:{name}",
             "-f", str(key))
        os.chmod(key, 0o600)
    return {"workspace": name, "public_key": relay.check_public_key(key.with_suffix(".pub").read_text(encoding="utf-8")),
            "local_port": relay_local_port(name)}


def relay_tunnel(name: str, mode: str) -> dict:
    """on: the unit that keeps the workspace's tunnel to its relay up
    (omniacli-relay-<workspace>.service; its values on the input: relay, host,
    ssh_port, remote_port, host_key), started again now; off: the unit
    stopped and taken away, with the key, the relay's host key and the port."""
    if mode not in ("on", "off"):
        raise HelperError("usage: relay-tunnel <workspace> on|off")
    unit = SYSTEMD / relay.unit_name(_name("workspace", name))
    if mode == "off":
        removed = []
        if unit.exists():
            _run("systemctl", "disable", "--now", unit.name, check=False)
            unit.unlink()
            _run("systemctl", "daemon-reload")
            removed.append(str(unit))
        folder = TUNNELS / name
        if folder.is_dir() and not folder.is_symlink():
            for item in ("id_ed25519", "id_ed25519.pub", "known_hosts", "port"):
                if (folder / item).exists():
                    (folder / item).unlink()
                    removed.append(str(folder / item))
            folder.rmdir()
            removed.append(str(folder))
        if TUNNELS.is_dir() and not any(TUNNELS.iterdir()):
            TUNNELS.rmdir()
            # the last tunnel: its user goes too
            try:
                pwd.getpwnam(relay.TUNNEL_USER)
            except KeyError:
                pass
            else:
                _run("userdel", relay.TUNNEL_USER)
                removed.append(f"the user {relay.TUNNEL_USER}")
        return {"workspace": name, "unit": unit.name, "removed": removed}
    spec = _input_json("the tunnel")
    folder = _tunnel_folder(name)
    if not (folder / "id_ed25519").is_file():
        raise HelperError(f"workspace {name} has no tunnel key yet (relay-key)")
    try:
        host, ssh_port = relay.check_host(spec.get("host")), relay.check_port(spec.get("ssh_port"))
        line = relay.known_hosts_line(host, ssh_port, spec.get("host_key"))
        text = relay.tunnel_unit_text(name, str(spec.get("relay") or ""), host, ssh_port, spec.get("remote_port"),
                                      relay_local_port(name, make=False), str(folder))
    except relay.RelayError as exc:
        raise HelperError(str(exc)) from None
    try:
        user = pwd.getpwnam(relay.TUNNEL_USER)
    except KeyError:
        _run("useradd", "--system", "--user-group", "--home-dir", "/nonexistent", "--no-create-home",
             "--shell", "/usr/sbin/nologin", "--comment", "OmniaCLI, tunnels to a relay", relay.TUNNEL_USER)
        user = pwd.getpwnam(relay.TUNNEL_USER)
    # the key the tunnel's ssh reads: the tunnels' user's, nobody else's
    os.chown(folder / "id_ed25519", user.pw_uid, user.pw_gid)
    os.chmod(folder / "id_ed25519", 0o600)
    known = folder / "known_hosts"
    changed = not unit.is_file() or unit.read_text(encoding="utf-8") != text \
        or not known.is_file() or known.read_text(encoding="utf-8") != line
    _write(known, line)
    _write(unit, text)
    _run("systemctl", "daemon-reload")
    _run("systemctl", "enable", unit.name)
    # the same tunnel published again (another name of the workspace): its connection is not cut
    if changed or _run("systemctl", "is-active", unit.name, check=False).strip() != "active":
        _run("systemctl", "restart", unit.name)
    return {"workspace": name, "unit": unit.name, "changed": changed, "host": host, "ssh_port": ssh_port}


ACTIONS = {"disk": (disk, 3), "drop": (drop, 2), "limits": (limits, 3), "create": (create, 5), "remove": (remove, 3),
           "web": (web, 3), "web-names": (web_taken, 1), "upstream": (upstream, 6), "owncert": (owncert, 2), "firewall": (firewall, 1), "template-install": (template_install, 1),
           "conf": (conf, 5), "space": (space, 0),
           "storage-setup": (storage_setup, 0), "storage-check": (storage_check, 1), "storage-reset": (storage_reset, 1),
           "pitr-enable": (pitr_enable, 4), "pitr-base": (pitr_base, 4), "pitr-state": (pitr_state, 4),
           "pitr-restore": (pitr_restore, 7), "login-path": (login_path, 2),
           "portal": (portal, 1), "portal-names": (portal_taken, 0),
           "machine-db-create": (machine_db_create, 2), "machine-db-remove": (machine_db_remove, 1),
           "machine-db-state": (machine_db_state, 0), "machine-db-mounts": (machine_db_mounts, 0),
           "machine-db-move": (machine_db_move, 4), "machine-db-purge": (machine_db_purge, 3),
           "machine-db-grant": (machine_db_grant, 1), "machine-db-ungrant": (machine_db_ungrant, 3),
           "machine-db-port": (machine_db_port, 2),
           "machine-db-archive-state": (machine_db_archive_state, 1), "machine-db-archive-on": (machine_db_archive_on, 5),
           "machine-db-archive-off": (machine_db_archive_off, 1), "machine-db-archive-base": (machine_db_archive_base, 1),
           "machine-db-recover": (machine_db_recover, 5), "machine-db-recover-sweep": (machine_db_recover_sweep, 0),
           "machine-db-replica-facts": (machine_db_replica_facts, 1), "machine-db-replica-allow": (machine_db_replica_allow, 1),
           "machine-db-replica-ready": (machine_db_replica_ready, 1), "machine-db-replica-state": (machine_db_replica_state, 1),
           "machine-db-replica-cap": (machine_db_replica_cap, 2), "machine-db-replica-drop": (machine_db_replica_drop, 1),
           "machine-db-replica-replaced": (machine_db_replica_replaced, 1),
           "machine-db-replica-read": (machine_db_replica_read, 4), "machine-db-replica-follow": (machine_db_replica_follow, 3),
           "machine-db-replica-make": (machine_db_replica_make, 2), "machine-db-replica-hosted": (machine_db_replica_hosted, 1),
           "machine-db-replica-sync": (machine_db_replica_sync, 1), "machine-db-replica-promote": (machine_db_replica_promote, 1),
           "machine-db-replica-remove": (machine_db_replica_remove, 1),
           "machine-db-replica-return": (machine_db_replica_return, 2), "machine-db-return": (machine_db_return, 2),
           "machine-db-replica-back": (machine_db_replica_back, 3),
           "machine-db-replica-grant": (machine_db_replica_grant, 3), "machine-db-moved": (machine_db_moved, 3),
           "machine-db-replica-lent": (machine_db_replica_lent, 1),
           "machine-db-replica-unlisten": (machine_db_replica_unlisten, 1),
           "enterprise-sync": (enterprise_sync, 1), "enterprise": (enterprise_switch, 4),
           "relay-facts": (relay_facts, 0), "relay-host": (relay_host, 1), "relay-unhost": (relay_unhost, 1),
           "relay-hosted": (relay_hosted, 0), "relay-key": (relay_key, 1), "relay-tunnel": (relay_tunnel, 2)}


def run(argv: list[str]) -> dict:
    if not argv or argv[0] not in ACTIONS:
        raise HelperError(f"usage: workspace-helper {' | '.join(ACTIONS)} ...")
    action, count = ACTIONS[argv[0]]
    if len(argv) - 1 != count:
        raise HelperError(f"{argv[0]} takes {count} arguments (workspace-helper --help: README, Workspaces)")
    if os.geteuid() != 0:
        raise HelperError("run it as root, through sudo")
    return action(*argv[1:])


# the SSH keys of the people who log in to the machine (README, SSH access; sshkeys.py)
ACTIONS.update({"ssh-keys": (ssh_keys, 0), "ssh-key-add": (ssh_key_add, 1), "ssh-key-remove": (ssh_key_remove, 2)})


def os_updates() -> dict:
    """os-updates: unattended-upgrades on or off, the restart, the last run; it reads only."""
    return osupdates.state()


def os_updates_set(mode: str, policy: str) -> dict:
    """os-updates-set on|off never|<HH:MM>: the switch and the restart written, the state read again."""
    if mode not in ("on", "off"):
        raise HelperError("os-updates-set on|off never|<HH:MM>")
    try:
        return osupdates.apply(mode == "on", policy)
    except (osupdates.UpdatesRefused, OSError) as exc:
        raise HelperError(str(exc)) from None


# the automatic security updates of the system (README; osupdates.py)
ACTIONS.update({"os-updates": (os_updates, 0), "os-updates-set": (os_updates_set, 2)})


def main(argv: list[str]) -> int:
    try:
        print(json.dumps(run(argv)))
    except (HelperError, machinedb.ClusterError) as exc:
        print(f"workspace-helper: {exc}", file=sys.stderr)
        return 2
    return 0

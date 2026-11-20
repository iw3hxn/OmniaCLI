#!/bin/bash
# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Prepare a clean Ubuntu or Debian machine for OmniaCLI (machines.py, README
# Machines): run as root through SSH by another OmniaCLI, this text on bash's
# input (bash -s), after a line that sets its arguments (never on a command
# line, which ps shows):
#
#   set -- <package URL> <SSH port> <firewall 1|0> <link user> <link name> <link token hash> \
#          <authorized_keys line of the link, base64> <clean|existing>
#
# clean: a new machine, as below. existing: a machine that already runs Odoo
# (a hosting panel's server, one installed by hand): nothing of it is upgraded,
# installed beside it or closed (no nginx, Docker, workspaces nor firewall);
# OmniaCLI and the link back as below, then its Odoo made ready for
# OmniaCLI (omniacli/odoo/adopt.py: read access by ACLs, click-odoo, sudoers).
#
# 1. the system: a supported one, its updates, unattended upgrades;
# 2. what the workspaces need: nginx, certbot, Docker with its compose plugin;
# 3. OmniaCLI from the package, with the workspaces allowed;
# 4. the link back: a user that may only forward to OmniaCLI (the Links
#    page's authorized_keys line and Match block), and the link's token hash
#    in OmniaCLI's users file, as admin;
# 5. the firewall (ufw): SSH, 80 and 443 only.
#
# Run again, it does the same: every step is idempotent. Its last line says
# it is done: OMNIACLI-PROVISIONED.
set -euo pipefail

# bash reads this text from its input: the whole of it is read (main's
# definition) before anything runs, and nothing it runs reads the rest
main() {

    PACKAGE_URL="$1"
    SSH_PORT="$2"
    FIREWALL="$3"
    LINK_USER="$4"
    LINK_NAME="$5"
    LINK_HASH="$6"
    LINK_LINE="$(printf '%s' "$7" | base64 -d)"
    MODE="${8:-clean}"
    export DEBIAN_FRONTEND=noninteractive
    APT_OPTIONS=(-y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

    step() { echo "--- $*"; }
    fail() { echo "PROVISION FAILED: $*"; exit 1; }

    step "the system"
    [ "$(id -u)" = 0 ] || fail "not root"
    # shellcheck source=/dev/null
    . /etc/os-release
    case "$ID:$VERSION_ID" in
        ubuntu:22.04|ubuntu:24.04|debian:11|debian:12|debian:13) echo "$PRETTY_NAME" ;;
        *) fail "$PRETTY_NAME is not supported: Ubuntu 22.04 or 24.04, Debian 11, 12 or 13" ;;
    esac
    case "$(dpkg --print-architecture)" in
        amd64|arm64) ;;
        *) fail "$(dpkg --print-architecture): amd64 or arm64 only" ;;
    esac
    if [ "$MODE" = clean ]; then
        # a machine that already serves the web is not a clean one: its sites would meet the workspaces'
        for site in /etc/apache2/sites-enabled/*; do
            [ -e "$site" ] && fail "apache serves sites here ($site): a clean machine is needed, or choose a server that already has Odoo"
        done
    fi

    if [ "$MODE" = existing ]; then
        # what OmniaCLI itself needs, nothing upgraded: the machine is someone's at work
        step "updates"
        apt-get update -q
        apt-get install "${APT_OPTIONS[@]}" ca-certificates curl acl python3-venv
    else
        step "updates"
        apt-get update -q
        apt-get upgrade "${APT_OPTIONS[@]}"
        # rclone: the backups' remote destinations (SFTP, FTP, S3, Drive...)
        apt-get install "${APT_OPTIONS[@]}" ca-certificates curl ufw unattended-upgrades rclone
        # the security updates by themselves, every day
        printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' \
            > /etc/apt/apt.conf.d/20auto-upgrades

        step "nginx, certbot and Docker"
        apt-get install "${APT_OPTIONS[@]}" nginx certbot
        command -v docker >/dev/null || apt-get install "${APT_OPTIONS[@]}" docker.io
        # Debian 13 keeps the docker command apart from the daemon (docker-cli)
        command -v docker >/dev/null || apt-get install "${APT_OPTIONS[@]}" docker-cli
        if ! docker compose version >/dev/null 2>&1; then
            # Ubuntu's, Docker's own, Debian 13's (docker-compose 2.x, the plugin)
            for compose in docker-compose-v2 docker-compose-plugin docker-compose; do
                apt-get install "${APT_OPTIONS[@]}" "$compose" >/dev/null 2>&1 && break
            done
        fi
        docker compose version 2>/dev/null \
            || echo "WARNING: no docker compose here (Debian 12: install Docker's docker-ce and docker-compose-plugin)"
    fi

    step "OmniaCLI"
    work="$(mktemp -d)"
    trap 'rm -rf "$work"' EXIT
    curl -fsSL --retry 3 -o "$work/omniacli.deb" "$PACKAGE_URL" || fail "cannot download $PACKAGE_URL"
    [ "$(dpkg-deb -f "$work/omniacli.deb" Package 2>/dev/null)" = omniacli ] || fail "$PACKAGE_URL is no omniacli package"
    apt-get install "${APT_OPTIONS[@]}" "$work/omniacli.deb"
    if [ "$MODE" = clean ]; then
        grep -q '^OMNIACLI_WORKSPACES=1' /etc/default/omniacli || echo OMNIACLI_WORKSPACES=1 >> /etc/default/omniacli
        dpkg-reconfigure -f noninteractive omniacli
    fi

    step "the link back"
    if ! id "$LINK_USER" >/dev/null 2>&1; then
        adduser --system --group --home "/home/$LINK_USER" --shell /usr/sbin/nologin "$LINK_USER"
    fi
    # no password, but not locked: sshd refuses the key of a locked account without PAM
    usermod -p '*' "$LINK_USER"
    install -d -m 700 -o "$LINK_USER" -g "$LINK_USER" "/home/$LINK_USER/.ssh"
    printf '%s\n' "$LINK_LINE" > "/home/$LINK_USER/.ssh/authorized_keys"
    chown "$LINK_USER:$LINK_USER" "/home/$LINK_USER/.ssh/authorized_keys"
    chmod 600 "/home/$LINK_USER/.ssh/authorized_keys"
    permit="$(printf '%s' "$LINK_LINE" | sed -n 's/.*permitopen="\([^"]*\)".*/\1/p')"
    block="# OmniaCLI link: $LINK_USER may only forward to OmniaCLI
    Match User $LINK_USER
        AllowTcpForwarding local
        PermitOpen $permit
        PermitListen none
        ForceCommand /bin/false
        PermitTTY no
        X11Forwarding no
        AllowAgentForwarding no
        AllowStreamLocalForwarding no
        PermitTunnel no
    # end of OmniaCLI link"
    if ! grep -q "^# OmniaCLI link: $LINK_USER " /etc/ssh/sshd_config; then
        cp /etc/ssh/sshd_config /etc/ssh/sshd_config.omniacli-before
        printf '\n%s\n' "$block" >> /etc/ssh/sshd_config
        if ! sshd -t; then
            mv /etc/ssh/sshd_config.omniacli-before /etc/ssh/sshd_config
            fail "sshd -t refused the Match block: sshd_config put back"
        fi
    fi
    systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true

    step "the link's token"
    service_user="$(sed -n 's/^[[:space:]]*OMNIACLI_USER=//p' /etc/default/omniacli | tail -n 1 | tr -d "\"'")"
    runuser -u "${service_user:-omniacli}" -- /opt/omniacli/venv/bin/python -I - "$LINK_NAME" "$LINK_HASH" <<'PY'
import sys

from omniacli.config import LinkAccess, load_access, load_settings, save_access

name, token_hash = sys.argv[1], sys.argv[2]
settings = load_settings("/etc/omniacli/omniacli.yaml")
access = load_access(settings.users_file)
access.links[name] = LinkAccess(token=token_hash, role="admin")
save_access(settings.users_file, access)
print(f"link {name} allowed in, as admin, in {settings.users_file}")
PY
    systemctl restart omniacli
    for _ in $(seq 60); do curl -s -o /dev/null http://127.0.0.1:8090/ && break; sleep 1; done
    curl -s -o /dev/null http://127.0.0.1:8090/ || fail "OmniaCLI does not answer on 127.0.0.1:8090"

    if [ "$MODE" = existing ]; then
        step "the Odoo instances"
        /opt/omniacli/venv/bin/python -I -m omniacli.odoo.adopt "${service_user:-omniacli}"
        # the service reads the configs with the access just given
        systemctl restart omniacli
        for _ in $(seq 60); do curl -s -o /dev/null http://127.0.0.1:8090/ && break; sleep 1; done
    fi

    if [ "$FIREWALL" = 1 ] && [ "$MODE" = clean ]; then
        step "the firewall"
        ufw allow "$SSH_PORT/tcp" >/dev/null
        ufw allow 80/tcp >/dev/null
        ufw allow 443/tcp >/dev/null
        ufw --force enable
        ufw status verbose | sed -n '1,12p'
    fi

    echo "OMNIACLI-PROVISIONED"
}

main "$@" </dev/null

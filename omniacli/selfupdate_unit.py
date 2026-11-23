# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The install of an OmniaCLI package, run by systemd in a transient unit of
its own (selfupdate.py starts it with systemd-run), so that it outlives the
OmniaCLI it replaces: apt stops OmniaCLI, rebuilds its venv and starts it
again, while this script goes on.

It is copied by root into the folder of the updates (selfupdate.STATE) and
run there with the system's python3 (3.8 or newer, the standard library
only): never from OmniaCLI's venv, which the install replaces.

    python3 -I unit.py <run file>

The run file (JSON, written by root) names the job, the package to install,
the versions from and to, the package to go back to if the install fails,
and where to write. The script writes, next to it:

- <job>.log: what it does, line by line (the job shows it once OmniaCLI is back);
- <job>.apt.log: apt-get's output;
- <job>.outcome.json: the facts before and after (OmniaCLI's version and
  state, nginx -t and its files, the start times of the running Odoo,
  PostgreSQL and nginx units and of the containers, the login page), how
  long the panel was away, and whether it went well. OmniaCLI, restarted,
  reads it and finishes the job.
"""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

APT_OPTIONS = ["-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold"]
# how long OmniaCLI has to answer again after the install (the venv is built during it)
BACK_TIMEOUT = 300
PING_EVERY = 0.5


def now():
    return datetime.now().isoformat(timespec="seconds")


class Unit:
    def __init__(self, run_file):
        self.run_file = run_file
        with open(run_file, encoding="utf-8") as fh:
            self.run = json.load(fh)
        folder = os.path.dirname(run_file)
        job = self.run["job"]
        self.log_path = os.path.join(folder, job + ".log")
        self.apt_path = os.path.join(folder, job + ".apt.log")
        self.outcome_path = os.path.join(folder, job + ".outcome.json")
        self.port = int(self.run.get("port") or 8090)
        self.down_at = None
        self.up_at = None
        self.stop = False

    def say(self, text):
        line = datetime.now().strftime("%H:%M:%S") + " " + text
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        print(text, flush=True)

    # --- the facts -------------------------------------------------------------

    def login_code(self, timeout=5):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/login" % self.port, timeout=timeout) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except Exception:  # noqa: BLE001 - down: no answer at all
            return 0

    @staticmethod
    def out(cmd, timeout=60):
        try:
            done = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                                  universal_newlines=True)
            return done.returncode, done.stdout.strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 127, str(exc)

    def facts(self):
        found = {}
        code, version = self.out(["dpkg-query", "-W", "-f=${Version}", "omniacli"])
        _, state = self.out(["systemctl", "is-active", "omniacli"])
        found["omniacli"] = {"version": version if code == 0 else "", "state": state}
        if os.path.isdir("/etc/nginx"):
            digest = hashlib.sha256()
            for root, dirs, files in os.walk("/etc/nginx"):
                dirs.sort()
                for name in sorted(files):
                    path = os.path.join(root, name)
                    try:
                        with open(path, "rb") as fh:
                            digest.update(path.encode() + b"\0" + hashlib.sha256(fh.read()).digest())
                    except OSError:
                        continue
            code, said = self.out(["nginx", "-t"])
            found["nginx"] = {"test": "ok" if code == 0 else (said.splitlines() or ["failed"])[-1],
                              "files": digest.hexdigest()[:12]}
        code, units = self.out(["systemctl", "list-units", "--type=service", "--state=running", "--no-legend",
                                "--plain"])
        started = {}
        if code == 0:
            for line in units.splitlines():
                name = line.split(" ", 1)[0]
                if any(word in name.lower() for word in ("odoo", "postgres", "nginx")):
                    # @<seconds since 1970> (systemd 247 and later), else as systemctl writes it
                    code, when = self.out(["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value",
                                           "--timestamp=unix", name])
                    if code:
                        _, when = self.out(["systemctl", "show", "-p", "ActiveEnterTimestamp", "--value", name])
                    started[name] = when
        found["units"] = started
        containers = {}
        code, names = self.out(["docker", "ps", "--format", "{{.Names}}"])
        if code == 0 and names:
            for name in sorted(names.split()):
                _, when = self.out(["docker", "inspect", "-f", "{{.State.StartedAt}}", name])
                containers[name] = when
        found["containers"] = containers
        found["login"] = self.login_code()
        return found

    # --- the install -----------------------------------------------------------

    def ping(self):
        """The login page every half second: when it stopped answering and
        when it answered again."""
        while not self.stop:
            code = self.login_code(timeout=2)
            moment = time.monotonic()
            if code == 0 and self.down_at is None:
                self.down_at = moment
            elif code and self.down_at is not None and self.up_at is None:
                self.up_at = moment
            time.sleep(PING_EVERY)

    def apt(self, deb, downgrade):
        cmd = ["apt-get", "install", "-y", "-q", *APT_OPTIONS]
        if downgrade:
            cmd.append("--allow-downgrades")
        cmd.append(deb)
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        self.say("$ " + " ".join(cmd))
        with open(self.apt_path, "a", encoding="utf-8") as fh:
            try:
                return subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, env=env, timeout=3600).returncode
            except (OSError, subprocess.TimeoutExpired) as exc:
                fh.write("%s\n" % exc)
                return 127

    def wait_back(self, version):
        """OmniaCLI of version active and its login page answering."""
        deadline = time.monotonic() + BACK_TIMEOUT
        while time.monotonic() < deadline:
            _, state = self.out(["systemctl", "is-active", "omniacli"])
            code, installed = self.out(["dpkg-query", "-W", "-f=${Version}", "omniacli"])
            if state == "active" and installed == version and self.login_code() in (200, 303):
                return True
            time.sleep(1)
        return False

    def apt_tail(self, lines=12):
        try:
            with open(self.apt_path, encoding="utf-8", errors="replace") as fh:
                return [line.rstrip() for line in fh.readlines()[-lines:]]
        except OSError:
            return []

    def main(self):
        run = self.run
        outcome = {"job": run["job"], "mode": run["mode"], "from": run["from"], "to": run["to"],
                   "started": now(), "ok": False, "error": "", "warnings": [], "went_back": False}
        self.say("--- the facts before")
        outcome["before"] = self.facts()
        pinger = threading.Thread(target=self.ping, daemon=True)
        pinger.start()
        began = time.monotonic()
        self.say("--- install OmniaCLI %s (it was %s)" % (run["to"], run["from"]))
        code = self.apt(run["deb"], downgrade=run["mode"] == "back")
        outcome["apt_exit"] = code
        back = code == 0 and self.wait_back(run["to"])
        if not back:
            outcome["error"] = ("apt-get ended with %d" % code) if code else \
                "OmniaCLI %s did not answer within %d s of the install" % (run["to"], BACK_TIMEOUT)
            self.say("FAILED: " + outcome["error"])
            previous = run.get("previous_deb")
            if previous:
                # the version of before, from its kept package: the panel comes back as it was
                self.say("--- back to OmniaCLI %s, from its kept package" % run["from"])
                again = self.apt(previous, downgrade=True)
                self.out(["systemctl", "restart", "omniacli"])
                outcome["went_back"] = again == 0 and self.wait_back(run["from"])
                self.say("OmniaCLI %s is back" % run["from"] if outcome["went_back"] else
                         "the version of before could not be installed again: see %s" % self.apt_path)
        self.stop = True
        pinger.join(timeout=5)
        if self.down_at is not None:
            end = self.up_at if self.up_at is not None else time.monotonic()
            outcome["away_seconds"] = round(end - self.down_at, 1)
        outcome["install_seconds"] = round(time.monotonic() - began, 1)
        self.say("--- the facts after")
        outcome["after"] = self.facts()
        before, after = outcome["before"], outcome["after"]
        if back:
            outcome["ok"] = True
            for what in ("units", "containers"):
                for name, when in before.get(what, {}).items():
                    if after.get(what, {}).get(name) != when:
                        outcome["warnings"].append("%s %s: started %s, now %s" % (
                            what[:-1], name, when, after.get(what, {}).get(name) or "not running"))
            if before.get("nginx") != after.get("nginx"):
                outcome["warnings"].append("nginx: %s, now %s" % (before.get("nginx"), after.get("nginx")))
        outcome["apt_tail"] = self.apt_tail()
        outcome["ended"] = now()
        tmp = self.outcome_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(outcome, fh, indent=1)
        os.chmod(tmp, 0o644)
        os.replace(tmp, self.outcome_path)
        self.say("--- done: " + ("OmniaCLI %s" % run["to"] if back else "failed"))
        return 0 if back else 1


if __name__ == "__main__":
    sys.exit(Unit(sys.argv[1]).main())

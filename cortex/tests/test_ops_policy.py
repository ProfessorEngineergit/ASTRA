"""Command-Policy: die Sicherheitsgrenze für alles, was ASTRA im HomeLab ausführt.
Bahrians Regel: Allow-List autonom, alles andere fragt, Destruktives nie."""
import pytest

from app import jobs, ops_policy as p


@pytest.mark.parametrize("cmd", [
    "uptime", "df -h", "free -m", "docker ps", "docker logs cortex --tail 50",
    "systemctl status docker", "journalctl -u docker -n 20", "git status",
    "docker restart cortex", "cat /etc/hostname", "pct list",
])
def test_harmless_status_commands_run_autonomously(cmd):
    assert p.classify(cmd)[0] == p.ALLOW


@pytest.mark.parametrize("cmd", [
    "rm -rf /", "rm -rf /opt/astra", "mkfs.ext4 /dev/sda1",
    "dd if=/dev/zero of=/dev/sda", "zpool destroy tank", "zfs destroy tank/data",
    "docker volume rm astra_postgres_data", "docker system prune -a",
    "shred /dev/sda", "wipefs -a /dev/sdb", ":(){ :|:& };:",
    "cat /etc/shadow", "userdel bahrian",
])
def test_destructive_commands_are_hard_blocked(cmd):
    assert p.classify(cmd)[0] == p.BLOCK


@pytest.mark.parametrize("cmd", [
    "apt install nginx", "systemctl restart nginx", "nano /etc/fstab",
    "docker compose up -d", "chmod 600 /root/.ssh/id_rsa",
])
def test_unknown_or_mutating_commands_need_approval(cmd):
    assert p.classify(cmd)[0] == p.APPROVE


def test_sudo_always_needs_approval_even_for_allowlisted():
    assert p.classify("sudo uptime")[0] == p.APPROVE


def test_chaining_cannot_smuggle_past_the_allowlist():
    # "uptime" allein wäre erlaubt — verkettet niemals.
    assert p.classify("uptime; rm -rf /tmp/x")[0] == p.BLOCK   # destruktiv gewinnt
    assert p.classify("uptime && apt install x")[0] == p.APPROVE
    assert p.classify("uptime | mail bahrian")[0] == p.APPROVE
    assert p.classify("cat /var/log/syslog > /tmp/x")[0] == p.APPROVE
    # Zugangsdaten-Dateien sind auch lesend tabu, egal wie verpackt.
    assert p.classify("cat /etc/passwd > /tmp/x")[0] == p.BLOCK


def test_empty_command_is_blocked():
    assert p.classify("")[0] == p.BLOCK


# ─── Rechte-Umschlag der Jobs ─────────────────────────────────────────────────
def test_envelope_blocks_hosts_outside_the_scope():
    env = {"hosts": ["pve"], "write": True}
    steps = [{"command": "docker ps", "host": "pve"},
             {"command": "docker ps", "host": "nas"}]
    got = jobs.review_steps(steps, env)
    assert got[0]["decision"] == p.ALLOW
    assert got[1]["decision"] == p.BLOCK


def test_empty_envelope_allows_nothing():
    got = jobs.review_steps([{"command": "uptime", "host": "pve"}], {"hosts": []})
    assert got[0]["decision"] == p.BLOCK


def test_read_only_envelope_downgrades_mutating_steps_to_approval():
    env = {"hosts": ["pve"], "write": False}
    got = jobs.review_steps([{"command": "docker restart cortex", "host": "pve"}], env)
    assert got[0]["decision"] == p.APPROVE


def test_read_only_envelope_still_allows_reads():
    env = {"hosts": ["pve"], "write": False}
    got = jobs.review_steps([{"command": "docker ps", "host": "pve"}], env)
    assert got[0]["decision"] == p.ALLOW


def test_wildcard_envelope():
    assert jobs.envelope_allows({"hosts": ["*"]}, "anything") is True

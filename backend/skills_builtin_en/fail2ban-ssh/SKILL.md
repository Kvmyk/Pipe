---
name: fail2ban-ssh
description: Install fail2ban and ban addresses that guess SSH passwords.
---

# fail2ban for SSH

## 1. Check (reads)
- security_audit: is password login enabled (then fail2ban matters; with keys only — less so).
- SSH port: `grep -i '^Port' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf` (default 22).
- The address the user connects from (so you do not ban them): `last -n 5 -a` or `who`.
- In docker mode give the commands from steps 2-3 to be run on the host.

## 2. Install
`apt-get install -y fail2ban` (Debian/Ubuntu); `dnf install -y fail2ban` (RHEL/Fedora).

## 3. Configuration — /etc/fail2ban/jail.local (write_file)
```
[DEFAULT]
bantime = 1h
findtime = 10m
maxretry = 5
ignoreip = 127.0.0.1/8 ::1 USER_ADDRESS

[sshd]
enabled = true
port = SSH_PORT
backend = systemd
```
On older systems with /var/log/auth.log use `backend = auto`.
Then: `systemctl enable --now fail2ban && systemctl restart fail2ban`.

## 4. Verification
`fail2ban-client status sshd` — the jail is active, number of banned addresses. Note it in SERVER.md.

Unban an address: `fail2ban-client set sshd unbanip ADDRESS`. Undo: /undo and `systemctl disable --now fail2ban`.

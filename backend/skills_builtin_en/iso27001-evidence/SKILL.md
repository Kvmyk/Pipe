---
name: iso27001-evidence
description: Read-only evidence report for the ISO/IEC 27001:2022 Annex A technological controls this server can show (access, vulnerabilities, backup, logs, network, TLS). Not a certification.
---

# ISO/IEC 27001:2022 evidence (read-only)

Gather technical evidence from this server for selected Annex A controls and write it up as a report.
Change NOTHING: only reads. The only write allowed is saving the report with write_file, and only when
the user asks for it (it goes through confirmation).

## Rules for the report
- Say at the top: "Technical evidence snapshot for selected ISO/IEC 27001:2022 Annex A controls. This is not a
  certification, not a full ISMS audit and not a statement of conformity. Organisational, people and physical
  controls (5.x, 6.x, 7.x) are out of scope."
- Never write that the server or the company is "compliant", "conformant" or "certified". Use only the
  statuses: met, partial, gap, no evidence.
- A command that fails, is not allowed or returns nothing = "no evidence" (with the reason). Do not guess.
- Never quote secrets, password hashes, keys or tokens. Fingerprints and counts are enough.
- Header: date and time (`date -Is`) and host name (`hostname -f`, fallback `hostname`), Pipe version if known.
- Retention periods (logs, backups) and who may have access are policy decisions: mark them
  "to be confirmed by a responsible person" and never decide them yourself.

## Steps
0. Header: `date -Is`, `hostname -f`, `cat /etc/os-release`, `uptime -s`.
1. security_audit: score and findings; most controls below take their evidence from it.
2. Evidence per control:
   - A.8.5 Secure authentication: from the audit, SSH password login and root login; recent logins by key:
     `grep -h 'Accepted' /var/log/auth.log /var/log/secure 2>/dev/null | tail -5` (or `journalctl -u ssh -u sshd -n 50`).
   - A.5.15 / A.5.18 / A.8.2 Access rights and privileged access: accounts with a login shell
     `getent passwd | awk -F: '$7 !~ /(nologin|false)$/ {print $1, $3, $7}'`, members of `getent group sudo wheel`,
     SSH key fingerprints and new users from server_history (last 30 days).
   - A.8.7 Protection against malware: only whether a scanner is installed and running (e.g. clamav); none = "no evidence".
   - A.8.8 Management of technical vulnerabilities: vulnerability findings from security_audit (KEV and critical first),
     automatic updates (unattended-upgrades / dnf-automatic), pending reboot (`/run/reboot-required`).
   - A.8.9 Configuration management and A.8.32 Change management: server_history (changes over 30 days) and journal
     (what Pipe changed, when, how to undo it). Changes made outside Pipe show up only in server_history.
   - A.8.13 Information backup: DIRECTORY entries of kind [backup] and backup alerts in the prompt. Without them look
     for traces: `ls -la /var/backups`, `crontab -l`, `ls /etc/cron.d /etc/cron.daily`, `systemctl list-timers --all`
     (no systemd: skip it). Name the newest backup file and its age (`ls -lt --time-style=long-iso <dir> | head`).
     Nothing found = gap. A backup older than its schedule = partial. Restore tests cannot be seen from here: "no evidence".
   - A.8.15 Logging: persistent journald (`ls -d /var/log/journal`), rsyslog / auditd active, logrotate
     (`ls /etc/logrotate.d`); retention to be confirmed by a responsible person.
   - A.8.16 Monitoring activities: Pipe's watcher (alerts in the prompt, morning digest), webhooks if any.
   - A.8.17 Clock synchronisation: `timedatectl show -p NTPSynchronized -p Timezone`
     (no systemd: `ps -e | grep -E 'chrony|ntp'`).
   - A.8.20 / A.8.21 Network security: firewall from the audit, listening ports `ss -tlnpu`, ports published by
     containers (`docker ps --format '{{.Names}} {{.Ports}}'` if docker is present).
   - A.8.24 Use of cryptography: certificate expiry and HTTPS from the audit and alerts, SSH algorithms
     `sshd -T 2>/dev/null | grep -iE '^(ciphers|kexalgorithms|macs) '`.
3. Report as a table: control | status | evidence (command + short result) | what to do. Fixes point to existing
   tools: `/patch` (security_patch), the skills harden-ssh, fail2ban-ssh, backup-postgres. Then a short list of gaps,
   most important first, and the list of points to be confirmed by a responsible person.
4. Ask whether to save the report (e.g. /root/iso27001-evidence-<date>.md). Only after a yes: write_file.

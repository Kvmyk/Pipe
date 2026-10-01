---
name: harden-ssh
description: Safely disable SSH password login and root password login — in an order that does not lock the user out.
---

# Hardening SSH without locking yourself out

Order matters: the key first, then disable passwords.

## 1. Check (reads)
- security_audit — what needs fixing (password login, root).
- Does the user have a working key: `ls -la /root/.ssh/authorized_keys /home/*/.ssh/authorized_keys`
  and `last -n 10 -a` — were the last logins by key (`grep 'Accepted publickey' /var/log/auth.log | tail -3`).
- If there is NO key — stop. Ask the user for their public key (`cat ~/.ssh/id_ed25519.pub` on the
  laptop) and add it (write_file to authorized_keys — show the content for approval).
- Ask the user to log in with the key in a SECOND terminal window and confirm that it works.

## 2. Change (after YES)
Use the command from security_audit (the file /etc/ssh/sshd_config.d/00-pipe-*.conf or sed on sshd_config).
The safety fuse makes a backup, runs `sshd -t` before the reload and checks that the ssh service is active.
A reload does not drop existing sessions.

## 3. Verification
- The user opens a NEW SSH session with the key — it must work before they close the old one.
- security_audit — the score should go up.

Undo: /undo (restores the configuration and reloads sshd).

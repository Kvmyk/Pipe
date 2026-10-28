"""
English tool descriptions (PIPE_LANG=en).

Only descriptions are translated — `english_tools()` copies the schemas from
core/tools.py (names, types, enums, required fields stay identical) and swaps the
texts. A missing translation falls back to Polish.

Pipe v0.49.2
"""

from __future__ import annotations

import copy

_CONFIRM = ("Optional. true forces a confirmation prompt, also for a read or a routine change. Usually unnecessary: "
            "the system asks about important changes by itself. Set it when a change is risky in a way the rule cannot see.")

TRANSLATIONS: dict[str, tuple[str, dict[str, str]]] = {
    "execute_command": (
        "Runs a shell command on the server in the working directory. Reads and routine changes (restart, reload, "
        "config edits, packages) run immediately through the safety fuse, the system asks the user about important "
        "ones, forbidden ones are rejected. Limit output (tail -n, head, grep). Docker too: docker ps -a, docker logs "
        "--tail 200 <container>, docker inspect, docker images (restart and stop -- through the safety fuse, rm -- with "
        "confirmation). Prefer the specialised tools for known areas.",
        {"command": "Shell command to run on the server.", "requires_confirmation": _CONFIRM,
         "host": "Docker mode only: true runs the command on the host (like root in the server shell, through a helper "
                 "container, with the host's tools), e.g. apt-get, systemctl, sed -i in /etc, ufw, docker compose build/pull/up -d "
                 "in the project directory. Routine right away, important changes after confirmation. Do not use it for "
                 "reads — read from the host directory."}),
    "read_file": (
        "Reads a file on the server. Host path (e.g. /etc/nginx/nginx.conf) or relative to the working directory. "
        "Secrets in the content are hidden from you ([ZREDAGOWANO]).",
        {"path": "Path to the file on the server."}),
    "write_file": (
        "Writes a whole file on the server (creates it together with missing directories, or overwrites). Outside the "
        "workshop the write goes through the safety fuse (backup, validation); access, autostart, code and secret "
        "files -- after confirmation. In the workshop directory (projects, your scripts) it writes right away. "
        "Do not use it for files you read with [ZREDAGOWANO] — change them surgically with execute_command (sed -i).",
        {"path": "Path to the file on the server.", "content": "New, complete file content."}),
    "change_directory": (
        "Changes the working directory on the server (e.g. '/srv/app'). Use it when the user wants to work in another "
        "directory — subsequent commands run there.",
        {"path": "Host path or relative, e.g. '/home/user/project'."}),
    "git_command": (
        "A Git operation in a repository: status, log, diff, branch, show (reads) or pull, commit, push, checkout, "
        "merge (through the safety fuse; push, reset --hard and clean after confirmation).",
        {"repo_path": "Host path to the repository, e.g. /srv/shop.",
         "subcommand": "Subcommand without 'git', e.g. 'status', 'log --oneline -20', 'pull'.",
         "requires_confirmation": _CONFIRM}),
    "system_stats": (
        "Host state straight from the host's /proc: uptime, load average vs number of cores, RAM, swap, usage of all "
        "disks. stat_type=processes adds the heaviest processes.",
        {"stat_type": "Scope of data (summary by default)."}),
    "network_info": (
        "Host network diagnostics: listening ports (public ones marked), connections, ping, curl, DNS.",
        {"check_type": "ports/listeners -- host ports, connections -- TCP connections, ping/curl/dns -- test a target.",
         "target": "For ping/curl/dns, e.g. 'example.com', 'http://localhost:8080/health'."}),
    "cron_manage": (
        "Host cron jobs: list (crontabs and /etc/cron.d), check-logs, add/remove (native mode only, with confirmation). "
        "Tasks the agent should run itself belong in routines (routine_manage).",
        {"schedule": "For add: cron schedule, e.g. '0 3 * * *'.",
         "command": "For add: the command; for remove: the exact crontab line to remove."}),
    "diagram": (
        "Draws a diagram and sends it to the user as an image (Telegram: photo, CLI: PNG file + terminal preview). "
        "mode=infra: automatic infrastructure map of the host (containers, compose projects, reverse proxy and domains, "
        "ports, services, Kubernetes cluster, remote targets) — use it when the user asks about the architecture or "
        "'what is running here'. mode=mermaid: your own diagram in Mermaid syntax (flowchart, sequenceDiagram, "
        "erDiagram, stateDiagram...) — for flows, procedures and dependencies.",
        {"mermaid": "For mode=mermaid: Mermaid diagram code (without ``` fences). Labels with special characters "
                    "in quotes: A[\"nginx :443\"].",
         "title": "Diagram title (short).",
         "include_kubernetes": "For mode=infra: include applications from the cluster (kubectl)."}),
    "server_history": (
        "The server's memory over time (read-only, no LLM). changes: what changed on the host in the given period — "
        "packages, containers and images, ports, systemd services, cron, accounts, SSH keys, configuration (nginx, "
        "sshd, sudoers, compose), reboots — with the time window of each change. USE IT FIRST when something broke "
        "('stopped working', 'since yesterday'). chart: load/RAM/disk chart from monitoring history, sent to the user "
        "as an image. checks: TLS certificate validity and responses of domains from the proxy configuration, DNS "
        "records, freshness of backups from DIRECTORY. incidents: incident memory — what happened before, what was "
        "found and what helped (use it when the user asks 'did this happen before').",
        {"since": "Period back: '24h' (default), '3h', '7d'. For changes and chart.", "metric": "For chart."}),
    "security_audit": (
        "Host security audit with a 0-100 score: SSH password/root login, firewall, databases and the Docker API "
        "exposed publicly, privileged containers and ones with docker.sock, accounts with uid 0 or no password, "
        "automatic updates, fail2ban, world-readable .env files, certificates, vulnerabilities (CVE) in the server's "
        "packages, container images and application dependencies — each with a description of what it is about. Every "
        "finding has a fix with the exact command. Read-only. Use it when the user asks about server security or its "
        "vulnerabilities.",
        {}),
    "security_patch": (
        "Patches known vulnerabilities that have a fix, exactly like the /patch command: this server's packages "
        "(apt/apk with cleanup: old kernels, downloaded packages), container images from compose (a newer image and, "
        "where there is none, the fixes installed inside the image; the previous image stays for /undo), application "
        "dependencies (a package manager in an isolated container bumps the versions in the dependency files, backed up "
        "in the journal, then the compose project is rebuilt) and remote servers (ssh targets), then removes old images "
        "and caches and rescans. The user approves the whole patch ONCE, not every command — do not assemble it from "
        "execute_command or write_file.",
        {"scope": "'' or 'all' — everything; 'images' — container images only; 'system' — this server's packages only; "
                  "'apps' — application dependencies only; "
                  "'servers' — remote servers only; a remote server name — only that one; 'urgent' — this server's "
                  "urgent packages."}),
    "journal": (
        "Log of confirmed changes: before every change Pipe backs up files, git state and crontab and records "
        "inverse commands (docker start/stop, systemctl enable/disable). list: recent entries. undo: undo an entry "
        "(the latest by default). Use it when the user says 'undo', 'put it back', or when a "
        "change turned out to be wrong.",
        {"id": "For undo: entry id (e.g. 3f2a9c1d); empty = latest."}),
    "server_md": (
        "Your lasting memory about this server: the SERVER.md file, included in every conversation. Record lasting "
        "facts: system, services, containers, domains, ports, user decisions. NEVER secrets. Operations: read; "
        "update_section (replace or add one '## ...' section — preferred); write (whole file).",
        {"section": "Section title without '##' (for update_section).",
         "content": "Markdown: section content or the whole file."}),
    "directory": (
        "DIRECTORY: a map of important places on the server — repositories, application directories, compose projects, "
        "configuration, data, logs, backups — with a one-sentence description. It is in the system prompt. upsert "
        "adds/updates an entry, scan discovers git repositories and compose projects automatically.",
        {"path": "Host path, e.g. /srv/shop.", "description": "One sentence: what it is and what it is for.",
         "remote": "For repo: remote address (without tokens).", "branch": "For repo: main branch."}),
    "skill_manage": (
        "Skills are reusable procedures you saved (e.g. deploying an application, renewing a certificate). The list is "
        "in the system prompt — before doing a matching task, read the skill (read). Save a skill (save) after a "
        "multi-step procedure that will be repeated. install: skills (and, with plugin=true, MCP servers) from a git "
        "repository — owner/repo on GitHub, an https address or git@ — a preview and one confirmation; the user can "
        "also type /download. A skill does not bypass the safety rules.",
        {"source": "For install: the repository (e.g. 'anthropics/skills' or a URL).",
         "plugin": "For install: true — the plugin's MCP servers too (.mcp.json).",
         "name": "Name: lowercase letters, digits and dashes, e.g. 'renew-certificate'. For install: optional filter.",
         "description": "For save: one sentence — when to use this skill.",
         "content": "For save: Markdown with steps, commands and verification."}),
    "vibe": (
        "VIBE: a note on how to talk to the current user (tone, length, form, technical level). It also updates itself "
        "in the background. Use update when the user says explicitly how they want you to talk — give the whole new "
        "note (Markdown, starting with '# VIBE', at most 12 points).",
        {"content": "For update: the complete new note."}),
    "target_manage": (
        "Remote targets: SSH servers, Docker containers, Kubernetes clusters and pods where you can run commands "
        "(remote_exec) and send workers (delegate). Nothing is installed on the other side. add requires confirmation; "
        "test checks connectivity. The 'local' target (this host) always exists.",
        {"name": "Target name: lowercase letters, digits, '-', '_' (e.g. 'web-2').",
         "description": "What machine/cluster it is.", "host": "ssh: host address.", "user": "ssh: user.",
         "port": "ssh: port (22 by default).", "identity_file": "ssh: key path inside the Pipe container (optional).",
         "container": "docker: container name.", "context": "kubernetes: kubeconfig context (optional).",
         "namespace": "kubernetes: namespace (optional).",
         "pod": "kubernetes: pod for kubectl exec (empty = whole cluster).",
         "pod_container": "kubernetes: container in the pod (optional)."}),
    "remote_exec": (
        "Runs one command on a remote target. Safety classification as in execute_command (for the target command). "
        "On a Kubernetes cluster target the command starts with kubectl or helm.",
        {"target": "Target name (target_manage list) or 'local'.", "command": "Command to run on the target."}),
    "delegate": (
        "Sends workers — sub-agents that investigate targets in parallel and return reports to you. Each worker has one "
        "target and one task, only reads, and returns state-changing commands as proposals. Use it for several targets "
        "at once or a longer diagnosis (logs, cause of an outage). Giving a worker name used earlier in this "
        "conversation continues its thread.",
        {"tasks": "List of tasks (a few at most).", "tasks.items.target": "Target name or 'local'.",
         "tasks.items.task": "A concrete task: what to check, what to look for, what the report should contain.",
         "tasks.items.name": "Optional worker name."}),
    "routine_manage": (
        "Routines: tasks you run on your own on a schedule (cron) and report to the user — e.g. 'every day at 7 check "
        "backups and certificate validity'. A worker runs them (read-only). add requires confirmation; run starts a "
        "routine right away.",
        {"name": "Routine name, e.g. 'morning-review'.",
         "schedule": "Cron (5 fields, server time), e.g. '0 7 * * *', or @hourly/@daily/@weekly.",
         "task": "What to check and what the report should contain.", "target": "Target (local by default).",
         "notify": "Report always or only on a problem."}),
    "reminder": (
        "Reminders: a ONE-OFF message or task at a given time — 'write to me in 10 seconds', 'remind me tomorrow at 9 "
        "to renew the domain', 'in an hour check whether the backup finished'. This is the only way to reach the user "
        "later: you cannot wait by yourself. kind=message sends the text (no confirmation); kind=task starts a worker "
        "(read-only) at that time and sends a report. Set recurring tasks up as routines "
        "(routine_manage).",
        {"delay": "For add: how long from now, e.g. '10s', '15m', '2h', '1d', '1h30m'. Use it when the user gives a "
                  "relative time ('in 10 seconds').",
         "at": "For add instead of delay: server time 'HH:MM' (today or tomorrow) or 'YYYY-MM-DD HH:MM'. "
               "operation=list returns the current server time.",
         "text": "For add: the reminder text written the way the user should read it; for kind=task: what to check "
                 "and what the report should contain.",
         "kind": "message by default.", "target": "For kind=task: the target (local by default).",
         "id": "For cancel: the reminder id from list."}),
    "web_search": (
        "Searches the internet (no API key) and returns titles, addresses and page snippets. Use it ON YOUR OWN when "
        "the answer depends on knowledge from outside the server that you may lack or that may have changed: an "
        "unknown error message, changes in a new version, CVEs, documentation, the current version or end of "
        "support. Do not search for what you can check on the server. The query leaves the server: write it in "
        "general terms (error text, software name and version), never with passwords, tokens, domains, IP addresses "
        "or user names.",
        {"query": "A short query, preferably in English, e.g. 'nginx upstream prematurely closed connection 502'.",
         "max_results": "How many results (1-10, 6 by default)."}),
    "web_fetch": (
        "Reads a page from the internet. With a question a separate model reads the page and returns only the answer "
        "— read documentation, release notes and bug threads that way. Without a question the start of the page text "
        "comes back. Addresses from web_search results and given by the user are read right away, others require "
        "confirmation. Public internet only — check local services with network_info.",
        {"url": "Page address (http/https), preferably from web_search results.",
         "question": "What you are looking for on the page, e.g. 'which option disables X in version 2.4?'."}),
    "software_info": (
        "Software knowledge from public databases (no key). eol — endoflife.date: whether a product version (OS, "
        "database, language, web server) is still supported, until when and what the latest one is. vulns — OSV.dev: "
        "known vulnerabilities (CVEs) of a package in the installed version, with the version that fixes them. Check the "
        "versions on the server first (/etc/os-release, dpkg-query, apk, pip). More precise than web_search for these "
        "questions. A question about the whole server's vulnerabilities (packages, container images, applications) is "
        "security_audit — vulns is for a single package.",
        {"product": "For eol: the endoflife.date name, e.g. 'ubuntu', 'debian', 'postgresql', 'nginx', 'nodejs', "
                    "'python', 'php'.",
         "version": "Version: for eol a cycle or full version ('22.04', '16.4'); for vulns the exact package version "
                    "(e.g. '1.22.1-9+deb12u2').",
         "ecosystem": "For vulns: the ecosystem from this system's /etc/os-release (e.g. Debian 13 -> 'Debian:13', Ubuntu 22.04 -> "
                      "'Ubuntu:22.04:LTS', 'Alpine:v3.20'), 'PyPI', 'npm', 'Go', 'Maven', "
                      "'crates.io', 'RubyGems'.",
         "package": "For vulns: the package name; on Debian and Ubuntu the SOURCE package "
                    "(dpkg-query -W -f='${source:Package} ${Version}' name)."}),
    "project": (
        "Workshop: projects in the workshop directory (path in the ENVIRONMENT section) — repositories, small apps and "
        "your own scripts. Use it when the user asks: 'clone and run', 'set up this app', 'what does this repo do', "
        "'write a script that...'. In the workshop you act on your own, without asking: list, create, clone, inspect, "
        "exec (a command in a one-off, isolated container that sees only the project directory as /work: installing "
        "dependencies, tests, builds, your scripts), logs, stop, start, pull — and you write the project files with "
        "write_file. run (a long-running service: container pipe-<name>, port on 127.0.0.1 only) and remove ask once. "
        "Order: clone -> inspect (what it is and how to run it) -> run -> logs. When inspect does not know how to run "
        "it, write a Dockerfile in the project directory and run with strategy=dockerfile.",
        {"name": "Project name: lowercase letters, digits, hyphens (for clone taken from the address by default).",
         "url": "For clone: https://github.com/org/repo or git@host:org/repo.git (no tokens).",
         "command": "For exec: a shell command in the project directory, e.g. 'npm test', 'python3 report.py'.",
         "image": "For exec: an image, e.g. python:3.12-slim, node:22-bookworm-slim, golang:1.23 (by default from the "
                  "project's stack).",
         "strategy": "For run: how to run it (auto by default — from inspect).",
         "port": "For run: the port the app listens on inside the container (by default from inspect)."}),
    "load_tools": (
        "Enables tool groups whose full descriptions you do not have yet (Pipe saves tokens). memory — your memory "
        "(server_md, directory, skill_manage, vibe); security — audit, patches, change journal and undo (security_audit, "
        "security_patch, journal); schedule — reminders, routines, cron (reminder, routine_manage, cron_manage); fleet — "
        "remote servers, clusters, workers (target_manage, remote_exec, delegate); web — the internet and version "
        "knowledge (web_search, web_fetch, software_info); diagram — diagrams and the infrastructure map; update — "
        "updating Pipe itself (pipe_update); mcp — managing external MCP servers (mcp_manage); mcp:<server> — the tools "
        "of a connected MCP server (listed in the prompt). After enabling, call the tool you need in the next step.",
        {"groups": "Groups to enable: memory, security, schedule, fleet, web, diagram, update, mcp or mcp:<server>."}),
    "mcp_manage": (
        "External MCP servers you use (their tools: mcp__<server>__<tool>, group mcp:<server>). list: state; catalog: "
        "ready add-ons (Google, Outlook, Microsoft 365/Teams, GitHub...) — add them with add and just the name; add: a new "
        "server — ALWAYS with confirmation: a catalog name or npx/uvx (command + args), a Docker image (image + args) or "
        "HTTP (url + headers). A stdio server runs in an isolated container (no server files, docker.sock or privileges). Secrets "
        "only as ${MCP_NAME} from backend/.env — never put a token in the configuration; when the user gives a token in "
        "the conversation, ask them to enter the key in the /mcp window (pipe web or the CLI). remove; reload.",
        {"operation": "Operation.", "name": "Server name: lowercase letters, digits, '-', '_'.",
         "command": "npx or uvx (or docker with args 'run ... <image>').", "args": "Arguments, e.g. ['-y', '@package/server'].",
         "image": "Docker image of the MCP server (instead of command).", "env": "Server variables: text or ${MCP_NAME}.",
         "url": "HTTP: the MCP endpoint address.", "headers": "HTTP: headers, e.g. {\"Authorization\": \"Bearer ${MCP_X}\"}.",
         "askAlways": "true — confirmation for reads too.", "description": "What the server is for (one sentence)."}),
    "pipe_update": (
        "Update of Pipe itself (you): 'update yourself', 'get the new Pipe version', 'which version are you, is there a "
        "newer one'. check — version, commit and whether the remote repository has newer changes (changes nothing). "
        "apply — a separate container pulls the changes (git pull --ff-only) and rebuilds the Pipe "
        "services; the backend restarts, the conversation is cut and the result arrives by itself as a message. NEVER "
        "update Pipe itself by hand with git/docker compose (host: true included) — rebuilding its own container would cut "
        "it off halfway. Other compose projects on the host you handle normally: execute_command with host: true.",
        {}),
}


def english_tools(tools: list[dict]) -> list[dict]:
    result = copy.deepcopy(tools)
    for tool in result:
        function = tool["function"]
        description, params = TRANSLATIONS.get(function["name"], (None, {}))
        if description:
            function["description"] = description
        properties = function["parameters"].get("properties", {})
        for path, text in params.items():
            node = properties
            parts = path.split(".")
            for part in parts[:-1]:
                node = node.get(part, {}) if part != "items" else node.get("items", {}).get("properties", {})
            if parts[-1] in node:
                node[parts[-1]]["description"] = text
    return result

---
name: update-container
description: Update a docker compose service image (pull + up) with verification and a plan to go back to the previous version.
---

# Updating a docker compose service

## 1. Before (reads)
- Project and directory: DIRECTORY (a [compose] entry) or docker_manage compose-ps.
- Current image and its id — write them in your answer (this is the way back):
  `docker inspect -f '{{.Config.Image}} {{.Image}}' CONTAINER`.
- Is the tag pinned (e.g. postgres:16.4) or floating (latest) — for databases do NOT bump the major
  version without a backup and the user's consent (data migration!).
- State before: server_history operation=checks (sites respond).

## 2. Update (after YES)
`docker compose -f DIR/docker-compose.yml pull SERVICE && docker compose -f DIR/docker-compose.yml up -d SERVICE`
Safety fuse: `docker compose config -q` before; after — project containers are running and sites respond.

## 3. After
- Logs: docker_manage logs CONTAINER --tail 50 — migration errors, restart loop.
- server_history operation=changes since=1h — the new image id.

## 4. Going back to the previous version
Set the previous tag in compose (or `image: NAME@sha256:...` from step 1) and `docker compose up -d SERVICE`.
The image from before the update stays locally until you run `docker image prune`.

# Pipe in Kubernetes

Kustomize manifests. The base runs Pipe as a **cluster** agent: it manages the cluster through kubectl with its own
ServiceAccount and keeps its memory (SERVER.md, DIRECTORY, skills, VIBE, targets, routines, audit log) on a PVC.

| Directory | What it adds |
|-----------|--------------|
| `base/` | Namespace `pipe`, ServiceAccount + ClusterRole **read-only, without secrets**, PVC, Deployment (1 replica, non-root), ClusterIP Service |
| `overlays/operator/` | Changes in the cluster: restarting/scaling workloads, deleting pods, `kubectl exec`, jobs, cordon. Every command still needs a confirmation in the chat. |
| `overlays/telegram/` | The Telegram bot as a sidecar (Unix socket in a shared `emptyDir`): watch alerts on your phone |
| `overlays/host-agent/` | Pipe also manages the **node** like a VPS: the node's `/` under `/hostfs`, `/proc` under `/hostproc`, `hostPID`. Root on the node, for clusters you administer yourself (e.g. k3s on a VPS). |

## Installation

```bash
# 1. Images (amd64 + arm64): releases are in ghcr.io/kvmyk/pipe and ghcr.io/kvmyk/pipe-telegram
#    (tag = version, e.g. 0.27.0; cosign signature, see the release notes on GitHub).
#    Your own images in your registry:
#      docker buildx build --platform linux/amd64,linux/arm64 -t REGISTRY/pipe-backend:TAG --push backend/
#      docker buildx build --platform linux/amd64,linux/arm64 -t REGISTRY/pipe-telegram:TAG -f clients/telegram/Dockerfile --push .

# 2. Point at the images, in the chosen overlay or the base:
cd deploy/kubernetes/overlays/telegram     # or base/, overlays/operator/ ...
kustomize edit set image pipe-backend=ghcr.io/kvmyk/pipe:VERSION pipe-telegram=ghcr.io/kvmyk/pipe-telegram:VERSION
#   (without kustomize: change newName/newTag in the images section of kustomization.yaml)

# 3. Secrets (don't keep them in the repository)
kubectl create namespace pipe
kubectl -n pipe create secret generic pipe-env \
    --from-literal=LLM_PROVIDER=gemini --from-literal=LLM_API_KEY=... \
    --from-literal=AGENT_TOKEN="$(openssl rand -hex 24)"
#   Polish: add --from-literal=PIPE_LANG=pl (also in the pipe-telegram secret)
# only with the telegram overlay:
kubectl -n pipe create secret generic pipe-telegram \
    --from-literal=TELEGRAM_BOT_TOKEN=... --from-literal=TELEGRAM_ALLOWED_USER_IDS=123456789 \
    --from-literal=AGENT_TOKEN=<the same as above>

# 4. Deployment
kubectl apply -k deploy/kubernetes/overlays/telegram    # or base, overlays/operator, overlays/host-agent
kubectl -n pipe rollout status deploy/pipe
```

Overlays can be combined: copy `overlays/operator` and list `../telegram` in `resources` instead of `../../base`.

## Connecting from the CLI

```bash
pipe --kube pipe --token <AGENT_TOKEN>            # kubectl port-forward svc/pipe in the pipe namespace
pipe --kube pipe --kube-context prod-cluster
```

The Service is a ClusterIP. **Don't expose Pipe through an Ingress or a LoadBalancer**: that is full access to the
cluster through an LLM. Access only through `kubectl port-forward` (that is, through your own cluster permissions).

## What the agent sees

- Mode `PIPE_RUNTIME=kubernetes`: the prompt tells the agent that the pod's file system is not the node and that it
  manages the cluster through kubectl. `/map` draws ingresses -> services -> deployments.
- Base permissions: `get/list/watch` on pods (with logs), services, nodes, events, configmaps, PV/PVC, workloads
  (`apps`, `batch`), ingresses, HPA, CRDs and metrics. **No `secrets`**, and `kubectl get secret -o yaml` needs a
  confirmation in the classifier anyway.
- Other clusters (e.g. production from your laptop) are added as `kubernetes` targets with a kubeconfig context; then
  mount the kubeconfig in the pod (Secret -> `/tmp/.kube/config`, `HOME=/tmp`).

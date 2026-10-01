# Pipe w Kubernetesie

Manifesty kustomize. Baza uruchamia Pipe jako agenta **klastra**: zarzadza nim przez kubectl
z wlasnym ServiceAccount, a pamiec (SERVER.md, DIRECTORY, skille, VIBE, cele, rutyny, audit log)
trzyma na PVC.

| Katalog | Co dodaje |
|---------|-----------|
| `base/` | Namespace `pipe`, ServiceAccount + ClusterRole **tylko do odczytu, bez sekretow**, PVC, Deployment (1 replika, non-root), Service ClusterIP |
| `overlays/operator/` | Zmiany w klastrze: restart/skalowanie workloadow, usuwanie podow, `kubectl exec`, joby, cordon. Kazda komenda nadal wymaga potwierdzenia w rozmowie. |
| `overlays/telegram/` | Bot Telegram jako sidecar (Unix socket we wspolnym `emptyDir`) -- alerty czuwania na telefon |
| `overlays/host-agent/` | Pipe zarzadza tez **wezlem** jak VPS-em: `/` wezla pod `/hostfs`, `/proc` pod `/hostproc`, `hostPID`. Uprawnienia roota na wezle -- dla klastrow, ktore administrujesz sam (np. k3s na VPS). |

## Instalacja

```bash
# 1. Obrazy (amd64 + arm64) do Twojego rejestru
docker buildx build --platform linux/amd64,linux/arm64 -t REJESTR/pipe-backend:TAG --push backend/
# (tylko z nakladka telegram)
docker buildx build --platform linux/amd64,linux/arm64 -t REJESTR/pipe-telegram:TAG -f clients/telegram/Dockerfile --push .

# 2. Wskaz obrazy — w wybranej nakladce albo bazie:
cd deploy/kubernetes/overlays/telegram     # albo base/, overlays/operator/ ...
kustomize edit set image pipe-backend=REJESTR/pipe-backend:TAG pipe-telegram=REJESTR/pipe-telegram:TAG
#   (bez kustomize: zmien newName/newTag w sekcji images pliku kustomization.yaml)

# 3. Sekrety (nie trzymaj ich w repozytorium)
kubectl create namespace pipe
kubectl -n pipe create secret generic pipe-env \
    --from-literal=LLM_PROVIDER=gemini --from-literal=LLM_API_KEY=... \
    --from-literal=AGENT_TOKEN="$(openssl rand -hex 24)"
#   jezyk angielski: dodaj --from-literal=PIPE_LANG=en (takze w sekrecie pipe-telegram)
# tylko z nakladka telegram:
kubectl -n pipe create secret generic pipe-telegram \
    --from-literal=TELEGRAM_BOT_TOKEN=... --from-literal=TELEGRAM_ALLOWED_USER_IDS=123456789 \
    --from-literal=AGENT_TOKEN=<ten sam co wyzej>

# 4. Wdrozenie
kubectl apply -k deploy/kubernetes/overlays/telegram    # albo base, overlays/operator, overlays/host-agent
kubectl -n pipe rollout status deploy/pipe
```

Nakladki mozna laczyc: skopiuj `overlays/operator` i dopisz w `resources` tez `../telegram`
zamiast `../../base`.

## Polaczenie z CLI

```bash
pipe --kube pipe --token <AGENT_TOKEN>            # kubectl port-forward svc/pipe w namespace pipe
pipe --kube pipe --kube-context prod-cluster
```

Service jest typu ClusterIP. **Nie wystawiaj Pipe przez Ingress ani LoadBalancer** -- to pelny dostep
do klastra przez LLM. Dostep tylko przez `kubectl port-forward` (czyli przez Twoje uprawnienia do klastra).

## Co agent widzi

- Tryb `PIPE_RUNTIME=kubernetes`: prompt mowi agentowi, ze system plikow poda to nie wezel, a klastrem
  zarzadza przez kubectl. `/mapa` rysuje ingressy -> serwisy -> deploymenty.
- Uprawnienia bazowe: `get/list/watch` na podach (z logami), serwisach, wezlach, eventach, configmapach,
  PV/PVC, workloadach (`apps`, `batch`), ingressach, HPA, CRD i metrykach. **Bez `secrets`**, a
  `kubectl get secret -o yaml` i tak wymaga potwierdzenia w klasyfikatorze.
- Inne klastry (np. produkcja z laptopa) dodasz jako cele `kubernetes` z kontekstem kubeconfig -- wtedy
  zamontuj kubeconfig w podzie (Secret -> `/tmp/.kube/config`, `HOME=/tmp`).

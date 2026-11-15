{{/* Shared metadata and sync policy for every child Application. */}}
{{- define "root.meta" -}}
namespace: argocd
annotations:
  argocd.argoproj.io/sync-wave: {{ .wave | quote }}
finalizers:
  # Deleting the Application deletes what it deployed (used by the Destroy workflow).
  - resources-finalizer.argocd.argoproj.io
{{- end }}

{{- define "root.syncPolicy" -}}
syncPolicy:
  automated:
    prune: true
    selfHeal: true # undo manual kubectl changes: git wins
  syncOptions:
    - CreateNamespace=true
    - ServerSideApply=true
    - RespectIgnoreDifferences=true # keep ignoreDifferences fields out of syncs too
  retry:
    limit: 10
    backoff:
      duration: 15s
      factor: 2
      maxDuration: 5m
{{- end }}

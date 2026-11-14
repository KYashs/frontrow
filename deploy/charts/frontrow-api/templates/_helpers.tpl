{{- define "api.labels" -}}
app.kubernetes.io/name: frontrow-api
app.kubernetes.io/part-of: frontrow
{{- end }}

{{- define "api.selector" -}}
app.kubernetes.io/name: frontrow-api
{{- end }}

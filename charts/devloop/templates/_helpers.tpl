{{/*
Required value validator: fails if `temporalHost` is empty.
*/}}
{{- define "devloop.validate.temporalHost" -}}
{{- if not (eq .Values.temporalHost "") -}}
  {{/* valid */}}
{{- else -}}
  {{- fail "temporalHost is required but was not set. Provide a value like 'temporal-frontend.agents.svc:7233'" -}}
{{- end -}}
{{- end -}}

{{/*
Common labels
*/}}
{{- define "devloop.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: devloop
{{- end }}

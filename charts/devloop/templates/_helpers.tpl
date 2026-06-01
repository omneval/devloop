{{/*
Required value validator: fails if `temporalHost` is empty.
*/}}
{{- define "devloop.validate.temporalHost" -}}
{{- if eq .Values.temporalHost "" -}}
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

{{/*
Common health probes (used by all components).
*/}}
{{- define "devloop.healthProbes" -}}
livenessProbe:
  httpGet:
    path: /healthz
    port: health
  initialDelaySeconds: 10
  periodSeconds: 30
readinessProbe:
  httpGet:
    path: /healthz
    port: health
  initialDelaySeconds: 5
  periodSeconds: 10
{{- end }}

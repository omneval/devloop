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
Effective ServiceAccount name for each component. When serviceAccount.name is
set by the operator the explicit value wins; otherwise the name is derived from
the release name so that multiple installs in the same namespace don't collide.
*/}}
{{- define "devloop.temporalWorker.serviceAccountName" -}}
{{- if .Values.temporalWorker.serviceAccount.name -}}
{{- .Values.temporalWorker.serviceAccount.name -}}
{{- else -}}
{{- printf "%s-temporal-worker" .Release.Name -}}
{{- end -}}
{{- end }}

{{- define "devloop.agentJob.serviceAccountName" -}}
{{- if .Values.temporalWorker.agentJob.serviceAccount.name -}}
{{- .Values.temporalWorker.agentJob.serviceAccount.name -}}
{{- else -}}
{{- printf "%s-agent-job" .Release.Name -}}
{{- end -}}
{{- end }}

{{/*
Port of an endpoint URL: "http://host:8000/v1" -> 8000, falling back to the
scheme default (80 for http, 443 otherwise) when the URL carries no explicit
port. Used to derive the agent-job egress NetworkPolicy allowlist from the
configured LLM/OTLP endpoints (issue #123).
*/}}
{{- define "devloop.urlPort" -}}
{{- $u := urlParse . -}}
{{- if contains ":" $u.host -}}
{{- last (splitList ":" $u.host) -}}
{{- else if eq $u.scheme "http" -}}
80
{{- else -}}
443
{{- end -}}
{{- end -}}

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

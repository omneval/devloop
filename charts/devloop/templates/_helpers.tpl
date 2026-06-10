{{/*
Required value validator: fails if `temporalHost` is empty and the bundled
Temporal subchart is not enabled (issue #117).
*/}}
{{- define "devloop.validate.temporalHost" -}}
{{- if and (eq .Values.temporalHost "") (not .Values.temporal.enabled) -}}
  {{- fail "temporalHost is required but was not set. Provide a value like 'temporal-frontend.agents.svc:7233', or set temporal.enabled=true to deploy the bundled Temporal subchart" -}}
{{- end -}}
{{- end -}}

{{/*
Effective Temporal frontend address. An explicit `temporalHost` always wins;
otherwise, when the bundled Temporal subchart is enabled, default to its
frontend Service. The subchart names that Service `<fullname>-frontend`, where
<fullname> follows the standard Helm convention: `temporal.fullnameOverride`
if set, the bare release name if it already contains "temporal", else
`<release>-temporal`.
*/}}
{{- define "devloop.temporalHost" -}}
{{- if .Values.temporalHost -}}
{{- .Values.temporalHost -}}
{{- else if .Values.temporal.enabled -}}
{{- $fullname := printf "%s-temporal" .Release.Name -}}
{{- if .Values.temporal.fullnameOverride -}}
{{- $fullname = .Values.temporal.fullnameOverride -}}
{{- else if contains "temporal" .Release.Name -}}
{{- $fullname = .Release.Name -}}
{{- end -}}
{{- printf "%s-frontend.%s.svc.cluster.local:7233" $fullname .Release.Namespace -}}
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

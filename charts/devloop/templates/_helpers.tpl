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
Required fields for each .Values.projects[] entry. One field name per line —
must stay in sync with _REQUIRED_FIELDS in src/devloop/projects.py (enforced
by tests/test_projects_chart_sync.py).
*/}}
{{- define "devloop.projects.requiredFields" -}}
id
github_url
default_branch
agent_label
github_token_secret
{{- end -}}

{{/*
Required value validator: fails if the now-removed
temporalWorker.projectsConfigMap.name is still set, pointing operators at the
top-level `projects:` block that replaced it.
*/}}
{{- define "devloop.validate.projectsConfigMap" -}}
{{- if (((.Values.temporalWorker).projectsConfigMap).name) -}}
  {{- fail "temporalWorker.projectsConfigMap is no longer supported. Move your project entries into the top-level `projects:` list in values.yaml — the chart now renders and mounts the registry ConfigMap itself." -}}
{{- end -}}
{{- end -}}

{{/*
Effective Temporal frontend address. An explicit `temporalHost` always wins;
otherwise, when the bundled Temporal subchart is enabled, default to its
frontend Service. The subchart names that Service `<fullname>-frontend`, where
<fullname> is `temporal.fullnameOverride` (defaulted to "temporal" in
values.yaml).
*/}}
{{- define "devloop.temporalHost" -}}
{{- if .Values.temporalHost -}}
{{- .Values.temporalHost -}}
{{- else if .Values.temporal.enabled -}}
{{- printf "%s-frontend.%s.svc.cluster.local:7233" (.Values.temporal.fullnameOverride | default "temporal") .Release.Namespace -}}
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

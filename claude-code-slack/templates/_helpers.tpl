{{- define "claude-code-slack.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "claude-code-slack.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "claude-code-slack.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "claude-code-slack.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "claude-code-slack.selectorLabels" -}}
app.kubernetes.io/name: {{ include "claude-code-slack.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "claude-code-slack.secretName" -}}
{{- if .Values.slack.existingSecret -}}
{{- .Values.slack.existingSecret -}}
{{- else -}}
{{- printf "%s-secret" (include "claude-code-slack.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "claude-code-slack.image" -}}
{{- if .Values.build.enabled -}}
{{- printf "%s:%s" (include "claude-code-slack.fullname" .) .Values.build.imageTag -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository .Values.image.tag -}}
{{- end -}}
{{- end -}}

{{- define "claude-code-slack.mem0SecretName" -}}
{{- if .Values.mem0.existingSecret -}}
{{- .Values.mem0.existingSecret -}}
{{- else -}}
{{- printf "%s-mem0" (include "claude-code-slack.fullname" .) -}}
{{- end -}}
{{- end -}}

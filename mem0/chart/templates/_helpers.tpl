{{- define "mem0.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mem0.fullname" -}}
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

{{- define "mem0.labels" -}}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "mem0.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "mem0.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mem0.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Component-specific helpers */}}

{{- define "mem0.postgres.fullname" -}}
{{- printf "%s-postgres" (include "mem0.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mem0.postgres.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mem0.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: postgres
{{- end -}}

{{- define "mem0.server.fullname" -}}
{{- printf "%s-server" (include "mem0.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mem0.server.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mem0.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: server
{{- end -}}

{{- define "mem0.dashboard.fullname" -}}
{{- printf "%s-dashboard" (include "mem0.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mem0.dashboard.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mem0.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: dashboard
{{- end -}}

{{- define "mem0.serviceAccountName" -}}
{{- if .Values.serviceAccount.name -}}
{{- .Values.serviceAccount.name -}}
{{- else -}}
{{- printf "%s-sa" (include "mem0.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "mem0.secretName" -}}
{{- if .Values.secret.existingSecret -}}
{{- .Values.secret.existingSecret -}}
{{- else -}}
{{- printf "%s-config" (include "mem0.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "mem0.server.image" -}}
{{- if .Values.server.build.enabled -}}
{{- printf "%s:%s" (include "mem0.server.fullname" .) .Values.server.build.imageTag -}}
{{- else -}}
{{- printf "%s:%s" .Values.server.image.repository .Values.server.image.tag -}}
{{- end -}}
{{- end -}}

{{- define "mem0.dashboard.image" -}}
{{- if .Values.dashboard.build.enabled -}}
{{- printf "%s:%s" (include "mem0.dashboard.fullname" .) .Values.dashboard.build.imageTag -}}
{{- else -}}
{{- printf "%s:%s" .Values.dashboard.image.repository .Values.dashboard.image.tag -}}
{{- end -}}
{{- end -}}

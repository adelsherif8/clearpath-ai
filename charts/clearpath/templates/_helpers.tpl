{{- define "clearpath.name" -}}{{ .Chart.Name }}{{- end -}}
{{- define "clearpath.labels" -}}
app.kubernetes.io/name: {{ include "clearpath.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end -}}
{{- define "clearpath.selector" -}}
app.kubernetes.io/name: {{ include "clearpath.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

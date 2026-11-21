{{- /*
root.cluster: this cluster's entry of `clusters` with its defaults, as JSON (fromJson it). Fails when `cluster` is
missing or names no entry: a root Application without a valid cluster must not render a platform for no one.
*/}}
{{- define "root.cluster" -}}
{{- $name := .Values.cluster | default "" | toString }}
{{- if not $name }}{{ fail "cluster: not set (the root Application passes it as a Helm parameter: cluster=<name of an entry in clusters>)" }}{{ end }}
{{- if not (hasKey .Values.clusters $name) }}{{ fail (printf "cluster %q: not in clusters (%s)" $name (keys .Values.clusters | sortAlpha | join ", ")) }}{{ end }}
{{- merge (deepCopy (index .Values.clusters $name)) (dict "name" $name "runtime" "generic" "objectStorage" "none") | toJson }}
{{- end }}

{{- /* root.tzOffsetHours: the standard offset of `timezone` in hours (files/timezones.yaml; summer time ignored) */}}
{{- define "root.tzOffsetHours" -}}
{{- $tz := .Files.Get "files/timezones.yaml" | fromYaml }}
{{- if not (hasKey $tz .Values.timezone) }}{{ fail (printf "timezone %q: not an IANA time zone name (platform/root/files/timezones.yaml)" .Values.timezone) }}{{ end }}
{{- index $tz .Values.timezone }}
{{- end }}

{{- /* root.product: an Application source in the product repository at product.version */}}
{{- define "root.product" -}}
repoURL: {{ .Values.product.repo }}
targetRevision: {{ .Values.product.version | quote }}
{{- end }}

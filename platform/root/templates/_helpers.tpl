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

{{- /* root.component: components.<name> of platform.yaml with its defaults, as JSON: {enabled, values};
clusters.<cluster>.components.<name> deep-merged over it (clusters differ: a small lab drops what prod keeps) */}}
{{- define "root.component" -}}
{{- $root := index . 0 }}{{ $name := index . 1 }}
{{- $top := index ($root.Values.components | default dict) $name | default dict }}
{{- $cl := index ((index ($root.Values.clusters | default dict) ($root.Values.cluster | default "") | default dict).components | default dict) $name | default dict }}
{{- $c := include "root.deepMerge" (dict "base" $top "over" $cl) | fromJson }}
{{- dict "enabled" (ne (toString (dig "enabled" true $c)) "false") "values" (dig "values" dict $c) | toJson }}
{{- end }}

{{- /* root.tracing: components.tracing with clusters.<cluster>.components.tracing deep-merged over it, as JSON
{enabled, external}: opt-in (enabled defaults to false); enabled needs external.queryUrl, an external Tempo (this
release installs no Tempo of its own). Only platform-monitoring's Grafana uses it (a Tempo datasource). */}}
{{- define "root.tracing" -}}
{{- $top := index (.Values.components | default dict) "tracing" | default dict }}
{{- $cl := index ((index (.Values.clusters | default dict) (.Values.cluster | default "") | default dict).components | default dict) "tracing" | default dict }}
{{- $c := include "root.deepMerge" (dict "base" $top "over" $cl) | fromJson }}
{{- $on := eq (toString (dig "enabled" false $c)) "true" }}
{{- $ext := dig "external" dict $c }}
{{- if and $on (not (dig "queryUrl" "" $ext)) }}{{ fail "components.tracing: enabled needs external.queryUrl (an external Tempo's HTTP API, e.g. http://tempo.example:3200): this release installs no Tempo of its own" }}{{ end }}
{{- dict "enabled" $on "external" $ext | toJson }}
{{- end }}

{{- /* root.enabled: "true" unless components.<name>.enabled is false ((list . "<name>"); empty = false) */}}
{{- define "root.enabled" -}}
{{- if (include "root.component" . | fromJson).enabled }}true{{ end }}
{{- end }}

{{- /*
root.helmValues: a Helm-sourced component's valuesObject ((list . "<name>" "<its values as YAML>")): as written when
platform.yaml has no components.<name>.values, else with them deep-merged over it (root.deepMerge).
*/}}
{{- define "root.helmValues" -}}
{{- $over := (include "root.component" (list (index . 0) (index . 1)) | fromJson).values }}
{{- if $over }}
{{- include "root.deepMerge" (dict "base" (index . 2 | fromYaml) "over" $over) | fromJson | toYaml }}
{{- else }}
{{- index . 2 | trim }}
{{- end }}
{{- end }}

{{- /* root.deepMerge: (dict "base" … "over" …) → JSON: maps merge key by key, anything else (lists, scalars, null) in
"over" replaces what "base" has */}}
{{- define "root.deepMerge" -}}
{{- $out := deepCopy .base }}
{{- range $k, $v := .over }}
{{- if and (kindIs "map" $v) (kindIs "map" (index $out $k)) }}
{{- $_ := set $out $k (include "root.deepMerge" (dict "base" (index $out $k) "over" $v) | fromJson) }}
{{- else }}
{{- $_ := set $out $k $v }}
{{- end }}
{{- end }}
{{- toJson $out }}
{{- end }}

{{- /* root.appsGlob: where the ApplicationSet finds the applications: <instance.apps>/<app>/app.yaml as a glob */}}
{{- define "root.appsGlob" -}}
{{- $apps := .Values.instance.apps | default "apps" | toString }}
{{- if eq $apps "." }}*/app.yaml{{ else }}{{ $apps }}/*/app.yaml{{ end }}
{{- end }}

{{- /* root.appsFiles: the git generator's files (flow sequence): root.appsGlob, then for each application of this
cluster's clusters.<name>.applications.exclude (directory names) `{ path: "<apps>/<app>/app.yaml", exclude: true }`;
without the key exactly [{ path: "<glob>" }], as before it existed */}}
{{- define "root.appsFiles" -}}
{{- $c := include "root.cluster" . | fromJson }}
{{- $apps := .Values.instance.apps | default "apps" | toString -}}
[{ path: {{ include "root.appsGlob" . | quote }} }
{{- range ((($c.applications | default dict).exclude) | default list) }}
{{- $f := ternary (printf "%s/app.yaml" .) (printf "%s/%s/app.yaml" $apps .) (eq $apps ".") }}, { path: {{ $f | quote }}, exclude: true }
{{- end }}]
{{- end }}

{{- /* root.environmentsIf: the Argo CD template that opens an if around each environment of an application (the
ApplicationSet's environments list; the application's name is $.name, the directory name), false for those this
cluster leaves out: clusters.<name>.environments.exclude (every application) and
clusters.<name>.applications.environments.<app>.exclude (that one); empty without them, as before they existed */}}
{{- define "root.environmentsIf" -}}
{{- $c := include "root.cluster" . | fromJson }}
{{- $every := (($c.environments | default dict).exclude) | default list }}
{{- $per := dict }}
{{- range $app, $e := (($c.applications | default dict).environments) | default dict }}
{{- $x := ($e | default dict).exclude | default list }}
{{- if $x }}{{ $_ := set $per $app $x }}{{ end }}
{{- end }}
{{- $terms := list }}
{{- if $every }}{{ $terms = append $terms (printf "(has .name %s)" (include "root.goList" $every)) }}{{ end }}
{{- if $per }}
{{- $d := list }}
{{- range $app := keys $per | sortAlpha }}
{{- $d = append $d (printf "%q %s" $app (include "root.goList" (index $per $app))) }}
{{- end }}
{{- $terms = append $terms (printf "(has .name (get (dict %s) $.name | default list))" (join " " $d)) }}
{{- end }}
{{- if $terms }}{{ printf "{{- if not (or %s) }}" (join " " $terms) }}{{ end }}
{{- end }}

{{- /* root.goList: a list of strings as a Go template expression for Argo CD's templates: (list "a" "b" …) */}}
{{- define "root.goList" -}}
(list{{ range . }} {{ printf "%q" (toString .) }}{{ end }})
{{- end }}

{{- /* root.experiments: the daily regression's experiment directories (globs), as a JSON list: regression.experiments,
default <instance.apps>/<every app>/experiments */}}
{{- define "root.experiments" -}}
{{- $x := .Values.regression.experiments }}
{{- if not $x }}
{{- $apps := .Values.instance.apps | default "apps" | toString }}
{{- $x = ternary "*/experiments" (printf "%s/*/experiments" $apps) (eq $apps ".") }}
{{- end }}
{{- if kindIs "string" $x }}{{ $x = list $x }}{{ end }}
{{- toJson $x }}
{{- end }}

{{- /* root.goDict: a map of strings as a Go template expression for Argo CD's templates: (dict "k" "v" …) */}}
{{- define "root.goDict" -}}
(dict
{{- range $k, $v := . | default dict }} {{ printf "%q" (toString $k) }} {{ printf "%q" (toString $v) }}{{ end -}}
)
{{- end }}

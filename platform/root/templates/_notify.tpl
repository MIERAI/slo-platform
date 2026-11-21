{{- /*
Notifications (platform.yaml `notify`, docs/configuration.md#notify): Alertmanager's configuration for this cluster.

root.alertmanager → JSON { config: <alertmanager.yaml>, secret: <bool>, templates: <slo-platform.tmpl or ""> }
  - lab: everything to "null" (a lab notifies nobody), no templates, no Secret.
  - prod: the default routing (docs/concepts.md); severity page → receiver "page", ticket → "ticket" (each "null" when
    its list is empty), Watchdog → "deadman" when notify.deadman is set. secret: some receiver reads a key of the Secret
    monitoring/alertmanager-notify (bootstrap creates it from the secrets file's `notify:`), which Alertmanager then
    mounts at /etc/alertmanager/secrets/alertmanager-notify/<key> (alertmanagerSpec.secrets).
Only Alertmanager's own integrations (v0.34, kube-prometheus-stack 92.17.0); ntfy through the webhook's custom payload
(ntfy's JSON publishing). Secrets only through *_file fields: platform.yaml never holds one.
*/ -}}

{{- define "root.notify.secretFile" -}}/etc/alertmanager/secrets/alertmanager-notify/{{ . }}{{- end }}

{{- /* root.notify.receiver (dict "r" <receiver> "severity" page|ticket) → JSON { kind: <x>_configs, config: {...}, secret: bool } */ -}}
{{- define "root.notify.receiver" -}}
{{- $r := .r }}
{{- $page := eq .severity "page" }}
{{- $title := `{{ template "slo.title" . }}` }}
{{- $text := `{{ template "slo.text" . }}` }}
{{- $kind := printf "%s_configs" $r.type }}
{{- $c := dict "send_resolved" true }}
{{- $secret := false }}
{{- if eq $r.type "slack" }}
  {{- $_ := set $c "api_url_file" (include "root.notify.secretFile" ($r.secretKey | default "slackWebhookUrl")) }}
  {{- $_ = set $c "title" $title }}
  {{- $_ = set $c "text" $text }}
  {{- $_ = set $c "color" (ternary `{{ if eq .Status "firing" }}danger{{ else }}good{{ end }}` `{{ if eq .Status "firing" }}warning{{ else }}good{{ end }}` $page) }}
  {{- with $r.channel }}{{ $_ = set $c "channel" . }}{{ end }}
  {{- $secret = true }}
{{- else if eq $r.type "telegram" }}
  {{- $_ := set $c "bot_token_file" (include "root.notify.secretFile" ($r.secretKey | default "telegramBotToken")) }}
  {{- $_ = set $c "chat_id" (int64 $r.chatId) }}
  {{- with $r.threadId }}{{ $_ = set $c "message_thread_id" (int64 .) }}{{ end }}
  {{- $_ = set $c "parse_mode" "HTML" }}
  {{- $_ = set $c "message" `{{ template "slo.telegram" . }}` }}
  {{- $secret = true }}
{{- else if eq $r.type "discord" }}
  {{- $_ := set $c "webhook_url_file" (include "root.notify.secretFile" ($r.secretKey | default "discordWebhookUrl")) }}
  {{- $_ = set $c "title" $title }}
  {{- $_ = set $c "message" $text }}
  {{- $secret = true }}
{{- else if eq $r.type "email" }}
  {{- $_ := set $c "to" $r.to }}
  {{- $_ = set $c "from" $r.from }}
  {{- $_ = set $c "smarthost" $r.smarthost }}
  {{- $_ = set $c "require_tls" (ternary $r.requireTls true (hasKey $r "requireTls")) }}
  {{- with $r.username }}
    {{- $_ = set $c "auth_username" . }}
    {{- $_ = set $c "auth_password_file" (include "root.notify.secretFile" ($r.secretKey | default "smtpPassword")) }}
    {{- $secret = true }}
  {{- end }}
  {{- $_ = set $c "headers" (dict "Subject" $title) }}
  {{- $_ = set $c "text" $text }}
  {{- $_ = set $c "html" `{{ template "slo.email.html" . }}` }}
{{- else if eq $r.type "webhook" }}
  {{- if $r.url }}
    {{- $_ := set $c "url" $r.url }}
  {{- else }}
    {{- $_ := set $c "url_file" (include "root.notify.secretFile" ($r.secretKey | default "webhookUrl")) }}
    {{- $secret = true }}
  {{- end }}
{{- else if eq $r.type "pushover" }}
  {{- $_ := set $c "token_file" (include "root.notify.secretFile" ($r.secretKey | default "pushoverToken")) }}
  {{- $_ = set $c "user_key_file" (include "root.notify.secretFile" ($r.userSecretKey | default "pushoverUserKey")) }}
  {{- $_ = set $c "title" $title }}
  {{- $_ = set $c "message" $text }}
  {{- $_ = set $c "priority" (ternary `{{ if eq .Status "firing" }}1{{ else }}0{{ end }}` "0" $page) }}
  {{- $secret = true }}
{{- else if eq $r.type "ntfy" }}
  {{- /* ntfy's JSON publishing (POST to the server's root URL). Alertmanager reads every rendered payload value as
  YAML: numbers stay numbers (priority); the message starts with the title "[PAGE] …", which is no valid YAML, so it
  stays a string (a body that looked like YAML would turn into an object). */}}
  {{- $kind = "webhook_configs" }}
  {{- $_ := set $c "url" (trimSuffix "/" ($r.url | default "https://ntfy.sh") | printf "%s/") }}
  {{- $_ = set $c "payload" (dict
        "topic" $r.topic
        "message" `{{ template "slo.ntfy" . }}`
        "priority" (ternary `{{ if eq .Status "firing" }}5{{ else }}3{{ end }}` `{{ if eq .Status "firing" }}3{{ else }}2{{ end }}` $page)
        "tags" (list (ternary `{{ if eq .Status "firing" }}rotating_light{{ else }}white_check_mark{{ end }}` `{{ if eq .Status "firing" }}warning{{ else }}white_check_mark{{ end }}` $page))) }}
  {{- with $r.secretKey }}
    {{- $_ = set $c "http_config" (dict "authorization" (dict "credentials_file" (include "root.notify.secretFile" .))) }}
    {{- $secret = true }}
  {{- end }}
{{- else if eq $r.type "msteams" }}
  {{- $kind = "msteamsv2_configs" }}
  {{- $_ := set $c "webhook_url_file" (include "root.notify.secretFile" ($r.secretKey | default "msteamsWebhookUrl")) }}
  {{- $_ = set $c "title" $title }}
  {{- $_ = set $c "text" $text }}
  {{- $secret = true }}
{{- else if eq $r.type "mattermost" }}
  {{- $_ := set $c "webhook_url_file" (include "root.notify.secretFile" ($r.secretKey | default "mattermostWebhookUrl")) }}
  {{- $_ = set $c "title" $title }}
  {{- $_ = set $c "text" $text }}
  {{- with $r.channel }}{{ $_ = set $c "channel" . }}{{ end }}
  {{- $secret = true }}
{{- else }}
  {{- fail (printf "notify.%s: unknown receiver type %q" .severity $r.type) }}
{{- end }}
{{- dict "kind" $kind "config" $c "secret" $secret | toJson }}
{{- end }}

{{- define "root.alertmanager" -}}
{{- $c := include "root.cluster" . | fromJson }}
{{- $n := .Values.notify | default dict }}
{{- $route := dict "receiver" "null" "group_by" (list "alertname" "namespace") "group_wait" "30s" "group_interval" "5m" "repeat_interval" "12h" }}
{{- $receivers := list (dict "name" "null") }}
{{- $secret := false }}
{{- $templates := "" }}
{{- if ne $c.role "lab" }}
  {{- $routes := list }}
  {{- with $n.deadman }}
    {{- /* the dead man's switch: Watchdog always fires; an outside watcher alarms when its pings stop */}}
    {{- $w := dict "send_resolved" false }}
    {{- if .url }}
      {{- $_ := set $w "url" .url }}
    {{- else }}
      {{- $_ := set $w "url_file" (include "root.notify.secretFile" (.secretKey | default "deadmanUrl")) }}
      {{- $secret = true }}
    {{- end }}
    {{- $i := .interval | default "1m" }}
    {{- /* sent at every group_interval tick: a repeat_interval equal to it would skip every other tick (the last
    send is always a little later than the tick it came from), so twice the interval between pings */}}
    {{- $routes = append $routes (dict "receiver" "deadman" "matchers" (list `alertname="Watchdog"`) "group_wait" "0s" "group_interval" $i "repeat_interval" "1s") }}
    {{- $receivers = append $receivers (dict "name" "deadman" "webhook_configs" (list $w)) }}
  {{- end }}
  {{- /* always firing by design (Watchdog: the dead man's switch above, if any) */}}
  {{- $routes = append $routes (dict "receiver" "null" "matchers" (list `alertname=~"Watchdog|InfoInhibitor"`)) }}
  {{- /* only prod notifies: dev / staging alerts stay visible in Alertmanager and Grafana. Their low traffic and the
  fault experiments run there made every short fault page twice (fast burn, then the 6h window after recovery). */}}
  {{- $routes = append $routes (dict "receiver" "null" "matchers" (list `namespace=~".+-(dev|staging)"`)) }}
  {{- /* SLO alerts (page / ticket, platform/slo) notify. The chart's default rules (critical / warning / info) are
  causes, not symptoms: dashboards only (several false-positive on k3s, where the control plane runs in one process). */}}
  {{- range $sev := list "page" "ticket" }}
    {{- $list := index $n $sev | default list }}
    {{- if $list }}
      {{- $rcv := dict "name" $sev }}
      {{- range $i, $r := $list }}
        {{- $x := include "root.notify.receiver" (dict "r" $r "severity" $sev) | fromJson }}
        {{- $_ := set $rcv $x.kind (append (get $rcv $x.kind | default list) $x.config) }}
        {{- if $x.secret }}{{ $secret = true }}{{ end }}
      {{- end }}
      {{- $receivers = append $receivers $rcv }}
    {{- end }}
    {{- $routes = append $routes (dict "receiver" (ternary $sev "null" (not (empty $list))) "matchers" (list (printf `severity="%s"` $sev))) }}
  {{- end }}
  {{- $_ := set $route "routes" $routes }}
  {{- if gt (len $receivers) 1 }}
    {{- $lang := $n.language | default .Values.language | default "en" }}
    {{- $templates = printf "%s\n%s" (.Files.Get "files/notify/common.tmpl") (.Files.Get (printf "files/notify/%s.tmpl" $lang)) }}
  {{- end }}
{{- end }}
{{- $config := dict "route" $route "receivers" $receivers "inhibit_rules" (list
      (dict "source_matchers" (list `alertname="ErrorBudgetBurn"` `severity="page"` `long="1h"`)
            "target_matchers" (list `alertname="ErrorBudgetBurn"` `severity="page"` `long="6h"`)
            "equal" (list "namespace" "slo"))
      (dict "source_matchers" (list `alertname="ErrorBudgetBurn"` `severity="page"`)
            "target_matchers" (list `alertname="ErrorBudgetBurn"` `severity="ticket"`)
            "equal" (list "namespace" "slo"))) }}
{{- if $templates }}{{ $_ := set $config "templates" (list "/etc/alertmanager/config/*.tmpl") }}{{ end }}
{{- dict "config" $config "secret" $secret "templates" $templates | toJson }}
{{- end }}

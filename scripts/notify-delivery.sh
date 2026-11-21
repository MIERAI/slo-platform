#!/usr/bin/env bash
# Delivery test of notify (docs/configuration.md#notify), no third-party account: the root chart renders a prod
# cluster's Alertmanager configuration with a receiver of every HTTP / SMTP type; it runs in Alertmanager (the version
# kube-prometheus-stack runs) in Docker, its secrets mounted as in the cluster, the receivers pointed at local stand-ins:
#   ntfy   a real ntfy server (JSON publishing; read back through its API)
#   email  mailpit (SMTP with login; read back through its API)
#   slack, discord, telegram, msteams, mattermost, webhook, deadman, and a second ntfy: a catcher that records requests
# Alerts are posted to Alertmanager's API, firing then resolved. Checked: every receiver gets both, in the configured
# language; dev namespaces, cause alerts and inhibited alerts stay silent; the dead man's switch pings at its interval;
# tokens arrive where they belong. Test-only changes to the configuration: shorter group_wait / group_interval, and
# Telegram's API URL (fixed to api.telegram.org by the platform) pointed at the catcher. Pushover has no such URL:
# untested here. Needs docker, helm, python3 with PyYAML, curl. Leaves nothing behind.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
AM_IMAGE=quay.io/prometheus/alertmanager:v0.34.2      # kube-prometheus-stack 92.17.0
NTFY_IMAGE=binwiederhier/ntfy:v2.11.0
MAILPIT_IMAGE=axllent/mailpit:v1.21
CATCHER_IMAGE=python:3.12-alpine
NET=slo-notify-delivery
tmp=$(mktemp -d)
cleanup() {
  docker rm -f nd-am nd-ntfy nd-mailpit nd-catcher > /dev/null 2>&1 || true
  docker network rm "$NET" > /dev/null 2>&1 || true
  rm -rf "$tmp"
}
trap cleanup EXIT
cleanup; tmp=$(mktemp -d)

cat > "$tmp/platform.yaml" <<'EOF'
apiVersion: slo-platform/v1
product: { repo: https://github.com/MIERAI/slo-platform.git, version: main }
instance: { repo: https://github.com/your-org/slo-instance.git, revision: main }
clusters:
  prod: { role: prod }
notify:
  page:
    - { type: ntfy, url: "http://ntfy", topic: slo-page }
    - { type: telegram, chatId: "-1001234567890" }
    - { type: slack, channel: "#pages" }
    - { type: discord }
    - { type: webhook }
  ticket:
    - { type: email, to: ops@example.com, from: alertmanager@example.com, smarthost: "mailpit:1025", username: am, requireTls: false }
    - { type: msteams }
    - { type: mattermost }
    - { type: ntfy, url: "http://ntfy", topic: slo-ticket }
    - { type: ntfy, url: "http://catcher:8080", topic: header-check, secretKey: ntfyToken }
  deadman: { interval: 10s }
EOF

echo "• configuration (root chart, cluster prod)"
helm template slo-platform platform/root --namespace argocd -f "$tmp/platform.yaml" --set cluster=prod > "$tmp/root.yaml"
"$PY" -I scripts/alertmanager-config.py "$tmp/root.yaml" "$tmp/am" | sed 's/^/  /'
"$PY" -I - "$tmp/am/alertmanager.yaml" <<'PY'
import sys, yaml
p = sys.argv[1]
c = yaml.safe_load(open(p))
c["templates"] = ["/etc/alertmanager/config/*.tmpl"]           # where the container mounts them
c["route"].update(group_wait="2s", group_interval="10s")        # test-only: faster
for r in c["receivers"]:
    for t in r.get("telegram_configs", []):
        t["api_url"] = "http://catcher:8080/telegram"           # test-only: Telegram's API
yaml.safe_dump(c, open(p, "w"), allow_unicode=True, sort_keys=False)
PY
mkdir -p "$tmp/secrets" "$tmp/out"
for kv in telegramBotToken=TESTTOKEN slackWebhookUrl=http://catcher:8080/slack discordWebhookUrl=http://catcher:8080/discord \
          webhookUrl=http://catcher:8080/webhook smtpPassword=s3cret msteamsWebhookUrl=http://catcher:8080/msteams \
          mattermostWebhookUrl=http://catcher:8080/mattermost ntfyToken=tk_test deadmanUrl=http://catcher:8080/deadman; do
  printf '%s\n' "${kv#*=}" > "$tmp/secrets/${kv%%=*}"    # a trailing newline, as editors and `kubectl create secret --from-file` leave it
done
cat > "$tmp/catcher.py" <<'PY'
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        with open("/out/requests.jsonl", "a") as f:
            f.write(json.dumps({"path": self.path, "auth": self.headers.get("Authorization"), "body": body}) + "\n")
        out = json.dumps({"ok": True, "result": {"message_id": 1, "date": 0, "chat": {"id": 1}}}).encode() \
            if self.path.startswith("/telegram/") else b"ok"
        self.send_response(200)
        self.end_headers()
        self.wfile.write(out)
    def log_message(self, *a):
        pass
HTTPServer(("", 8080), H).serve_forever()
PY
chmod -R a+rX "$tmp"; chmod a+rwx "$tmp/out"

echo "• containers"
docker network create "$NET" > /dev/null
docker run -d --name nd-catcher --network "$NET" --network-alias catcher -v "$tmp/catcher.py:/catcher.py:ro" \
  -v "$tmp/out:/out" "$CATCHER_IMAGE" python -u /catcher.py > /dev/null
docker run -d --name nd-ntfy --network "$NET" --network-alias ntfy -p 127.0.0.1::80 "$NTFY_IMAGE" serve \
  --cache-file /tmp/cache.db > /dev/null
docker run -d --name nd-mailpit --network "$NET" --network-alias mailpit -p 127.0.0.1::8025 "$MAILPIT_IMAGE" \
  --smtp-auth-accept-any --smtp-auth-allow-insecure > /dev/null
docker run -d --name nd-am --network "$NET" -p 127.0.0.1::9093 -v "$tmp/am:/etc/alertmanager/config:ro" \
  -v "$tmp/secrets:/etc/alertmanager/secrets/alertmanager-notify:ro" "$AM_IMAGE" \
  --config.file=/etc/alertmanager/config/alertmanager.yaml --storage.path=/tmp > /dev/null
port() { docker port "$1" "$2" | head -1 | sed 's/.*://'; }
AM=http://127.0.0.1:$(port nd-am 9093) NTFY=http://127.0.0.1:$(port nd-ntfy 80) MAIL=http://127.0.0.1:$(port nd-mailpit 8025)
for _ in $(seq 30); do curl -fs "$AM/-/ready" > /dev/null && break; sleep 1; done
curl -fs "$AM/-/ready" > /dev/null || { docker logs nd-am; echo "✗ Alertmanager did not start"; exit 1; }

# alerts <end: "" (firing) or a timestamp (resolved)>: as Prometheus sends them (Pyrra's ErrorBudgetBurn labels)
alerts() {
  "$PY" -I - "$1" <<'PY' | curl -fsS -X POST -H 'Content-Type: application/json' --data-binary @- "$AM/api/v2/alerts"
import json, sys
end = sys.argv[1]
def a(labels, summary="", runbook=""):
    x = {"labels": labels, "annotations": {k: v for k, v in (("summary", summary), ("runbook", runbook)) if v}}
    if end:
        x["endsAt"] = end
    return x
burn = lambda ns, slo, sev, long: {"alertname": "ErrorBudgetBurn", "namespace": ns, "slo": slo, "severity": sev, "long": long}
print(json.dumps([
    a(burn("storefront-prod", "storefront-api-availability", "page", "1h"),
      "storefront prod: api-availability SLO 99.5% is burning its error budget",
      "https://github.com/MIERAI/slo-platform/blob/main/docs/runbook.md#error-budget-burn"),
    a(burn("storefront-prod", "storefront-api-availability", "page", "6h"), "INHIBITED-6H"),
    a(burn("storefront-prod", "storefront-api-latency", "ticket", "1d"), "latency budget eroding", "https://example.com/rb#latency"),
    a(burn("storefront-prod", "storefront-api-availability", "ticket", "1d"), "INHIBITED-TICKET"),
    a(burn("storefront-dev", "storefront-api-availability", "page", "1h"), "SILENT-DEV"),
    a({"alertname": "KubePodCrashLooping", "namespace": "storefront-prod", "severity": "warning"}, "SILENT-CAUSE"),
] + ([] if end else [a({"alertname": "Watchdog", "severity": "none"})])))
PY
}
echo "• firing"
alerts ""
sleep 25
cp "$tmp/out/requests.jsonl" "$tmp/out/firing.jsonl"
echo "• resolved"
alerts "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
sleep 25
curl -fs "$NTFY/slo-page,slo-ticket/json?poll=1" > "$tmp/out/ntfy.jsonl"
curl -fs "$MAIL/api/v1/messages" > "$tmp/out/mail.json"
docker logs nd-am > "$tmp/out/am.log" 2>&1

"$PY" -I - "$tmp/out" <<'PY'
import collections, json, os, sys
out = sys.argv[1]
load = lambda f: [json.loads(l) for l in open(os.path.join(out, f)) if l.strip()]
reqs, firing = load("requests.jsonl"), load("firing.jsonl")
fails = []
def check(cond, msg):
    print(("  ✓ " if cond else "  ✗ ") + msg)
    if not cond:
        fails.append(msg)
paths = collections.Counter(r["path"] for r in reqs)
for p in ("/slack", "/discord", "/telegram/botTESTTOKEN/sendMessage", "/webhook", "/msteams", "/mattermost", "/"):
    texts = [r["body"] for r in reqs if r["path"] == p]
    check(len(texts) >= 2 and "FIRING" in texts[0] or p == "/webhook" and '"status":"firing"' in texts[0],
          f"{p}: firing ({len(texts)} requests)")
    check(any("RESOLVED" in t or '"status":"resolved"' in t for t in texts[1:]), f"{p}: resolved")
everything = " ".join(r["body"] for r in reqs)
for word in ("SILENT-DEV", "SILENT-CAUSE", "storefront-dev"):
    check(word not in everything, f"never sent: {word}")
# inhibition mutes firing alerts only: once the page resolves, Alertmanager lists the formerly inhibited alerts too
for word, what in (("INHIBITED-6H", "the 6h page"), ("INHIBITED-TICKET", "the same SLO's ticket")):
    check(all(word not in r["body"] for r in firing), f"inhibited while the 1h page fires: {what}")
ntfy_auth = {r["auth"] for r in reqs if r["path"] == "/"}
check(ntfy_auth == {"Bearer tk_test"}, f"ntfy access token as Bearer ({ntfy_auth})")
tg = [json.loads(r["body"]) for r in reqs if r["path"].startswith("/telegram/")]
check(tg and tg[0]["chat_id"] in ("-1001234567890", -1001234567890) and tg[0]["parse_mode"] == "HTML", "telegram chat_id, HTML")
slack = json.loads(next(r["body"] for r in reqs if r["path"] == "/slack"))["attachments"][0]
check(slack["title"] == "[PAGE] storefront-prod: storefront-api-availability" and "Runbook: https://" in slack["text"],
      f"text: title and runbook ({slack['title']})")
dm = paths["/deadman"]
check(4 <= dm <= 7, f"dead man's switch: {dm} pings in ~50s at a 10s interval")
ntfy = load("ntfy.jsonl")
got = [(m["topic"], m["priority"], m["message"].split("\n")[0]) for m in ntfy]
check(("slo-page", 5, "[PAGE] storefront-prod: storefront-api-availability") in got, "ntfy page: priority 5")
check(any(t == "slo-ticket" and p == 2 and m.startswith("[TICKET RESOLVED] storefront-prod") for t, p, m in got),
      "ntfy ticket resolved: priority 2")
mail = json.load(open(os.path.join(out, "mail.json")))
subjects = sorted(m["Subject"] for m in mail["messages"])
check(len(subjects) == 2 and subjects[0].startswith("[TICKET RESOLVED] storefront-prod")
      and subjects[1] == "[TICKET] storefront-prod: storefront-api-latency", f"email (SMTP login): {subjects}")
errors = [l for l in open(os.path.join(out, "am.log")) if "level=ERROR" in l]
check(not errors, "no notification errors in Alertmanager's log" + ("".join(errors[:3]) and ": " + errors[0]))
sys.exit(1 if fails else 0)
PY
echo "✓ delivered"

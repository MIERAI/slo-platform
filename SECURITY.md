# Security policy

Report vulnerabilities privately through GitHub's "Report a vulnerability" (Security tab), not in public issues.
You will get an answer within 7 days.

Defaults the platform keeps: UIs without authentication (Prometheus, Alertmanager, Pyrra) are not exposed outside the
cluster unless the instance's configuration says so; secrets live in SOPS-encrypted files, never in plain manifests.

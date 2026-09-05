# Security policy

## Supported versions

VisualWorld has not released a supported version yet. Security fixes currently
target the default branch.

## Reporting a vulnerability

Do not open a public issue with exploit details, secrets, private media, or
personal data. Use the repository's enabled GitHub private vulnerability
reporting channel. A dedicated security contact may be added before the first
release; if the private channel is ever unavailable, pause disclosure rather
than posting details publicly.

Include the affected revision, impact, safe reproduction steps, and suggested
mitigation. Do not access data you do not own or have permission to test.

## Security boundaries

Video, frames, OCR, captions, model output, datasets, URLs, and contributor
content are untrusted data. They must never directly cause command execution,
SQL execution, file access, network requests, configuration changes, or secret
disclosure. See [the threat model](docs/security/threat-model.md) and
[prompt-injection policy](docs/security/prompt-injection.md).

# Security Policy

## Supported versions

Remote Library Client is distributed as tagged releases. Security fixes land on the latest
release; older tagged builds are not separately patched. Please run the most recent release.

A security fix only protects you once it's installed, so keep the plugin up to date. How you
update depends on how you installed it (see the README's Install section):

- **A git-clone install updates in place** through FeedBack's plugin manager —
  **Check for Updates → Update** pulls the latest commit and applies it on the next restart.
- **A release-zip install is not picked up by "Check for Updates"** — download the newer zip
  from the [Releases](https://github.com/Taynavv/feedback-remote-library-client/releases) page
  and replace the plugin folder yourself.

## Reporting a vulnerability

Report security issues **privately** — do not open a public issue for anything that could
be exploited before a fix ships.

**Preferred:** use GitHub's private vulnerability reporting on this repository
(**Security → Report a vulnerability**). This opens a private advisory visible only to the
maintainers and requires no email round-trip.

The plugin is maintained by [@Taynavv](https://github.com/Taynavv). If you cannot use
GitHub's private reporting, open a minimal public issue asking for a private contact
channel — **without disclosing details** — and the maintainer will follow up.

Please include:

- affected version (`plugin.json` → `version`),
- a description of the issue and its impact,
- steps to reproduce or a proof of concept,
- any suggested remediation.

We aim to acknowledge reports within a few days.

## Scope and threat model

This plugin connects to **user-configured** Remote Library Server URLs, makes outbound
HTTP(S) requests to them, and writes downloaded packages and NAM-tone assets into a local
cache. Treat every configured server as semi-trusted: **only add servers you control or
trust.**

Hardening already in place:

- Base URLs must use `http`/`https`; other schemes are rejected.
- Response bodies are size-capped (JSON, binary, package, and error responses).
- Downloaded file names are sanitized and confined to their target directories.
- By default the client refuses to follow a redirect that pivots to a *different*
  internal / loopback / link-local host (an SSRF guard). This can be disabled per source
  with the **Allow unsafe redirects** toggle when a trusted server legitimately relies on
  such redirects.

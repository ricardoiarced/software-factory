# Stable release signals for Codex CLI and Claude Code

_Research date: 2026-09-23. Live registry, API, pointer, and documentation queries were run on this date from the Linux/WSL2 Factory host (installed: `codex-cli 0.156.0`, `claude 2.1.278`). This file resolves the research question in [issue #98](https://github.com/ricardoiarced/software-factory/issues/98) under map [#97](https://github.com/ricardoiarced/software-factory/issues/97). It proposes a verification strategy and deliberately implements nothing._

## Decision

**Codex CLI.** The single authoritative stable-release signal is the GitHub Releases API endpoint `GET https://api.github.com/repos/openai/codex/releases/latest`. It is the only surface where one unauthenticated response returns, together, (a) the newest stable version — GitHub selects it server-side as "the most recent non-prerelease, non-draft release", so alpha trains never leak through[^gh-releases-api] — and (b) the exact platform artifact with its `sha256` `digest` field on every asset object[^gh-assets-api]. Live proof of the stable filter: on 2026-09-23 the endpoint returned `rust-v0.156.1` (`prerelease: false`) even though the strictly newer `rust-v0.158.0-alpha.2` (`prerelease: true`) had already been published.[^codex-gh-list] npm `latest` agreed (`0.156.1`), and upstream Codex itself treats "npm `latest` == GitHub latest release" as a required invariant, bailing when the two disagree.[^codex-npm-registry-rs] Use npm only as the artifact-identity cross-check when Factory certifies npm-distributed artifacts.

**Claude Code.** The authoritative version signal is the npm registry dist-tag set for `@anthropic-ai/claude-code`, fetched as one small unauthenticated document (`GET https://registry.npmjs.org/-/package/@anthropic-ai%2Fclaude-code/dist-tags`). It exposes both publisher channels in a single response — `latest: 2.1.280`, `stable: 2.1.267`, `next: 2.1.280` on 2026-09-23[^cc-dist-tags] — and `latest` equals GitHub `releases/latest` (`v2.1.280`) and the native-channel pointer `https://downloads.claude.ai/claude-code-releases/latest` (`2.1.280`).[^cc-pointers] GitHub's `prerelease` flag is useless for this harness: none of the last 100 releases used it, so it cannot filter anything; the publisher instead curates a delayed `stable` channel (~1 week behind, documented as "skipping releases with major regressions").[^cc-setup-docs] For artifact identity on native installs — the install type on this host — the integrity source is the publisher's per-release manifest `https://downloads.claude.ai/claude-code-releases/<version>/manifest.json`: per-platform SHA-256 + size, GPG-signed (`manifest.json.sig`, key fingerprint `31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE`), and documented so that "verifying the signature on the manifest transitively verifies every binary it lists".[^cc-setup-docs]

**Artifact identity.** A version string alone never certifies anything. The certification tuple stays `(publisher version, exact artifact SHA-256, OS, architecture, install channel)`, matching the existing candidate-manifest schema (`official_sha256` + `official_source`)[^cert-readme] and the production policy that hashes the resolved executable on every Run.[^issue11]

**Fail-closed reporting.** Every lookup failure (unreachable, timeout, rate-limited, malformed, or surfaces disagreeing) refuses the Run for that harness and names the remediation class. The harness-vs-Factory distinction is determinable by comparison order: installed version < newest stable → "update the harness"; installed version ≥ newest stable but the exact tuple is not in Factory's embedded Compatibility Set → "update Factory"; lookup itself failed → "check connectivity, retry" with no stale-allow fallback.

Observed world state on 2026-09-23 — the exact refusal scenario is live today:

| Quantity | Codex CLI | Claude Code |
| --- | --- | --- |
| Newest stable (publisher) | 0.156.1 (npm `latest` == GitHub `releases/latest`) | 2.1.280 (`latest` channel) |
| Publisher's delayed `stable` channel | n/a (none exists) | 2.1.267 |
| Installed on this host | 0.156.0 (standalone install) | 2.1.278 (native install, auto-updates off) |
| Factory's certified production tuple | 0.155.1 (`_Codex01551Policy`)[^codex-policy] | none (uncertified)[^map97] |
| Newest prerelease (excluded) | 0.158.0-alpha.2 | none exist |

Installed Codex is newer than Factory's certified tuple (refuse, "update Factory") and older than the publisher's newest stable (refuse, "update harness"). Installed Claude Code is behind `latest` and ahead of `stable`, demonstrating that channel selection changes the verdict and must be explicit.

## Policy frame

Map [#97][^map97] supersedes two points of the compatibility policy recorded in issue #11, quoted verbatim from the issue body:

- Superseded — network avoidance: "Admission performs no inference request and should avoid network access." The new policy *requires* an immediate-before-Run lookup against the publisher, and "Failure to reach the publisher to verify latest status also stops that harness's Run."
- Superseded — offline-only allowlist: "Compatibility data ships with Factory. Factory does not fetch a mutable remote allowlist, and it never invokes `codex update`." The first sentence stands (Factory still embeds its own certification set and never fetches a third-party allowlist); the *effect* of offline-only admission — "it currently … allows an older certified artifact after a newer publisher release" — is superseded: a newer publisher release now stops Runs for that harness.
- Retained unchanged: exact-tuple certification ("An upgraded Codex remains unsupported until that exact artifact is certified."), no auto-update, no bypass, Linux + macOS host scope.

## Codex CLI

### Release metadata surfaces

| Surface | Endpoint | Auth | Newest stable on 2026-09-23 | Notes |
| --- | --- | --- | --- | --- |
| GitHub Releases API | `GET /repos/openai/codex/releases/latest` | none (60 req/h unauth; 5,000/h authed)[^gh-ratelimits] | `rust-v0.156.1`, `prerelease: false`, 176 assets each with `digest: "sha256:<64hex>"`[^codex-gh-latest] | Also publishes `codex-package_SHA256SUMS` and `.sigstore` sidecar assets. |
| npm registry | `GET https://registry.npmjs.org/@openai%2Fcodex/latest` (dist-tag endpoint) | none[^codex-npm-unauth] | dist-tag `latest: 0.156.1` | Each npm version carries `dist.integrity` (sha512 SRI), `dist.shasum`, and SLSA provenance attestations.[^codex-npm-latest] |
| Official docs | `https://developers.openai.com/codex/cli` → 308 → `https://learn.chatgpt.com/docs/codex/cli` | none | no version shown | Standalone installer (`curl -fsSL https://chatgpt.com/codex/install.sh | sh`) listed first; npm and Homebrew as "You can also install" alternatives. Zero occurrences of "alpha", "prerelease", "stable", or "channel" on the page (grep-verified).[^codex-docs] |
| CLI's own updater | `https://api.github.com/repos/openai/codex/releases/latest` + `https://formulae.brew.sh/api/cask/codex.json` + npm packument | none | caches for 20 h | `codex-rs/tui/src/updates.rs`: refreshes in background, rechecks at most every 20 hours, and selects the endpoint by install method (brew → cask JSON; npm-global → GitHub first, then npm cross-check; standalone → GitHub only).[^codex-updates-rs] |

The GitHub release *is* the stable channel: every stable tag `rust-v<semver>` is published with `prerelease: false`, every `rust-v<semver>-alpha.N` with `prerelease: true` (3 of the 4 most recent releases were prereleases on 2026-09-23).[^codex-gh-list]

### Prerelease and platform filtering

- **GitHub:** `/releases/latest` excludes drafts and prereleases server-side — documented as "The latest release is the most recent non-prerelease, non-draft release, sorted by the `created_at` attribute" (where `created_at` is the date of the commit used for the release, not the publish time).[^gh-releases-api] Factory should still defensively assert `prerelease == false` and `draft == false` on the response.
- **npm:** prerelease builds carry a semver prerelease identifier (`0.157.0-alpha.9`), and platform builds are *alias versions of the same package*, not separate packages: `@openai/codex@0.156.1-linux-x64` (with `"os": ["linux"], "cpu": ["x64"]`), referenced from the launcher's `optionalDependencies` via `npm:@openai/codex@0.156.1-linux-x64`. There is no `@openai/codex-linux-x64` package (E404 verified).[^codex-npm-latest] `latest` points only at unsuffixed stable versions today; semver makes any prerelease-suffixed version lower precedence than its normal version — "Pre-release versions have a lower precedence than the associated normal version" — so the deterministic filter is: reject any version containing a prerelease identifier or a `-<platform>` suffix.[^semver]
- **Host filter (GitHub assets):** exact asset names per platform, listed in the README: Linux x86_64 `codex-x86_64-unknown-linux-musl.tar.gz`, Linux arm64 `codex-aarch64-unknown-linux-musl.tar.gz`, macOS `codex-{x86_64,aarch64}-apple-darwin.tar.gz`.[^codex-readme] Linux assets target **musl only** (no `-gnu` asset exists among the 176).

### Artifact identity

- GitHub asset objects carry `digest` — "required, string or null" — observed populated as `sha256:<64hex>` on the sampled asset objects of `rust-v0.156.1` (e.g. asset `argument-comment-lint`: `sha256:16c3b1f90841562b65246445b7c6718acb6dc4577566bb07f968b1deef1e427f`).[^gh-assets-api][^codex-gh-latest] A `codex-package_SHA256SUMS` asset and sigstore sidecars (`*.sigstore`) supplement this; the checksum file's contents were not downloaded/verified in this research.
- npm: `dist.integrity` (sha512 SRI) and `dist.shasum` per version, plus registry provenance attestations (`predicateType: https://slsa.dev/provenance/v1`). Identity must be per-artifact: the `latest` launcher tarball is a 3-file, 13 KB JS shim, while the platform alias version `0.156.1-linux-x64` unpacks to ~387 MB containing the binary.[^codex-npm-latest]
- Factory already anchors certification on the GitHub artifact: candidate manifests record `official_sha256` with `official_source` pointing into `https://github.com/openai/codex/releases/...`, reviewed independently of the installed binary.[^cert-readme] Run Admission re-hashes the resolved executable every Run and aborts if identity changes mid-admission.[^issue11]

### Mutable metadata and races

- npm dist-tags are mutable pointers controlled by the publisher: "Publishing a package sets the `latest` tag to the published version unless the `--tag` option is used"; tags can be added, moved, and removed after the fact, and a publisher *can* put a prerelease-suffixed version under `latest` — the protection is convention ("Typically, projects only use the `latest` tag for stable release versions"), not the registry.[^npm-disttag-docs] This is why GitHub's server-side prerelease exclusion is the better primary filter for Codex.
- A release can land between lookup and launch: stable releases ship roughly 1–3 times per week (0.155.0 → 0.155.1 → 0.156.0 → 0.156.1 between 2026-09-17 and 2026-09-23), and alpha trains publish many times a day.[^codex-npm-time] Under the map's policy this race is self-correcting: because every Run re-queries and admission refuses non-newest binaries, a Run admitted just before a publish is merely one release old at launch and is caught at the next admission. The lookup result (version, etag, timestamp) should be recorded in the admission output for auditability.
- Surfaces can transiently disagree during a publish window; upstream's `ensure_version_ready` treats that as fatal: `"npm latest dist-tag points to {latest}, expected GitHub release {version}"` and `"npm package is missing latest dist-tag"`.[^codex-npm-registry-rs] This is upstream precedent for Factory's own ambiguity rule.
- Version metadata itself is effectively immutable once published (npm rejects re-publishing an existing version), and GitHub asset digests are stable once uploaded; the mutable parts are *which* release is latest and *which* version a tag names.
- Observed HTTP caching on `/releases/latest`: `Cache-Control: private, max-age=60, s-maxage=60` with a weak ETag — a response can be up to 60 seconds stale at CDN/HTTP layer, an irreducible (and acceptable) race window.[^codex-gh-headers]

## Claude Code

### Release metadata surfaces

| Surface | Endpoint | Auth | Value on 2026-09-23 | Notes |
| --- | --- | --- | --- | --- |
| npm registry | `GET https://registry.npmjs.org/-/package/@anthropic-ai%2Fclaude-code/dist-tags` | none (Cloudflare-fronted; unauth `/latest` verified)[^cc-npm-unauth] | `latest: 2.1.280`, `next: 2.1.280`, `stable: 2.1.267` | 522 published versions, **zero** ever carried a prerelease suffix; all tags point at plain `X.Y.Z`.[^cc-dist-tags] |
| GitHub Releases | `GET /repos/anthropics/claude-code/releases/latest` | none | `v2.1.280`, `prerelease: false` | `prerelease: true` unused across the last 100 releases; the release feed doubles as the changelog (`CHANGELOG.md` top entry matches release body). Assets: 8 platform tarballs + `SHASUMS256.txt` + `SHASUMS256.txt.sig` (PGP).[^cc-gh] |
| Native channel (GCS-backed) | `GET https://downloads.claude.ai/claude-code-releases/{stable,latest}` then `…/<version>/manifest.json` (+ `.sig`) | none | `stable → 2.1.267`, `latest → 2.1.280`; manifest for 2.1.280 is 2,161 bytes, 8 platform entries, GPG-signed | `install.sh` resolves `DOWNLOAD_BASE_URL="https://downloads.claude.ai/claude-code-releases"`, reads the `latest` pointer, fetches `<version>/manifest.json`, and verifies the downloaded binary against the manifest checksum; it does **not** verify the GPG signature.[^cc-install-sh] |
| Official docs | `https://code.claude.com/docs/en/setup` | none | — | Defines the two channels and the update machinery (below). README marks npm as deprecated: "Installation via npm is deprecated. Use one of the recommended methods below." (recommended: native installer, brew, winget).[^cc-readme] |
| apt / brew | apt `stable`/`latest` suites; casks `claude-code` (stable) and `claude-code@latest` | none | apt stable newest = `2.1.267-1`, latest = `2.1.280-1` | Same signing key fingerprint as the manifest (`31DD…CACE`).[^cc-setup-docs] |

### Channel semantics — the ambiguity Factory must resolve by decision

Claude Code has no prerelease artifacts to filter. It has two publisher-managed *channels*:

- `latest` — "receive new features as soon as they're released" (default; the `autoUpdatesChannel` setting's default).
- `stable` — "use a version that is typically about one week old, skipping releases with major regressions", embodied consistently in npm's `stable` tag, the GCS `stable` pointer, the apt `stable` suite, and the `claude-code` brew cask.[^cc-setup-docs][^cc-pointers]

Under map #97's definition — "newest stable release, excluding prereleases" — the matching signal is `latest`: there is nothing prerelease to exclude, and `stable` is a *curated delayed subset*, not a prerelease filter. The ticket's phrase "for the selected … release channel" can be honored by making the channel an explicit Factory input whose two values read exactly the publisher's two pointers/tags; Factory must never infer it from context. (Recommendation and the residual decision are in Open questions.)

### Prerelease and platform filtering

- Nothing to exclude today: no `-beta`/`-rc`/`-alpha` version has ever been published to npm, and GitHub's `prerelease` flag has never been set. The deterministic filter is still the semver prerelease-identifier test — it is a no-op that becomes load-bearing if Anthropic ever ships prerelease artifacts — plus explicit channel selection (`latest` vs `stable`).[^semver]
- Platform artifacts: npm resolves the binary through pinned per-platform optional dependencies — `@anthropic-ai/claude-code-{linux-x64,linux-arm64,linux-x64-musl,linux-arm64-musl,darwin-x64,darwin-arm64,win32-x64,win32-arm64}`, each with `os`/`cpu` fields and its own `dist.integrity`; all pinned to the meta version (2.1.280).[^cc-npm-latest] GitHub tarballs are named `claude-<platform>-<arch>[-musl].tar.gz`; the manifest keys platforms as `linux-x64`, `linux-arm64-musl`, `darwin-arm64`, etc. The host filter is an exact key/name match on `(os, arch, libc)` — no substring matching.

### Artifact identity

- Native install (this host): `~/.local/bin/claude → ~/.local/share/claude/versions/2.1.278`; the docs describe this launcher-symlinks-into-versions layout. Identity = the versioned binary's SHA-256 as published in the signed `manifest.json` for that version (`platforms["linux-x64"].checksum`, plus exact `size`). The vendored local copy of the 2.1.280 manifest is byte-identical in length (2,161 bytes) to the live object.[^cc-install-sh][^cc-setup-docs]
- Manifest signature: `manifestSignatureEnforcement: "flag"` appears in the manifest; the installer ignores it and trusts the checksum fetched over TLS. An isolated live verification succeeded: importing `https://downloads.claude.ai/keys/claude-code.asc` yields `Anthropic Claude Code Release Signing <security@anthropic.com>`, fingerprint `31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE`, and `gpg --verify manifest.json.sig manifest.json` reports a good signature. Docs: "The manifest is signed with an Anthropic GPG key, so verifying the signature on the manifest transitively verifies every binary it lists… Manifest signatures are available for releases from `2.1.89` onward."[^cc-setup-docs]
- GitHub: per-asset `digest` (`sha256:<64hex>`) values are byte-identical to the shipped `SHASUMS256.txt` lines; the detached `SHASUMS256.txt.sig` is a PGP signature, but no README/SECURITY documentation describes verifying it (grep: zero matches) — the *documented* verification path is the GCS manifest, not the GitHub signature.[^cc-gh][^cc-readme]
- npm: `dist.integrity` sha512 SRI on both the meta package and each platform package. Docs: "The npm package installs the same native binary as the standalone installer… pulls the binary in through a per-platform optional dependency… and a postinstall step links it into place."[^cc-setup-docs]

### Mutable metadata and races

- Three independent mutable pointers exist (npm dist-tags, GCS `stable`/`latest` objects, GitHub latest-release rollup) plus the brew/apt mirrors. They are not updated atomically: the observed `stable` pointer's `last-modified` equals the 2.1.280 publish instant while still naming `2.1.267` — proof that the publisher rewrites pointers on every release on its own schedule, and that a Factory cross-check between surfaces can observe a transient, *intentional* divergence (channel lag), which is different from a publish-window race and must not be treated as corruption.[^cc-pointers]
- The native channel self-updates: "Native installations automatically update in the background to keep you on the latest version… Updates download and install in the background, then take effect the next time you start Claude Code" (`DISABLE_AUTOUPDATER` stops the background check; `DISABLE_UPDATES` blocks all update paths).[^cc-setup-docs] An installed artifact can therefore change between Factory invocations; the retained per-Run executable re-hash and mid-admission abort covers exactly this window.[^issue11]
- Version rollups: `claude update` reports the transition explicitly ("Successfully updated from <old> to version <new>"), and `minimumVersion` prevents downgrade when switching to `stable` — useful bounds when reasoning about which version a channel check will return.[^cc-setup-docs]

## Cross-cutting comparison

### Which endpoint to trust

| | Codex CLI primary | Claude Code primary |
| --- | --- | --- |
| Version of newest stable | `GET api.github.com/repos/openai/codex/releases/latest` (server-side prerelease/draft exclusion) | `GET registry.npmjs.org/-/package/@anthropic-ai%2Fclaude-code/dist-tags` (both channels in one document; `latest` == GitHub latest == GCS `latest`) |
| Artifact digest in the same or next response | same response (`assets[].digest`) | next request: `downloads.claude.ai/claude-code-releases/<version>/manifest.json` (+ `.sig`) |
| Cross-check | npm `latest` dist-tag (upstream demands equality) | GCS pointer vs npm tag equality (for the chosen channel); GitHub `releases/latest` as second opinion |
| Why | only surface that filters prereleases server-side; asset digests co-located | GitHub flag unused; npm exposes the publisher's own channel split; single small unauth document |

### Failure modes and required reports (fail-closed)

| Condition | Detection | Report | Action surfaced to user |
| --- | --- | --- | --- |
| Network unreachable / DNS failure | connect error | `factory: error:` naming harness + surface + "cannot reach publisher" | check connectivity; retry (no stale-allow) |
| Timeout | bounded deadline exceeded | same class as unreachable | same |
| Rate-limited (GitHub 403/429 with `x-ratelimit-remaining: 0`; npm 429) | status + `Retry-After` / `x-ratelimit-reset`[^gh-ratelimits][^npm-rateblog] | "publisher rate-limited the lookup" | wait and retry; do not retry before the reset instant |
| Malformed metadata (missing `tag_name`/dist-tags, non-semver, missing digest fields) | strict schema validation | "unrecognized publisher metadata" | update Factory (publisher format drift) |
| No stable release found (`/releases/latest` 404, empty dist-tags) | endpoint semantics | "no stable release published" | check publisher status |
| Surfaces disagree (npm `latest` ≠ GitHub latest; pointer ≠ tag for same channel) | cross-check mismatch | "ambiguous newest-stable evidence" with both observed values | wait for publish window to settle, then retry; report if persistent (update Factory) |
| Installed < newest stable | semver comparison | "newer publisher release exists (<version>)" | update the harness (exact per-channel command) |
| Installed ≥ newest stable but tuple not in embedded Compatibility Set | set membership | "release not certified in this Factory" | update Factory |
| Installed artifact changed between lookup and launch | existing executable re-hash | existing isolation-failure diagnostic | existing recovery path |

The harness-vs-Factory distinction (#97 requirement) falls out of comparison order and needs no extra metadata: only the last two rows point at Factory.

### Timeouts, rate limits, caching

- GitHub REST: "The primary rate limit for unauthenticated requests is 60 requests per hour"; authenticated requests draw on a 5,000/hour personal limit (live `rate_limit` check on 2026-09-23: `core.limit: 5000`). Secondary limits return `retry-after`; clients "should not retry" until `x-ratelimit-reset`. Conditional requests are cheap: "Making a conditional request does not count against your primary rate limit if a 304 response is returned", making ETag polling the documented efficient pattern; the docs also warn that "Continuing to make requests while you are rate limited may result in the banning of your integration."[^gh-ratelimits][^gh-bestpractices]
- npm registry: no numeric read limit is published; npm's own guidance is to handle `429` ("Your tools should be ready to handle responses with http status code 429, which means that you have exceeded the allowed limit") — the npm blog post announcing this is discontinued as a live URL, so treat it as historical policy, not a current SLA. Observed responses are Cloudflare-fronted (`server: cloudflare`, `cf-cache-status: DYNAMIC`). One or two GETs per Run is negligible against both services' limits.[^npm-rateblog][^cc-npm-unauth]
- Recommended request shape (Factory policy, not publisher-published): one unauthenticated GET per harness, HTTP connect timeout ~3 s, total timeout ~10 s, at most one retry on transient connect/timeout errors with the retry decision left to the grilling ticket. At that cadence neither rate limit is approachable and a hung lookup costs the Run, not the session.
- HTTP-level caching is bounded and acceptable: GitHub `max-age=60` observed; npm/GCS responses are CDN-fronted. The race window introduced by CDN staleness (≤60 s) is far below the certification-lag window the map already tolerates.
- Caching *decisions* is forbidden: a previously observed "newest stable" may be recorded (timestamped, with etag) for audit, but must never admit a Run — that is the map's no-stale-allow rule.

### Deterministic verification strategy (proposal — not implemented)

1. **Admission lookup (per harness, immediately before submit).** Codex: `GET /repos/openai/codex/releases/latest` → assert `prerelease == false`, `draft == false`; parse `tag_name` after the `rust-v` prefix as strict semver; select the host asset by exact name (`x86_64-unknown-linux-musl` / `aarch64-unknown-linux-musl` / `x86_64-apple-darwin` / `aarch64-apple-darwin`) and read its `digest`. Claude Code: `GET …/dist-tags` → take the user-selected channel's tag (`latest` default); optional native-channel confirmation `GET downloads.claude.ai/claude-code-releases/<channel>` → must equal the tag; artifact checksum from `<version>/manifest.json` when certifying native artifacts.
2. **Comparison ladder** (order produces the harness-vs-Factory verdict): parse installed `codex --version` / `claude --version` output as strict semver, rejecting prerelease-suffixed installs outright as uncertified channels → installed < newest stable ⇒ "update harness" (per-channel command: `npm install -g @openai/codex`, `brew upgrade --cask codex`, standalone installer one-liner, `claude update`, `npm install -g @anthropic-ai/claude-code@latest`) → installed ≥ newest but tuple (version, artifact sha256, OS, arch, channel) absent from the embedded Compatibility Set ⇒ "update Factory" → match ⇒ admit.
3. **Fail-closed rule:** any lookup error, schema violation, or cross-check mismatch refuses the Run with the error class; nothing falls back to a cached allow; the admission record includes the lookup timestamp, response etag, and observed values for audit.
4. **Offline-deterministic CI:** commit recorded publisher responses as fixtures (both repositories' `/releases/latest` and dist-tags documents, with volatile headers stripped). Unit tests exercise parsing, prerelease/platform filtering, the comparison ladder, and every refusal path purely against fixtures — no network. A separately marked integration test may hit the live endpoints and asserts only response *shape* (fields present, semver-parseable, prerelease boolean present), never specific versions, so it cannot flake when publishers ship. Version-specific assertions exist only in fixtures.
5. **Certification loop unchanged:** a new newest-stable version triggers the existing real-host certification suite (`certification/candidates/<tuple>.json` with `official_source` + `official_sha256` reviewed against the publisher response, results recorded under `certification/results/`), and only a Factory release extends the Compatibility Set.[^cert-readme]

## Open questions for the follow-up grilling ticket

1. **Channel definition for Claude Code:** default Factory's "newest stable" to the publisher's `latest` channel (matches map wording) while allowing an explicit user-selected `stable` that reads the publisher's `stable` tag/pointer? What Codex equivalent exists (none — only "latest")?
2. **Ambiguity policy:** on a cross-check mismatch (npm vs GitHub vs pointer), is the rule refuse-immediately, or refuse-after-one-bounded-retry given publish windows can take minutes? Upstream Codex refuses (bails) with no retry.
3. **Retry/timeout numbers:** exact connect/total timeouts and retry count inside a millisecond-scale startup target ("Security checks take precedence over the original millisecond-scale startup target")[^issue11] — the lookup makes Run Admission the slowest path; is that accepted?
4. **Signature verification scope:** must Factory verify Claude Code's `manifest.json` GPG signature (documented, key pinned) and Codex's `codex-package_SHA256SUMS`/sigstore assets, or is TLS + equality with a second surface + certification-time digest review sufficient?
5. **musl-only Codex Linux binaries:** certification scope is `(version, artifact, OS, arch)` — do glibc-distro hosts accept the musl build as the certified artifact, or does npm's `linux-x64` alias version (glibc-compatible) become the certified artifact for those hosts?
6. **Unauthenticated vs tokenized GitHub lookups:** 60 req/h suffices for a single-user CLI (1–2 requests per Run), but Factory should state whether it ever reads `GH_TOKEN` to lift the limit, and that fail-closed behavior is identical in both modes.

## Sources

Primary sources accessed 2026-09-23 unless noted. Live-query observations are attributed inline to the surface that returned them.

[^gh-releases-api]: GitHub REST API — Releases: definition of the latest release ("The latest release is the most recent non-prerelease, non-draft release, sorted by the `created_at` attribute") and `prerelease`/`draft` field semantics. https://docs.github.com/en/rest/releases/releases
[^gh-assets-api]: GitHub REST API — Release assets: asset object schema including `digest` ("required, string or null"). https://docs.github.com/en/rest/releases/assets
[^gh-ratelimits]: GitHub REST API — Rate limits: 60 req/h unauthenticated, 5,000/h authenticated, `x-ratelimit-remaining`/`x-ratelimit-reset`/`retry-after` behavior. https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
[^gh-bestpractices]: GitHub REST API — Best practices: conditional requests ("Making a conditional request does not count against your primary rate limit if a `304` response is returned"), efficient polling, serial requests. https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api
[^semver]: Semantic Versioning 2.0.0, items 9 and 11 ("Pre-release versions have a lower precedence than the associated normal version"). https://semver.org/spec/v2.0.0.html
[^npm-disttag-docs]: npm CLI documentation — `npm dist-tag` (default `latest` semantics, mutability via add/rm, `--tag` on publish, "Typically, projects only use the `latest` tag for stable release versions"). https://docs.npmjs.com/cli/v11/commands/npm-dist-tag
[^npm-rateblog]: npm Blog, "API rate limiting rolling out" (2017-08-30; npm blog since discontinued, post no longer served at its original URL): 429 handling guidance for registry clients. https://blog.npmjs.org/post/164800180295/api-rate-limiting-rolling-out
[^codex-gh-latest]: Live query 2026-09-23: `GET api.github.com/repos/openai/codex/releases/latest` → `tag_name: rust-v0.156.1`, `prerelease: false`, `published_at: 2026-09-23T02:41:36Z`, 176 assets with `digest: "sha256:<64hex>"` including `codex-package_SHA256SUMS` and `*.sigstore` sidecars. Durable surface: https://api.github.com/repos/openai/codex/releases/latest
[^codex-gh-list]: Live query 2026-09-23: `GET /repos/openai/codex/releases?per_page=30` — `rust-v0.158.0-alpha.2` and `rust-v0.157.0-alpha.11` carry `prerelease: true`; `rust-v0.156.1` and `rust-v0.156.0` carry `prerelease: false`.
[^codex-npm-unauth]: Live query 2026-09-23: unauthenticated `GET https://registry.npmjs.org/@openai%2Fcodex/latest` returned the 0.156.1 manifest with Cloudflare/CDN headers. Durable surface: https://registry.npmjs.org/@openai/codex
[^codex-npm-latest]: Live queries 2026-09-23: `npm view @openai/codex dist-tags --json` (`latest: 0.156.1`, `alpha: 0.158.0-alpha.2`, platform-suffixed tags); `npm view @openai/codex@0.156.1 --json` (`optionalDependencies` alias versions `npm:@openai/codex@0.156.1-<platform>`, `dist.integrity`, SLSA `attestations`); `npm view @openai/codex@0.156.1-linux-x64 --json` (`os: ["linux"]`, `cpu: ["x64"]`, ~387 MB unpacked). Durable surface: https://registry.npmjs.org/@openai%2Fcodex
[^codex-npm-time]: Live query 2026-09-23: `npm view @openai/codex time --json` — `0.155.0: 2026-09-17`, `0.155.1: 2026-09-18`, `0.156.0: 2026-09-22`, `0.156.1: 2026-09-23`; alpha timestamps multiple per day.
[^codex-docs]: OpenAI Codex CLI documentation (fetched as markdown 2026-09-23): install methods and update commands; developers.openai.com/codex/cli 308-redirects to https://learn.chatgpt.com/docs/codex/cli; no channel/prerelease statements on the page.
[^codex-readme]: `openai/codex` README (fetched via GitHub Contents API 2026-09-23): npm, Homebrew, and GitHub-release binary install instructions with the exact per-platform asset names. https://github.com/openai/codex#install
[^codex-updates-rs]: `openai/codex` source, `codex-rs/tui/src/updates.rs` (fetched via GitHub Contents API 2026-09-23): 20-hour recheck interval, `LATEST_RELEASE_URL = "https://api.github.com/repos/openai/codex/releases/latest"`, `HOMEBREW_CASK_API_URL`, install-method-specific channel selection.
[^codex-npm-registry-rs]: `openai/codex` source, `codex-rs/tui/src/npm_registry.rs` (line-level quote verified 2026-09-23): `"npm latest dist-tag points to {latest}, expected GitHub release {version}"` and `"npm package is missing latest dist-tag"` bail paths in `ensure_version_ready`. https://github.com/openai/codex/blob/main/codex-rs/tui/src/npm_registry.rs
[^codex-gh-headers]: Live query 2026-09-23: response headers of `GET /repos/openai/codex/releases/latest` — `Cache-Control: private, max-age=60, s-maxage=60`, `ETag: W/"…"` (weak), `X-RateLimit-Remaining` present.
[^codex-policy]: Factory source, `_codex.py`: `_Codex01551Policy` (production Run Admission policy, `version = "0.155.1"`) binding artifact sha256, OS, architecture, canonical path, and filesystem identity. https://github.com/ricardoiarced/software-factory/blob/main/_codex.py
[^cert-readme]: Factory `certification/README.md`: candidate-manifest schema (`codex_version`, `official_sha256`, `official_source`, OS, architecture) and the exact-tuple evidence rules. https://github.com/ricardoiarced/software-factory/blob/main/certification/README.md
[^issue11]: ricardoiarced/software-factory issue #11, "Define Codex isolation compatibility policy" (closed): embedded immutable allowlist, no mutable remote allowlist, "Admission … should avoid network access", executable re-hash and mid-admission abort, millisecond-scale startup note. https://github.com/ricardoiarced/software-factory/issues/11
[^map97]: ricardoiarced/software-factory issue #97, "Map latest-only certified Codex and Claude Code Runs": newest-stable definition, per-harness independent checks, stop-on-newer-release, stop-on-lookup-failure, no auto-update, no mutable allowlist. https://github.com/ricardoiarced/software-factory/issues/97
[^cc-dist-tags]: Live query 2026-09-23: `npm view @anthropic-ai/claude-code dist-tags --json` → `{ "stable": "2.1.267", "latest": "2.1.280", "next": "2.1.280" }`; `versions --json` (522 entries) contains zero prerelease-suffixed versions. Durable surface: https://registry.npmjs.org/@anthropic-ai%2Fclaude-code
[^cc-npm-unauth]: Live query 2026-09-23: unauthenticated `GET https://registry.npmjs.org/@anthropic-ai%2Fclaude-code/latest` returned the 2.1.280 manifest; response headers `server: cloudflare`, `cf-cache-status: DYNAMIC`.
[^cc-npm-latest]: Live queries 2026-09-23: `npm view @anthropic-ai/claude-code@2.1.280 --json` — `bin: {"claude": "bin/claude.exe"}`, `engines: node >=22`, eight pinned platform `optionalDependencies`; platform package `@anthropic-ai/claude-code-linux-x64` exposes `os: ["linux"]`, `cpu: ["x64"]`, own `dist.integrity`. Durable surface: https://registry.npmjs.org/@anthropic-ai%2Fclaude-code
[^cc-gh]: Live queries 2026-09-23: `GET /repos/anthropics/claude-code/releases/latest` → `v2.1.280`, `prerelease: false`, 10 assets (`claude-{darwin-arm64,darwin-x64,linux-arm64-musl,linux-arm64,linux-x64-musl,linux-x64}.tar.gz`, `claude-win32-{arm64,x64}.zip`, `SHASUMS256.txt`, `SHASUMS256.txt.sig`); asset `digest` values byte-identical to `SHASUMS256.txt`; zero `prerelease: true` across the last 100 releases; `CHANGELOG.md` matches release bodies. Durable surface: https://github.com/anthropics/claude-code/releases
[^cc-pointers]: Live queries 2026-09-23: `GET https://downloads.claude.ai/claude-code-releases/stable` → `2.1.267`; `…/latest` → `2.1.280`; GCS `UploadServer` headers with `x-goog-generation` object generation; observed `stable` pointer `last-modified` equal to the 2.1.280 publish instant. Fallback bucket `storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819/claude-code-releases` returns identical values.
[^cc-install-sh]: `https://claude.ai/install.sh` (fetched 2026-09-23): `DOWNLOAD_BASE_URL="https://downloads.claude.ai/claude-code-releases"`; usage `stable|latest|VERSION`; resolves the `latest` pointer; verifies the binary against `<version>/manifest.json` per-platform checksum; no signature verification step; optional `manifest.zst.json` compressed path checked against the uncompressed checksum.
[^cc-setup-docs]: Claude Code documentation, "Set up Claude Code" (https://code.claude.com/docs/en/setup, fetched 2026-09-23): native auto-update quotes, `autoUpdatesChannel` (`latest` default, `stable` "typically about one week old, skipping releases with major regressions"), `DISABLE_AUTOUPDATER`/`DISABLE_UPDATES`, `claude update`/`claude doctor`/`minimumVersion`, npm per-platform optional-dependency mechanism, and the signed-manifest verification text ("verifying the signature on the manifest transitively verifies every binary it lists"; signatures from `2.1.89` onward; key fingerprint and apt key `31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE`).
[^cc-readme]: `anthropics/claude-code` README (fetched via GitHub Contents API 2026-09-23): "Installation via npm is deprecated" notice and recommended native installer / brew / winget methods; no SHASUMS/GPG verification instructions (grep: zero matches), same for SECURITY.md.

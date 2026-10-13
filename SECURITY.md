# Security policy

Collaboration-Hub is intentionally read-only. The monitor uses only explicitly
configured repositories and HTTPS API hosts; it blocks cross-host redirects and
never executes GitHub text.

## Credentials

- Use one least-privilege read token per private repository where practical.
- In the multi-repository configuration, give every entry an explicit, non-empty
  `token_env`; duplicate environment names are rejected unless the top-level
  `allow_shared_token_env: true` is consciously declared.
- Inject tokens through environment variables or an external credential store.
- Never commit tokens, SSH private keys, `admin.auth`, cookies, device identifiers,
  raw private comments, or server paths containing secrets.
- State and report directories should be `0700`; generated files should be `0600`.

## Reporting

If a secret is exposed, revoke/rotate it first, then report the affected file,
commit, or GitHub location privately to the repository owner. Do not paste the
credential into a public Issue or PR.

The project does not support automatic Issue/PR writes, Project updates, Releases,
Webhooks, or deployment from this prototype. Any future change must be separately
reviewed for scope, least privilege, redaction, idempotency, and a tested stop path.

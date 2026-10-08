# Forward-looking huggingface_hub CI

English | [简体中文](hf-forward-ci.zh-CN.md)

`.github/workflows/hf-forward.yml` runs every day at 03:23 UTC against the **latest**
`huggingface_hub` release. It is separate from the manual regression workflow and runs only
the tests marked `hf_client`, which are the tests that call `huggingface_hub` directly:

```sh
python -m pytest test/kohakuhub -m hf_client --collect-only -q   # list them
```

The job starts the same isolated service stack as the regression workflow. When it fails,
`scripts/ci/hf_forward_triage.py` decides what to report:

1. **Exact repeat.** The failure signature (failing test ids and first message lines) matches
   the hidden marker in an open issue labelled `hf-forward-ci`. The script comments on that
   issue with the run link and version. It does not open a second issue.
2. **Same problem, different wording.** Otherwise the model `claude-haiku-5-5` receives the
   failing tests, a log tail and the open issues. It answers with a duplicate number, which
   must be one of those open issues, or a new title and body. A duplicate gets a comment; a
   new problem gets one new issue.
3. **Without model access**, for example in a fork without secrets, only the exact-repeat rule
   applies and the generic issue text is used. A model error is printed and falls back the same way.

## Secrets

The model is reached through an Anthropic-compatible gateway configured in the repository
secrets `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN`. Their values never appear in logs or
in issues. Rotate them in **Settings → Secrets and variables → Actions**.

## Reusing the report (humans and Claude Code)

Each run uploads `hf-forward-<run id>-<attempt>` with 14 days retention:

| File | Content |
| --- | --- |
| `hf-forward.xml` | JUnit report of the `hf_client` tests |
| `hf-forward.log` | Test output |
| `hf-forward-triage.json` | The decision: `create` or `comment`, the issue number, and the signature |
| `services.log` | Logs of the service containers |

```sh
gh run list -R cheesecave/cheesecave-backend --workflow hf-forward.yml -L 5
gh run download <run-id> -R cheesecave/cheesecave-backend -n hf-forward-<run-id>-1 -D /tmp/hf-forward
```

Run the workflow manually with `dry_run` set to `true` to see the decision without writing
to issues.

## Limits

A green run means only the `hf_client` tests passed with the latest release. It is not a
certification of other clients (transformers, datasets, the `hf` CLI, git/LFS). The model
judges duplicates; it can be wrong, so check the linked issue before acting on it.

# Project
Fleet SRE: an agentic SRE for multi-tenant Google SecOps SOAR, built on a local-first platform with a
mock SecOps that follows Google's published Chronicle API and SOAR log format. Spec: docs/spec.md.

# Hard rules
- Synthetic data only. No real company, client or employer names, systems, code or data.
- No secrets in the repo. Credentials come from the environment or impersonation.
- Every mock resource field must exist in specs/chronicle/*.yaml; cite the doc URL in a comment
  on each mock resource model.
- Anything not in Google's docs is invented: mark it with a `# invented:` comment.
- fleetsre.sre must not import google.*, fleetsre.adapters or fleetsre.mock. It reaches the world
  only through fleetsre.sre.tools.ToolSet.
- Use SimClock in tests. No sleeps.

# Protected paths
Read-only on impl/* branches:
contracts/ fixtures/ specs/ prompts/ tests/acceptance/ docs/ AGENTS.md

# Architecture
Ports and adapters (docs/spec.md §4.2). Local adapters first; GCP adapters in D7 only.

# Commands
make check | make test-live | make test-gcp | make demo

# Deliverable protocol
Work only on the deliverable named in the goal. Tests sessions write tests and stubs only.
Implementation sessions never modify acceptance tests. Stop when the goal's acceptance passes and
print the summary the goal asks for. Never start the next deliverable.

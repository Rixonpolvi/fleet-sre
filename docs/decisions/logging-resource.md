# Decision: Cloud Logging resource type for mock SOAR logs

result: accepted
resource_type: k8s_container
date: 2026-10-29

## Test
Wrote one structured entry from outside GKE with resource type `k8s_container`
(labels: project_id, location, cluster_name, namespace_name=chronicle-soar,
pod_name, container_name=python) and the four connector entry labels.

## Outcome
Entry was queryable within seconds via `resource.labels.namespace_name="chronicle-soar"`.
Resource type and all six resource labels were preserved. All four entry labels were preserved.
No fallback needed.

## Constraint
`resource.labels.project_id` must equal the real GCP project ID. The mock reads it
from configuration (env `PROJECT`); `fleet-sre-demo` appears only inside mock
Chronicle resource names.

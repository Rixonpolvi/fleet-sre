#!/usr/bin/env bash
# D7 lite GCP infra (docs/d7-pins.md §Interfaces 3): the three service
# accounts, the log sink, the topic and the subscription, and their
# bindings. No service account ever gets a key; Rixon's account
# impersonates each one.
#
#   FLEETSRE_GCP_PROJECT=<project> infra/gcp.sh up    # create (idempotent)
#   FLEETSRE_GCP_PROJECT=<project> infra/gcp.sh down  # remove (tolerates gone)
set -euo pipefail

: "${FLEETSRE_GCP_PROJECT:?FLEETSRE_GCP_PROJECT is unset}"
set -x

PROJECT="$FLEETSRE_GCP_PROJECT"
SA_NAMES="mock-soar obs-reader deployer"
TOPIC="fleet-sre-logs"
SUBSCRIPTION="fleet-sre-logs-obs"
SINK="fleet-sre-soar"
SINK_FILTER='resource.type="k8s_container" AND resource.labels.namespace_name="chronicle-soar"'

sa_email() {
    printf '%s@%s.iam.gserviceaccount.com' "$1" "$PROJECT"
}

# A binding whose member is a just-created service account can fail with
# "does not exist" for up to about a minute (d7-pins 3): each
# add-iam-policy-binding retries up to 6 times, 10 s apart.
bind() {
    attempt=1
    while true; do
        if "$@"; then
            return 0
        fi
        if [ "$attempt" -ge 6 ]; then
            echo "binding failed after 6 attempts: $*" >&2
            return 1
        fi
        attempt=$((attempt + 1))
        echo "retry $attempt/6 in 10 s: $*"
        sleep 10
    done
}

cmd_up() {
    gcloud services enable logging.googleapis.com pubsub.googleapis.com \
        iam.googleapis.com iamcredentials.googleapis.com \
        --project="$PROJECT"

    for name in $SA_NAMES; do
        if gcloud iam service-accounts describe "$(sa_email "$name")" \
            --project="$PROJECT" >/dev/null 2>&1; then
            echo "service account exists: $(sa_email "$name")"
        else
            gcloud iam service-accounts create "$name" --project="$PROJECT" \
                --display-name="Fleet SRE D7: $name"
        fi
    done

    if gcloud pubsub topics describe "$TOPIC" \
        --project="$PROJECT" >/dev/null 2>&1; then
        echo "topic exists: $TOPIC"
    else
        gcloud pubsub topics create "$TOPIC" --project="$PROJECT"
    fi

    if gcloud pubsub subscriptions describe "$SUBSCRIPTION" \
        --project="$PROJECT" >/dev/null 2>&1; then
        echo "subscription exists: $SUBSCRIPTION"
    else
        gcloud pubsub subscriptions create "$SUBSCRIPTION" \
            --topic="$TOPIC" --project="$PROJECT"
    fi

    if gcloud logging sinks describe "$SINK" \
        --project="$PROJECT" >/dev/null 2>&1; then
        echo "sink exists: $SINK"
    else
        gcloud logging sinks create "$SINK" \
            "pubsub.googleapis.com/projects/$PROJECT/topics/$TOPIC" \
            --log-filter="$SINK_FILTER" --project="$PROJECT"
    fi

    bind gcloud projects add-iam-policy-binding "$PROJECT" \
        --member="serviceAccount:$(sa_email mock-soar)" \
        --role=roles/logging.logWriter
    bind gcloud projects add-iam-policy-binding "$PROJECT" \
        --member="serviceAccount:$(sa_email obs-reader)" \
        --role=roles/logging.viewer
    bind gcloud pubsub subscriptions add-iam-policy-binding "$SUBSCRIPTION" \
        --project="$PROJECT" \
        --member="serviceAccount:$(sa_email obs-reader)" \
        --role=roles/pubsub.subscriber

    writer_identity="$(gcloud logging sinks describe "$SINK" \
        --project="$PROJECT" --format='value(writerIdentity)')"
    echo "sink writer identity: $writer_identity"
    bind gcloud pubsub topics add-iam-policy-binding "$TOPIC" \
        --project="$PROJECT" --member="$writer_identity" \
        --role=roles/pubsub.publisher

    me="$(gcloud config get-value account)"
    echo "token-creator grantee: $me"
    for name in $SA_NAMES; do
        bind gcloud iam service-accounts add-iam-policy-binding \
            "$(sa_email "$name")" --project="$PROJECT" --member="user:$me" \
            --role=roles/iam.serviceAccountTokenCreator
    done

    echo "IAM changes can take several minutes to apply. Wait ~5 minutes before make test-gcp."
}

cmd_down() {
    for name in $SA_NAMES; do
        gcloud projects remove-iam-policy-binding "$PROJECT" \
            --member="serviceAccount:$(sa_email "$name")" \
            --role=roles/logging.logWriter >/dev/null 2>&1 || true
        gcloud projects remove-iam-policy-binding "$PROJECT" \
            --member="serviceAccount:$(sa_email "$name")" \
            --role=roles/logging.viewer >/dev/null 2>&1 || true
    done
    gcloud logging sinks delete "$SINK" --project="$PROJECT" --quiet || true
    gcloud pubsub subscriptions delete "$SUBSCRIPTION" \
        --project="$PROJECT" --quiet || true
    gcloud pubsub topics delete "$TOPIC" --project="$PROJECT" --quiet || true
    for name in $SA_NAMES; do
        gcloud iam service-accounts delete "$(sa_email "$name")" \
            --project="$PROJECT" --quiet || true
    done
}

case "${1:-}" in
    up) cmd_up ;;
    down) cmd_down ;;
    *)
        echo "usage: $0 up|down" >&2
        exit 2
        ;;
esac

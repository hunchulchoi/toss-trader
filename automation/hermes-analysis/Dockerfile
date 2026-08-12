ARG HERMES_BASE_IMAGE=hermes-agent-honcho:local
FROM ${HERMES_BASE_IMAGE}

RUN install -d -m 0555 /opt/hermes-analysis
COPY --chmod=0444 config.yaml /opt/hermes-analysis/config.yaml
COPY --chmod=0555 start.sh /opt/hermes-analysis/start.sh

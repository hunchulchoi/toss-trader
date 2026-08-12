#!/bin/sh
set -eu

umask 077
temporary_config="/opt/data/config.yaml.tmp"
cp /opt/hermes-analysis/config.yaml "$temporary_config"
mv "$temporary_config" /opt/data/config.yaml

exec /opt/hermes/.venv/bin/hermes gateway run

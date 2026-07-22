#!/bin/bash

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

protoc --python_out=../lib apex_manifest.proto ota_metadata.proto

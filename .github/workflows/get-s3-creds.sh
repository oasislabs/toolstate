#!/bin/bash

set -euo pipefail

vault_token=$(curl -sX POST "$VAULT_ADDR/v1/auth/approle/login" \
    -d "{\"role_id\": \"$VAULT_ROLE_ID \", \"secret_id\": \"$VAULT_SECRET_ID \" }" \
  | jq -r '.auth.client_token')

curl -sX GET "$VAULT_ADDR/v1/aws/sts/production-toolstate-s3-bucket" \
  -H "x-vault-token: $vault_token" \
  | jq -j '.data | [(.access_key, .secret_key, .security_token)] | @tsv'

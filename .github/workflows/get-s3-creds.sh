#!/bin/bash

get_creds() {
  vault_token=$(curl -sX POST "$VAULT_ADDR/v1/auth/approle/login" \
    -d "{\"role_id\": \"$VAULT_ROLE_ID \", \"secret_id\": \"$VAULT_SECRET_ID \" }" \
    | jq -r '.auth.client_token')

  curl -sX GET "$VAULT_ADDR/v1/aws/sts/production-toolstate-s3-bucket" \
    -H "x-vault-token: $vault_token" \
    | jq -j '.data | [(.access_key, .secret_key, .security_token)] | @tsv'
}

set -uo pipefail
if [ "${BASH_SOURCE[0]:-${(%):-}}" != "$0" ]; then
  # export creds when sourced
  read AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN <<<$(get_creds)
  export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN
  set +uo pipefail
else
  set -e
  # write creds to stdout when invoked
  get_creds
fi

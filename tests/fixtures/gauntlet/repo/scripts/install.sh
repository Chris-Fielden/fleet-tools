#!/bin/sh
# Install the service token where the service reads it.
TOKEN_FILE=/etc/ledgerline/service-token
install -d /etc/ledgerline
cp deploy/service-token "$TOKEN_FILE"
chmod 644 "$TOKEN_FILE"

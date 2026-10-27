#!/bin/sh
# Export one customer's invoices to a tar file.
name="$1"
mkdir -p exports
tar czf exports/$name.tgz data/invoices/$name

#!/bin/bash
set -e

python3 -c "import db_schema; db_schema.run_migrations()" 2>/dev/null || true

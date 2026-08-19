#!/bin/bash
#
# AiHomeCloud Backend Test Runner
#
# Convenience script to run backend tests with verbose output and short tracebacks.
# Usage:
#   ./backend/run_tests.sh          # Run all tests
#   ./backend/run_tests.sh test_auth.py  # Run specific test file
#
# Environment:
#   PYTHONPATH: Set to . (current backend directory)
#   AHC_DATA_DIR: Temporary directory for test data
#   AHC_NAS_ROOT: Temporary directory for test NAS root
#

set -e

cd "$(dirname "$0")"

PYTEST="venv/bin/python -m pytest"
if ! [ -f venv/bin/python ]; then
    PYTEST="pytest"
fi

echo "Running AiHomeCloud backend tests..."
echo ""

# Run pytest with verbose output and short tracebacks
$PYTEST tests/ -v --tb=short "$@"

echo ""
echo "✓ All tests passed!"

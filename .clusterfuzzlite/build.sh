#!/bin/bash -eu
# Builds every fuzz/fuzz_*.py target. The core dependencies come from a hash-locked file.
pip3 install --require-hashes -r requirements/fuzz.txt
pip3 install --no-deps --no-build-isolation .
for fuzzer in fuzz/fuzz_*.py; do
  compile_python_fuzzer "$fuzzer"
done

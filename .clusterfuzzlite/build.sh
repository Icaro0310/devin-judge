#!/bin/bash -eu
pip3 install atheris pyinstaller
pip3 install -e .
compile_python_fuzzer fuzz/fuzz_primitives.py

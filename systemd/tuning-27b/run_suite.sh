#!/bin/bash
# Прогон полного набора для одной конфигурации. Использование: run_suite.sh <tag>
# Прогрев, потом короткие случаи x3, длинные x2 (1-й fresh, 2-й с prefix cache).
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$DIR/bench.py" --probe
echo "=== warmup ==="
python3 "$DIR/bench.py" --tag "${1:-run}" --cases short_code --repeats 1 --max-tokens-short 16
echo "=== suite ==="
python3 "$DIR/bench.py" --tag "${1:-run}" --cases short_code,short_tool --repeats 3 --max-tokens-short 256
python3 "$DIR/bench.py" --tag "${1:-run}" --cases long32 --repeats 2 --max-tokens-long 384
python3 "$DIR/bench.py" --tag "${1:-run}" --cases long60 --repeats 2 --max-tokens-long 384
echo "=== done ==="

#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

DB_PATH="${CHAOS_DB:-runtime/chaos_state.sqlite3}"
SAMPLE_DIR="${CHAOS_SAMPLE_DIR:-sample_data}"

mkdir -p "$SAMPLE_DIR"
python generate_samples.py --output-dir "$SAMPLE_DIR"

python main.py --sample-dir "$SAMPLE_DIR" --db "$DB_PATH" load --reset --clock 1000
python main.py --sample-dir "$SAMPLE_DIR" --db "$DB_PATH" run --clock 1000
python main.py --sample-dir "$SAMPLE_DIR" --db "$DB_PATH" observe --clock 1400
python main.py --sample-dir "$SAMPLE_DIR" --db "$DB_PATH" run --clock 1500
python main.py --sample-dir "$SAMPLE_DIR" --db "$DB_PATH" observe --clock 1595
python main.py --sample-dir "$SAMPLE_DIR" --db "$DB_PATH" run --clock 1600
python main.py --sample-dir "$SAMPLE_DIR" --db "$DB_PATH" observe --clock 1650
python main.py --sample-dir "$SAMPLE_DIR" --db "$DB_PATH" resume --experiment EXP-002 --clock 1700
python main.py --sample-dir "$SAMPLE_DIR" --db "$DB_PATH" observe --clock 1800
python main.py --sample-dir "$SAMPLE_DIR" --db "$DB_PATH" run --clock 1800
python main.py --sample-dir "$SAMPLE_DIR" --db "$DB_PATH" status
python main.py --sample-dir "$SAMPLE_DIR" --db "$DB_PATH" verify --strict

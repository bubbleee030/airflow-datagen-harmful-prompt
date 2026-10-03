import sys
from pathlib import Path

# Make `harmful_prompt` importable the same way the Airflow worker will.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dags" / "scripts"))

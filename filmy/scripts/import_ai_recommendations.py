from __future__ import annotations

import argparse
import json
from pathlib import Path

from filmy.ai_recommendations import import_ai_recommendations_file


def main() -> None:
    parser = argparse.ArgumentParser(description="Import stable AI recommendation JSON into FILMY.")
    parser.add_argument("json_file", type=Path, help="Path to one filmy_output recommendation JSON file.")
    arguments = parser.parse_args()
    result = import_ai_recommendations_file(arguments.json_file)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

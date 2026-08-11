import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from adapters.chirp import main  # noqa: E402

if __name__ == "__main__":
    main(model="retell-gpt4.1-flash2.5")

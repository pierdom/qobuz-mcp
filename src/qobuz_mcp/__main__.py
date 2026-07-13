from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent.parent / ".env")

from .server import main

if __name__ == "__main__":
    main()

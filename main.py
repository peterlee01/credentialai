"""CLI entry point for Credential Verify.

Usage:
    python main.py "Jane" "Doe" "Colorado" "psychologist"

Requires ANTHROPIC_API_KEY set in the environment (or a .env file — see .env.example).
"""

import sys

from dotenv import load_dotenv

from src.coordinator import run_verification

load_dotenv()


def main() -> None:
    if len(sys.argv) != 5:
        print('Usage: python main.py "First" "Last" "State" "profession"')
        print('  profession is one of: psychologist, counselor, "social worker"')
        sys.exit(1)

    first_name, last_name, state, profession = sys.argv[1:5]

    print(f"Verifying {first_name} {last_name} — {profession} in {state}...\n")
    verdict_text = run_verification(first_name, last_name, state, profession)
    print("\n--- Verdict ---")
    print(verdict_text)


if __name__ == "__main__":
    main()

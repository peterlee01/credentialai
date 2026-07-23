"""Interactive human review queue — resolve real NEEDS REVIEW verifications.

Usage:
    python review.py
"""

from src.review_queue import get_unresolved_needs_review, resolve


def main() -> None:
    queue = get_unresolved_needs_review()
    if not queue:
        print("No unresolved NEEDS REVIEW cases.")
        return

    print(f"{len(queue)} unresolved NEEDS REVIEW case(s):\n")
    for i, entry in enumerate(queue):
        req = entry["request"]
        print(f"  [{i}] {entry['timestamp']} — {req['first_name']} {req['last_name']}, "
              f"{req['profession']} in {req['state']}")

    choice = input("\nEnter number to review (or blank to quit): ").strip()
    if not choice:
        return
    entry = queue[int(choice)]

    print("\n--- Full verdict ---")
    print(entry["verdict_text"])

    decision = ""
    while decision not in ("approve", "deny"):
        decision = input("\nDecision (approve/deny): ").strip().lower()

    rationale = ""
    while not rationale:
        rationale = input("Rationale (required): ").strip()

    resolve(entry, decision, rationale)
    print(f"\nLogged: {decision} — \"{rationale}\"")


if __name__ == "__main__":
    main()

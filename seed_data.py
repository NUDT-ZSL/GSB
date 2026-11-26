"""Root wrapper for the fixed-seed generator."""

from entity_link_lab.seed import SEED, generate_all


if __name__ == "__main__":
    manifest = generate_all("data")
    print(
        f"seed={SEED} mentions={manifest['mention_count']} "
        f"candidates={manifest['seed_candidate_count']}"
    )

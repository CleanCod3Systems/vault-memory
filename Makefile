PY ?= python3

.PHONY: test eval example validate release eval-plugin eval-plugin-write eval-plugin-quick

test:
	$(PY) -B -m unittest discover tests

eval:
	$(PY) -B scripts/cli.py --vault tests/example_vault eval --tsv tests/eval.tsv --minimum 20
	$(PY) -B scripts/cli.py --vault tests/example_vault eval --tsv tests/eval.tsv --hook --minimum 20

example:
	$(PY) -B tests/make_example_vault.py

validate:
	claude plugin validate .

# Hosted catalog (what Vercel serves): marketplace.json + zip with a pinned sha256.
release:
	$(PY) -B scripts/build_release.py --out public --base-url $${BASE_URL:-http://localhost:8000}

# claude plugin eval suite (with/without the plugin). It costs money: run before each release.
# `read` runs anywhere; `write` needs Bash in the sandbox.
eval-plugin:
	claude plugin eval . --tag read --scaffold --trust-plugin --threshold 0.8 \
		--max-cost-usd 5 --no-publish --json evals/results/latest.json

eval-plugin-write:
	claude plugin eval . --tag write --scaffold --allow-tools Bash --trust-plugin --threshold 0.8 \
		--max-cost-usd 5 --no-publish

eval-plugin-quick:
	claude plugin eval . --tag read --scaffold --trust-plugin --runs 1 --ablation none \
		--max-cost-usd 1 --no-publish

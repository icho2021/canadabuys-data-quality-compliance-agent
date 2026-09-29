#!/usr/bin/env python3
"""Retrieve the official text governing a finding, then verify the quotation.

Runs per rule, never per row, and never decides whether a row is a finding:
that is deterministic SQL upstream. Hybrid retrieval (exact field name plus
cosine similarity), a local model quotes the governing passage, then the quote
is checked against the retrieved text and rejected when absent.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import requests

OLLAMA = "http://localhost:11434/api"
EMBED_MODEL = "nomic-embed-text"
CHAT_MODEL = "llama3"
TOP_K = 4

# Which source field each rule is about. Declared, not inferred, so retrieval
# quality can be measured against it.
RULE_FIELDS: dict[str, str] = {
    "invalid_award_date": "contractAwardDate-dateAttributionContrat",
    "missing_award_date": "contractAwardDate-dateAttributionContrat",
    "legacy_cutover_mismatch": "contractAwardDate-dateAttributionContrat",
    "current_cutover_mismatch": "contractAwardDate-dateAttributionContrat",
    "invalid_contract_amount": "contractAmount-montantContrat",
    "negative_amount_unknown_activity": "contractAmount-montantContrat",
    "negative_amount_on_known_amendment": "contractAmount-montantContrat",
    "zero_contract_amount": "contractAmount-montantContrat",
    "contract_amount_exceeds_total_value": "totalContractValue-valeurTotaleContrat",
    "negative_total_contract_value": "totalContractValue-valeurTotaleContrat",
    "unexpected_currency": "contractCurrency-contratMonnaie",
    "unknown_amendment_type": "amendmentType-typeModification-eng",
    "amendment_date_before_award_date": "amendmentDate-dateModification",
    "future_amendment_date": "amendmentDate-dateModification",
    "end_date_before_start_date": "contractEndDate-dateFinContrat",
    "end_date_before_award_date": "contractEndDate-dateFinContrat",
    "unusually_distant_end_date": "contractEndDate-dateFinContrat",
    "non_canonical_trade_agreements": "tradeAgreements-accordsCommerciaux-eng",
    "non_canonical_supplier_country": "supplierAddressCountry-fournisseurAdressePays-eng",
    "postal_code_format_mismatch_canada": "supplierAddressPostalCode-fournisseurAdresseCodePostal",
    "missing_standardized_supplier_name": "supplierStandardizedName-nomNormaliseFournisseur-eng",
}

ASSESS_PROMPT = """You are helping a data steward triage a data quality finding \
in the Government of Canada CanadaBuys contract history dataset.

RULE: {rule_id}
WHAT IT DETECTED: {description}
ROWS AFFECTED: {affected_rows}
EXAMPLE OBSERVED VALUE: {sample}

OFFICIAL DOCUMENTATION RETRIEVED:
{passages}

Use ONLY the documentation above. Do not use outside knowledge. Do not describe \
what a data type "usually" means.

1. QUOTE: Copy, word for word, the single sentence or pattern from the \
documentation above that most directly governs this field. Copy it exactly, \
changing nothing. If nothing above governs this field, write exactly NONE.
2. VERDICT: Exactly one of these three words and nothing else: \
EXPLAINED if the quote accounts for the observed pattern, CONTRADICTED if the \
observed value conflicts with the quote, NOT_ADDRESSED if the quote does not \
settle the question.
3. QUESTION: One specific question for the data steward, or "none needed".

Answer in exactly three lines, each starting with the label."""


def embed(text: str) -> list[float]:
    r = requests.post(f"{OLLAMA}/embeddings", json={"model": EMBED_MODEL, "prompt": text}, timeout=120)
    r.raise_for_status()
    return r.json()["embedding"]


def chat(prompt: str) -> str:
    r = requests.post(
        f"{OLLAMA}/generate",
        json={"model": CHAT_MODEL, "prompt": prompt, "stream": False,
              "options": {"temperature": 0}},
        timeout=600,
    )
    r.raise_for_status()
    return r.json()["response"].strip()


def retrieve(connection, query: str, field: str, mode: str) -> list[dict]:
    """mode: exact | embedding | hybrid."""
    results: list[dict] = []
    if mode in ("exact", "hybrid") and field:
        results = connection.execute(
            "SELECT chunk_id, source_document, section, text, 1.0 AS score, "
            "'exact' AS retrieved_by FROM kb WHERE list_contains(field_names, ?)",
            [field],
        ).df().to_dict("records")
    if mode == "exact":
        return results[:TOP_K]
    seen = {r["chunk_id"] for r in results}
    vector = embed(query)
    semantic = connection.execute(
        "SELECT chunk_id, source_document, section, text, "
        "array_cosine_similarity(embedding::FLOAT[768], ?::FLOAT[768]) AS score, "
        "'embedding' AS retrieved_by FROM kb ORDER BY score DESC LIMIT ?",
        [vector, TOP_K * 2],
    ).df().to_dict("records")
    for row in semantic:
        if row["chunk_id"] not in seen and len(results) < TOP_K:
            results.append(row)
    return results[:TOP_K]


# Natural-language questions a steward would actually ask, paired with the
# field whose official entry answers them. Retrieval must be measured against
# both query shapes: an identifier query is the case where lexical match wins
# by construction, so measuring only that overstates the exact matcher.
NL_QUERIES: tuple[tuple[str, str], ...] = (
    ("What is the total cumulative value of a contract including all amendments?",
     "totalContractValue-valeurTotaleContrat"),
    ("Is a contract allowed to have a negative monetary value?",
     "contractAmount-montantContrat"),
    ("What tells me whether an entry is the original contract or a later change?",
     "amendmentType-typeModification-eng"),
    ("When does an awarded contract stop being valid?",
     "contractEndDate-dateFinContrat"),
    ("Which currency do the reported amounts use?",
     "contractCurrency-contratMonnaie"),
    ("What date is recorded when a contract is changed after award?",
     "amendmentDate-dateModification"),
    ("How is the kind of purchasing instrument recorded, such as a standing offer?",
     "instrumentType-typeInstrument-eng"),
    ("What date is the contract legally in force from?",
     "contractStartDate-contratDateDebut"),
)


LABELS = ("QUOTE", "VERDICT", "QUESTION")
VALID_VERDICTS = ("EXPLAINED", "CONTRADICTED", "NOT_ADDRESSED")


def normalise(text: str) -> str:
    """Collapse whitespace and case so a quote can be checked against source."""
    return " ".join(text.lower().split())


def verify_grounding(quote: str, passages: list[dict]) -> tuple[bool, str]:
    """Is the model's quote actually present in the text it was given?

    This is the deterministic guardrail on a non-deterministic component. An
    earlier version of this agent asserted that `schema.org/Number` "allows only
    positive numbers or zero", a constraint that appears nowhere in the passage
    it cited and that the very next line of that passage contradicts. A quote
    that cannot be located in the retrieved text is rejected rather than shown
    to a reviewer as if it were sourced.
    """
    cleaned = normalise(quote.strip().strip('"').strip("'"))
    if not cleaned or cleaned == "none":
        return False, "no_quote"
    for passage in passages:
        if cleaned in normalise(passage["text"]):
            return True, passage["chunk_id"]
    return False, "not_found_in_retrieved_text"
# The model prefixes labels with list numbering ("1. EXPLAINED:") and continues
# the answer on following lines, so each section runs until the next label.
ASSESSMENT_RE = re.compile(
    rf"^[^\w]*\d*[.)]?\s*({'|'.join(LABELS)})\s*:\s*(.*?)"
    rf"(?=^[^\w]*\d*[.)]?\s*(?:{'|'.join(LABELS)})\s*:|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def parse_assessment(answer: str) -> dict[str, str]:
    parsed = {label: "" for label in LABELS}
    for match in ASSESSMENT_RE.finditer(answer):
        parsed[match.group(1).upper()] = " ".join(match.group(2).split())
    return parsed


def _hit(results: list[dict], field: str) -> bool:
    return any(field in (r["section"] or "") or field in r["text"] for r in results)


def evaluate(connection) -> None:
    """Measure retrieval against both query shapes, not just the flattering one."""
    identifier_queries = [
        (f"What does the official documentation say about the field {field}?", field)
        for field in RULE_FIELDS.values()
    ]
    suites = (
        ("identifier query", identifier_queries, True),
        ("natural language", list(NL_QUERIES), False),
    )
    print(f"{'query shape':<20}{'mode':<12}{'hit@' + str(TOP_K):>8}{'of':>5}")
    for suite_name, queries, has_field in suites:
        for mode in ("exact", "embedding", "hybrid"):
            hits = 0
            for query, field in queries:
                # A natural-language question does not hand us the field name,
                # so the exact matcher has nothing to key on. Modelling that
                # honestly is the whole point of the second suite.
                known_field = field if has_field else ""
                hits += _hit(retrieve(connection, query, known_field, mode), field)
            print(f"{suite_name:<20}{mode:<12}{hits:>8}{len(queries):>5}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--knowledge-base", type=Path, required=True)
    parser.add_argument("--derived-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--mode", default="hybrid", choices=["exact", "embedding", "hybrid"])
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--rule", help="Assess a single rule instead of all")
    args = parser.parse_args()

    connection = duckdb.connect()
    connection.execute(
        f"CREATE VIEW kb AS SELECT * FROM read_parquet('{args.knowledge_base}')"
    )
    if args.evaluate:
        evaluate(connection)
        return

    summary = connection.execute(
        f"SELECT rule_id, rule_class, authority, sum(finding_count)::BIGINT AS n "
        f"FROM read_parquet('{args.derived_dir / 'gold_quality_summary.parquet'}') "
        f"GROUP BY ALL HAVING sum(finding_count) > 0 ORDER BY n DESC"
    ).df().to_dict("records")
    if args.rule:
        summary = [r for r in summary if r["rule_id"] == args.rule]
    findings = args.derived_dir / "quality_findings.parquet"
    observations = args.derived_dir / "quality_observations.parquet"
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    drafts = []
    for row in summary:
        rule_id = row["rule_id"]
        field = RULE_FIELDS.get(rule_id, "")
        source = observations if row["rule_class"] == "observe" else findings
        sample = connection.execute(
            f"SELECT observed_value FROM read_parquet('{source}') WHERE rule_id = ? LIMIT 1",
            [rule_id],
        ).fetchone()
        passages = retrieve(
            connection,
            f"{rule_id.replace('_', ' ')} for field {field}: what does the official "
            f"CanadaBuys documentation define and permit?",
            field, args.mode,
        )
        rendered = "\n\n".join(
            f"[{p['chunk_id']} | {p['source_document']} | {p['section']}]\n{p['text'][:900]}"
            for p in passages
        ) or "(nothing retrieved)"
        answer = chat(ASSESS_PROMPT.format(
            rule_id=rule_id, description=row["authority"], affected_rows=row["n"],
            sample=(sample[0] if sample else "")[:200] or "(blank)", passages=rendered,
        ))
        parsed = parse_assessment(answer)
        grounded, located_in = verify_grounding(parsed["QUOTE"], passages)
        verdict = next(
            (v for v in VALID_VERDICTS if v in parsed["VERDICT"].upper()), ""
        )
        # Acceptance depends on grounding alone, deliberately. The verdict is
        # recorded but is NOT a gate, because it was measured and found
        # degenerate: with this prompt llama3 answered CONTRADICTED for 16 of 16
        # rules, including observe-class rules the official documentation
        # explicitly permits. An earlier prompt answered "Partially" for 16 of
        # 16. Two prompt designs, two constant outputs. The deliverable of this
        # layer is a verified verbatim citation of the governing official text;
        # the model's opinion about that text is advisory only.
        accepted = grounded
        drafts.append({
            "generated_at_utc": generated_at,
            "retrieval_mode": args.mode,
            "chat_model": CHAT_MODEL,
            "embed_model": EMBED_MODEL,
            "rule_id": rule_id,
            "rule_class": row["rule_class"],
            "affected_rows": int(row["n"]),
            "target_field": field,
            "governing_quote": parsed["QUOTE"][:600],
            "quote_grounded": grounded,
            "quote_located_in": located_in,
            "verdict": verdict,
            "verdict_raw": parsed["VERDICT"][:200],
            "question_for_steward": parsed["QUESTION"],
            "citations": [p["chunk_id"] for p in passages],
            "citation_sources": sorted({p["source_document"] for p in passages}),
            "assessment_accepted": accepted,
            "status": "pending" if accepted else "rejected_ungrounded",
            "reviewed_by": "",
            "review_note": "",
        })
        print(f"  [{'OK ' if accepted else 'BAD'}] {rule_id:<40} "
              f"{verdict or '(no verdict)':<14} "
              f"{'grounded in ' + located_in if grounded else located_in}")

    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        out = args.output_dir / "agent_assessments.json"
        out.write_text(json.dumps(drafts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        accepted = sum(1 for d in drafts if d["assessment_accepted"])
        print(f"\n{len(drafts)} assessments, {accepted} accepted, "
              f"{len(drafts) - accepted} rejected as ungrounded -> {out}")
        print("Every assessment is a draft with status 'pending'. "
              "apply_remediation.py still refuses to act without human approval "
              "and a deterministic transform.")
    connection.close()


if __name__ == "__main__":
    main()

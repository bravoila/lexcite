"""Offline retrieval checks or explicitly opted-in live model evaluation on a DB snapshot."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import db, ingest
from app.research_agent import runner as engine, retrieval
from app.models import LawInput


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--questions", default="evals/questions.json")
    p.add_argument("--output", default="evals/results.json")
    p.add_argument("--live", action="store_true", help="Calls the configured model and may incur API costs")
    p.add_argument("--data", default="data", help="Source data directory for --live only")
    args = p.parse_args()
    questions = json.loads(Path(args.questions).read_text())
    source_data = Path(args.data).resolve()
    with tempfile.TemporaryDirectory() as temp:
        if args.live:
            if not (source_data / "lexcite.sqlite3").exists():
                raise SystemExit("--live requires an initialized source database")
            with sqlite3.connect(source_data / "lexcite.sqlite3") as a, sqlite3.connect(Path(temp)/"lexcite.sqlite3") as b:
                a.backup(b)
            if (source_data / "secret.key").exists():
                shutil.copy2(source_data / "secret.key", Path(temp)/"secret.key")
        else:
            for k in ("MODEL_PROVIDER", "MODEL_BASE_URL", "MODEL_NAME", "MODEL_API_KEY"):
                os.environ.pop(k, None)
        os.environ["LEXCITE_DATA"] = temp
        db.init()
        if not args.live:
            # Evaluation-only fixture. Not an automatic legal certification in the user app.
            value = LawInput.model_validate_json((db.ROOT/"examples/civil_excerpt.json").read_text())
            lid = ingest.import_law(value)["id"]
            with db.connect() as c:
                c.execute("UPDATE laws SET reviewed_at=? WHERE id=?", (db.now(), lid))
        results = []
        for item in questions:
            rows, warnings = retrieval.search([item["question"]], item["date"], item["jurisdiction"])
            actual = [r["number"] for r in rows[:10]]
            expected = item["expected_articles"]
            hit = bool(set(expected)&set(actual)) if expected else None
            record = {"id": item["id"], "retrieved_articles": actual, "hit_at_10": hit,
                      "empty_expected_pass": not rows if item.get("must_have_no_evidence") else None,
                      "warnings": warnings, "human_legal_correctness": None, "human_citation_entailment": None}
            if args.live:
                cid = db.uid()
                with db.connect() as c:
                    c.execute("INSERT INTO cases VALUES (?,?,?,?,?)", (cid,"Evaluation",item["date"],item["jurisdiction"],db.now()))
                record["answer"] = engine.answer(cid, item["question"])
            results.append(record)
        graded = [r for r in results if r["hit_at_10"] is not None]
        report = {"mode":"live" if args.live else "offline_retrieval_fixture", "created":db.now(),
                  "note":"Small smoke fixture, not an independent legal benchmark. Human review fields intentionally unset.",
                  "hit_at_10":sum(r["hit_at_10"] for r in graded)/len(graded) if graded else None, "cases":results}
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({"output":str(out),"mode":report["mode"],"questions":len(results),"hit_at_10":report["hit_at_10"]}))


if __name__ == "__main__":
    main()

"""Deterministic citation integrity; not proof of legal entailment."""
import re
from ..ingest import normalize, article_key
from .retrieval import article_bindings, law_key


def validate_claim(claim, evidence, law_names=()):
    """Deterministic integrity checks; these do NOT prove legal entailment.

    law_names: catalogue of in-library law names; used to bind bare-name
    article mentions (「民法典第五百七十七条」) to the cited evidence.
    """
    refs = {r["id"]: r for r in evidence}
    legal_numbers = set()
    legal_names = set()
    cited = {}
    for e in claim.evidence:
        row = refs.get(e.article_id)
        if not row:
            return False, "引用ID不在本轮证据中"
        quote = normalize(e.quote)
        if len(quote) < 8:
            return False, "引用须包含至少8个非空白字符"
        if quote not in normalize(row["body"]):
            return False, "引用原文与法条不一致"
        legal_numbers.add(article_key(row["number"]))
        for name in (row.get("name"), row.get("canonical_name"), row.get("display_name")):
            if not name:
                continue
            legal_names.add(name)
            legal_names.add(name.removeprefix("中华人民共和国"))
            cited.setdefault(law_key(name), set()).add(article_key(row["number"]))
    if re.search(r"https?://|\[[0-9]+\]", claim.text):
        return False, "模型自行生成链接或编号"
    try:
        bindings, _ = article_bindings(claim.text, evidence, law_names)
    except ValueError:
        return False, "结论含无效条号，法规条号及附加条号必须大于零"
    for number, name in bindings:
        if number not in legal_numbers:
            return False, "结论含未引用的条号"
        if name and number not in cited.get(law_key(name), ()):
            return False, "结论中「"+name+"」与条号不匹配引用证据"
    for name in re.findall(r"《([^》]+)》", claim.text):
        if legal_names and law_key(name) not in {law_key(n) for n in legal_names}:
            return False, "结论含未引用的规范名称"
    return True, "原文匹配"

"""Deterministic citation integrity; not proof of legal entailment."""
import re
from ..ingest import normalize, NUM, article_key
from .retrieval import law_key


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
    for number in re.findall(r"第"+NUM+r"条(?:之"+NUM+r")?", claim.text):
        if article_key(number) not in legal_numbers:
            return False, "结论含未引用的条号"
    for name in re.findall(r"《([^》]+)》", claim.text):
        if legal_names and law_key(name) not in {law_key(n) for n in legal_names}:
            return False, "结论含未引用的规范名称"
    # Bare-name binding: 「民法典第五百七十七条」must be cited from 民法典,
    # not from another law that happens to have an article with that number.
    text = law_key(claim.text)
    names = {k for k in [law_key(n) for n in law_names] + list(cited) if len(k) >= 2}
    for name in names:
        for match in re.finditer(re.escape(name) + r"第" + NUM + r"条(?:之" + NUM + r")?", text):
            number = article_key(match.group(0)[len(name):])
            if number in cited.get(name, ()):
                continue
            return False, "结论中「"+name+"」与条号不匹配引用证据"
    return True, "原文匹配"

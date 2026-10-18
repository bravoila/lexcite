"""Deterministic citation integrity; not proof of legal entailment."""
import re
from ..ingest import normalize, NUM


def validate_claim(claim, evidence):
    """Deterministic integrity checks; these do NOT prove legal entailment."""
    refs = {r["id"]: r for r in evidence}
    legal_numbers = set()
    legal_names = set()
    for e in claim.evidence:
        row = refs.get(e.article_id)
        if not row:
            return False, "引用ID不在本轮证据中"
        quote = normalize(e.quote)
        if len(quote) < 8:
            return False, "引用须包含至少8个非空白字符"
        if quote not in normalize(row["body"]):
            return False, "引用原文与法条不一致"
        legal_numbers.add(row["number"])
        if row.get("name"):
            legal_names.add(row["name"])
            legal_names.add(row["name"].removeprefix("中华人民共和国"))
    if re.search(r"https?://|\[[0-9]+\]", claim.text):
        return False, "模型自行生成链接或编号"
    for number in re.findall(r"第"+NUM+r"条(?:之[一二三四五六七八九十]+)?", claim.text):
        if number not in legal_numbers:
            return False, "结论含未引用的条号"
    for name in re.findall(r"《([^》]+)》", claim.text):
        if legal_names and name not in legal_names:
            return False, "结论含未引用的规范名称"
    return True, "原文匹配"

"""身份线索比对与确定性候选聚类。

比对只依赖记录中的姓名、证件尾号、场次三类线索，输出稳定分数与理由；
任何被人工驳回或经拆分解除的配对都不会再次成案（稳定重跑）。
"""
from __future__ import annotations

import unicodedata
from difflib import SequenceMatcher
from typing import Iterable, Sequence

from .models import ClueScore, Proposal

# 权重：姓名 0.4，证件尾号 0.4，场次 0.2
W_NAME = 0.4
W_ID = 0.4
W_SESSION = 0.2

# 总分成案阈值，且至少有一项强标识（姓名或尾号）完全一致
PROPOSE_THRESHOLD = 0.6
FUZZY_NAME_MIN = 0.6
TAIL_OVERLAP_MIN_LEN = 2


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").strip().lower()
    return "".join(text.split())


def normalize_tail(value: str) -> str:
    chars = []
    for ch in unicodedata.normalize("NFKC", value or ""):
        if ch.isalnum():
            chars.append(ch.upper())
    return "".join(chars)


def normalize_session(value: str) -> str:
    return "".join((value or "").split()).upper()


def name_score(a: str, b: str) -> tuple[float, str | None]:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0, None
    if na == nb:
        return 1.0, "姓名一致"
    ratio = SequenceMatcher(None, na, nb).ratio()
    if ratio >= FUZZY_NAME_MIN:
        return round(ratio, 3), f"姓名近似（{ratio:.2f}）"
    return 0.0, None


def id_tail_score(a: str, b: str) -> tuple[float, str | None]:
    ta, tb = normalize_tail(a), normalize_tail(b)
    if not ta or not tb:
        # 缺线索不等于匹配，也不等于冲突：不给分
        return 0.0, None
    if ta == tb:
        return 1.0, "证件尾号一致"
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(short) >= TAIL_OVERLAP_MIN_LEN and long_.endswith(short):
        return 0.5, "证件尾号后缀相容"
    return 0.0, "证件尾号不一致"


def compare(rec_a: dict, rec_b: dict) -> tuple[float, float, float, float, list[str]]:
    """比较两条记录，返回姓名/尾号/场次/总分与理由。

    输入字典需含 volunteer_name、id_tail、session_code。
    """
    ns, nr = name_score(rec_a["volunteer_name"], rec_b["volunteer_name"])
    ts, tr = id_tail_score(rec_a["id_tail"], rec_b["id_tail"])
    same_session = normalize_session(rec_a["session_code"]) == normalize_session(
        rec_b["session_code"]
    ) and bool(normalize_session(rec_a["session_code"]))
    ss = 1.0 if same_session else 0.0
    reasons: list[str] = []
    if same_session:
        reasons.append("同一场次")
    if nr:
        reasons.append(nr)
    if tr:
        reasons.append(tr)
    total = round(W_NAME * ns + W_ID * ts + W_SESSION * ss, 4)
    return ns, ts, ss, total, reasons


def _constrained_clusters(
    ids: Sequence[str],
    weighted_edges: dict[tuple[str, str], float],
    blocked: set[tuple[str, str]],
) -> list[list[str]]:
    """按边强度降序贪心合并；任何会使簇内出现阻断对的合并都被拒绝。

    阻断对因此既不直接复合，也不会经第三个顶点传递复合；
    强度相同与遍历顺序均按编号排序，保证结果确定。
    """
    parent = {rid: rid for rid in ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def members(root: str) -> set[str]:
        return {rid for rid in ids if find(rid) == root}

    ordered = sorted(weighted_edges, key=lambda pair: (-weighted_edges[pair], pair))
    for a, b in ordered:
        ra, rb = find(a), find(b)
        if ra == rb:
            continue
        ga, gb = members(ra), members(rb)
        if any(tuple(sorted((x, y))) in blocked  # type: ignore[operator]
               for x in ga for y in gb):
            continue
        parent[rb if ra < rb else ra] = ra if ra < rb else rb

    groups: dict[str, list[str]] = {}
    for rid in sorted(ids):
        groups.setdefault(find(rid), []).append(rid)
    return [sorted(m) for _, m in sorted(groups.items()) if len(m) > 1]


def _survivor(members: Iterable[str], records_by_id: dict[str, dict]) -> str:
    """主记录选择：有签到证据优先，其次有证件尾号，再次接收更早，最后编号最小。"""
    return min(
        members,
        key=lambda rid: (
            0 if records_by_id[rid].get("checkin_at") else 1,
            0 if normalize_tail(records_by_id[rid].get("id_tail", "")) else 1,
            records_by_id[rid].get("created_at", ""),
            rid,
        ),
    )


def build_proposals(
    records: Sequence[dict], blocked_pairs: Iterable[tuple[str, str]] | None = None
) -> list[Proposal]:
    """对活动记录生成确定性合并建议。

    records 只需参与去重的记录（未取消、未归档、当前未并入其他记录）。
    blocked_pairs 为人工驳回或已拆分解除的记录对，永不再成案。
    """
    records = sorted(records, key=lambda r: r["id"])
    by_id = {r["id"]: r for r in records}
    blocked = {tuple(sorted(pair)) for pair in (blocked_pairs or [])}  # type: ignore[misc]

    # 先按场次分组，跨场次不构成同一场服务重复
    sessions: dict[str, list[str]] = {}
    for rec in records:
        key = normalize_session(rec["session_code"])
        sessions.setdefault(key, []).append(rec["id"])

    weighted: dict[tuple[str, str], float] = {}
    edge_scores: dict[tuple[str, str], tuple[float, float, float, float]] = {}
    edge_reasons: dict[tuple[str, str], list[str]] = {}
    for members in sessions.values():
        for i, id_a in enumerate(members):
            for id_b in members[i + 1 :]:
                pair = tuple(sorted((id_a, id_b)))  # type: ignore[assignment]
                if pair in blocked:
                    continue
                ns, ts, ss, total, reasons = compare(by_id[id_a], by_id[id_b])
                if total < PROPOSE_THRESHOLD:
                    continue
                # 至少一项强标识完全一致，避免仅凭模糊姓名成案
                if ns < 1.0 and ts < 1.0:
                    continue
                weighted[pair] = total  # type: ignore[index]
                edge_scores[pair] = (ns, ts, ss, total)  # type: ignore[index]
                edge_reasons[pair] = reasons  # type: ignore[index]

    proposals: list[Proposal] = []
    for cluster in _constrained_clusters(
        [r["id"] for r in records], weighted, blocked  # type: ignore[arg-type]
    ):
        survivor = _survivor(cluster, by_id)
        duplicates = tuple(rid for rid in cluster if rid != survivor)
        scores: list[ClueScore] = []
        reasons: set[str] = set()
        for rid in cluster:
            if rid == survivor:
                ns = ts = total = 1.0
                ss = 1.0
                rec_reasons: list[str] = ["主记录"]
            else:
                pair = tuple(sorted((rid, survivor)))  # type: ignore[assignment]
                ns, ts, ss, total = edge_scores.get(pair, (0.0, 0.0, 0.0, 0.0))  # type: ignore[arg-type]
                rec_reasons = list(edge_reasons.get(pair, []))  # type: ignore[arg-type]
                # 传递相连的成员也要汇总其边理由
                for other in cluster:
                    if other == rid:
                        continue
                    p = tuple(sorted((rid, other)))  # type: ignore[assignment]
                    reasons.update(edge_reasons.get(p, []))  # type: ignore[arg-type]
            rec = by_id[rid]
            scores.append(
                ClueScore(
                    record_id=rid,
                    name=rec["volunteer_name"],
                    id_tail=rec.get("id_tail", ""),
                    session_code=rec["session_code"],
                    name_score=ns,
                    id_score=ts,
                    session_score=ss,
                    total=total,
                    reasons=tuple(rec_reasons),
                )
            )
        for other_a_i, rid_a in enumerate(cluster):
            for rid_b in cluster[other_a_i + 1 :]:
                reasons.update(edge_reasons.get(tuple(sorted((rid_a, rid_b))), []))  # type: ignore[arg-type]
        ordered_reasons = tuple(
            sorted(r for r in reasons if r not in {"主记录"})
        )
        proposals.append(
            Proposal(
                survivor_id=survivor,
                duplicates=duplicates,
                scores=tuple(sorted(scores, key=lambda s: s.record_id)),
                reasons=ordered_reasons,
            )
        )
    return proposals

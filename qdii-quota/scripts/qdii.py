#!/usr/bin/env python3
"""QDII 申购额度查询：调用 qdii-quota-api 并渲染 Markdown（只用标准库）。

配置：QDII_API_BASE（如 https://<域名>/qdii-quota-api）、QDII_API_KEY。优先读环境变量，其次读 Skill 目录下的
config.ini（每行 KEY=VALUE，参考 config.example.ini）。Python 3.8+，只用标准库；Windows / macOS / Linux 通用
（Windows 上命令用 `python` 或 `py -3` 代替 `python3`）。

  python3 qdii.py rank  [--region 美股] [--asset equity] [--q 纳指] [--n 10] [--currency CNY] [--investor individual]
                        [--channel tiantian|agency|direct] [--as-of YYYY-MM-DD] [--limited-only]
        不填 --as-of = 现在下单的受理日：已过 15:00 或今天非交易日时自动改查下一交易日
  python3 qdii.py table [同上筛选] [--codes 007280,019449]     完整表（含暂停、休市、未知）
  python3 qdii.py fund  007280 [--as-of ...]                     单只基金逐份额逐渠道明细
  python3 qdii.py coverage                                       数据时点与覆盖率
  任一子命令加 --json 输出原始 JSON。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / "config.ini"
CURRENCY = {"CNY": "人民币", "USD": "美元"}
INVESTOR = {"individual": "个人", "institution": "机构"}
INVESTOR_SHORT = {"individual": "个人", "institution": "机构"}
# QDII_DISPLAY=public (set in config.ini): plain channel names for retail readers, no platform
# channel or institution rows in the fund table; the platform check becomes one supplementary line
PUBLIC_CHANNEL_LABEL = {"agency": "代销（支付宝/天天基金等销售机构）", "direct": "直销（基金公司官方平台）"}


def setting(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name].strip()
    if CONFIG.exists():
        for line in CONFIG.read_text(encoding="utf-8-sig").splitlines():  # Windows 记事本可能加 BOM
            k, sep, v = line.partition("=")
            if sep and k.strip() == name:
                return v.strip().strip('"').strip("'")
    return ""


def public() -> bool:
    return setting("QDII_DISPLAY") == "public"


def channel_name(channel: str, label: str | None) -> str:
    return PUBLIC_CHANNEL_LABEL.get(channel, label or channel) if public() else (label or channel)


def call(path: str, params: dict) -> dict:
    base, key = setting("QDII_API_BASE").rstrip("/"), setting("QDII_API_KEY")
    if not base or not key:
        sys.exit(f"未配置 QDII_API_BASE / QDII_API_KEY（环境变量或 {CONFIG}），无法查询。")
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "", False)})
    req = urllib.request.Request(f"{base}{path}?{q}", headers={"X-API-Key": key, "Accept": "application/json"})
    for attempt in (1, 2, 3):  # read-only GET; the gateway sometimes drops the first TLS handshake after idle
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                detail = json.load(e).get("detail")
            except (ValueError, AttributeError):
                detail = None
            # retry gateway hiccups only; a 503 with a JSON detail is the service answering (e.g. view not built),
            # and repeating it makes the front gateway take the whole service out for a while
            if (e.code in (502, 504) or (e.code == 503 and not detail)) and attempt < 3:
                time.sleep(attempt)
                continue
            sys.exit(f"查询失败（HTTP {e.code}）：{detail or e.reason}")
        except (urllib.error.URLError, ConnectionError, TimeoutError, ValueError) as e:
            if attempt < 3:
                time.sleep(attempt)
                continue
            sys.exit(f"查询服务不可达：{getattr(e, 'reason', e)}")
    raise AssertionError("unreachable")


# ---------- formatting ----------

def money(v: str | None, cur: str | None) -> str:
    if v is None:
        return "—"
    d = float(v)
    unit = "美元" if cur == "USD" else "元"
    if d >= 10000 and d % 10000 == 0:
        return f"{d / 10000:g} 万{unit}"
    return f"{d:,.2f}".rstrip("0").rstrip(".") + f" {unit}"


def quota(r: dict) -> str:
    """状态 / 单日上限：状态优先，数值只在开放时是当日可申购上限。"""
    s = r["day_status"]
    if s == "open":
        if r["unlimited"]:
            return "不限额"
        return money(r["limit_amount"], r["limit_currency"]) if r["limit_amount"] else "限额（金额未知）"
    if s == "closed":
        base = "不限额" if r["unlimited"] else money(r["limit_amount"], r["limit_currency"])
        return f"休市（恢复后 {base}）" if r["action"] in ("limit", "unlimit") else "休市"
    return r["day_status_label"] or s


def sort_value(r: dict) -> float:
    if r["day_status"] != "open":
        return -1
    return float("inf") if r["unlimited"] else float(r["limit_amount"] or 0)


def md(d: str, as_of: str) -> str:
    """2026-09-02 -> 09-02（同年省略年份）。"""
    return d[5:] if d[:4] == as_of[:4] else d


def evidence(r: dict, as_of: str) -> str:
    kinds = {c["type"] for c in r.get("conflicts") or []}
    tag = {"channel_verified": "✓ 已核实", "announcement": "公告口径", "reference": "⚠ 参考值"}.get(
        r["evidence_level"], r["evidence_label"] or r["evidence_level"])
    if "pending_review" in kinds:
        tag = "⚠ 待复核"
    a = r.get("announcement")
    out = f"{tag} · [{md(a['publish_date'], as_of)} 公告]({a['url']})" if a else tag
    if r.get("changed_today"):
        out += " · **当日变更**"
    return out


SUFFIX = ("人民币", "美元现", "美元")


def base_name(names: list[str], fallback: str) -> str:
    """同一产品各份额简称的公共前缀，去掉份额类别：南方纳斯达克100A/C/I -> 南方纳斯达克100。"""
    names = [n for n in names if n]
    if not names:
        return fallback
    pre = os.path.commonprefix(names)
    if len(names) == 1 or pre == names[0]:
        pre = pre[:-1] if pre and pre[-1].isascii() and pre[-1].isupper() else pre
    for s in SUFFIX:
        if pre.endswith(s) and len(pre) > len(s):
            pre = pre[: -len(s)]
            break
    return pre or fallback


def share_label(r: dict, base: str) -> str:
    name = r["share_name"] or ""
    cls = name[len(base):] if name.startswith(base) else ""
    return f"{cls} {r['share_code']}" if cls else r["share_code"]


def by_product(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["product_id"], []).append(r)
    return out


def merged(rows: list[dict], base: str, as_of: str) -> list[tuple[str, str, str]]:
    """同额度、同依据的份额合并成一行：[(份额, 状态/上限, 依据)]，按额度从高到低。"""
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        groups.setdefault((quota(r), evidence(r, as_of)), []).append(r)
    items = sorted(groups.items(), key=lambda kv: -max(sort_value(r) for r in kv[1]))
    return [(" · ".join(share_label(r, base) for r in rs), q, e) for (q, e), rs in items]


def title_line(d: dict, what: str) -> str:
    if "channel_label" not in d:
        return f"### {what}"
    ch, cur = channel_name(d["channel"], d["channel_label"]), CURRENCY[d["currency"]]
    if public() and d["investor"] == "individual":  # the label carries its own brackets; retail readers only
        return f"### {what}｜{ch} · {cur}份额"
    return f"### {what}（{ch} · {INVESTOR[d['investor']]} · {cur}份额）"


def timing(d: dict) -> list[str]:
    """标题下一行：查询时间 + 这张表是哪一天的额度。"""
    qt, as_of = d["query_time"], d["as_of"]
    note = f"{md(qt[:10], as_of)} {qt[11:16]} 查询"
    shift = d.get("_shift")
    if shift == "cutoff":
        note += f" · 已过 {d['cutoff']}，现在下单按 **{md(as_of, as_of)}** 受理，下表为 {md(as_of, as_of)} 额度"
    elif shift == "non_trading":
        note += f" · 今天不是交易日，现在下单按 **{md(as_of, as_of)}** 受理，下表为 {md(as_of, as_of)} 额度"
    elif not d["trading_day"]:
        note += f" · **{md(as_of, as_of)} 不是交易日，不受理申购**；下一交易日 {md(d['next_trading_day'], as_of)}"
    elif d.get("cutoff_passed"):
        note += f" · 下表为今日额度；已过 {d['cutoff']}，现在下单按 {md(d['next_trading_day'], as_of)} 受理"
        if not d.get("next_view_available"):
            note += "（该日额度尚未生成）"
    elif as_of != qt[:10]:
        note += f" · 下表为 **{md(as_of, as_of)}** 额度"
    else:
        note += f" · 下表为今日（{md(as_of, as_of)}）额度"
    return [f"> {note}", ""]


def footnote(channel: str, extra: list[str]) -> list[str]:
    parts = ["额度 = 单个基金账户单日累计申购上限，A/C 等份额分别计算、不可相加"]
    if channel == "tiantian":
        parts.append("✓ 已核实 = 公告与天天基金页面一致；天天基金不销售美元份额")
    elif channel == "agency":
        parts.append("代销口径为基金公告的统一规定，具体平台可能更严")
    elif channel == "both":
        parts.append("代销 = 支付宝/天天基金等销售机构，口径为基金公告的统一规定，具体平台可能更严；直销 = 基金公司官方平台")
    parts.append("⚠ 参考值 = 公告与渠道不一致或仅有渠道显示，已取较严值")
    parts += extra
    parts.append("仅供参考，以下单页面为准")
    return ["", "注：" + "；".join(parts) + "。"]


def unavailable(rows: list[dict], shown: set[str], as_of: str) -> list[str]:
    """表外一行列出暂停 / 休市的产品；榜单内产品只列被暂停的份额类别。"""
    out = []
    for status, label in (("suspended", "暂停申购"), ("closed", "当日休市")):
        items = []
        for pid, rs in by_product(rows).items():
            hit = [r for r in rs if r["day_status"] == status]
            if not hit:
                continue
            base = base_name([r["share_name"] for r in rs], rs[0]["product_name"])
            name = base
            if pid in shown:
                name += "（" + "/".join(share_label(r, base).split(" ")[0] for r in hit) + " 类）"
            starts = {(r.get("announcement") or {}).get("effective_from") for r in hit}
            if status == "suspended" and as_of in starts:
                name += f"（{md(as_of, as_of)} 起）"
            items.append(name)
        if items:
            out += ["", f"**{label}**（{len(items)} 只）：" + "、".join(items)]
    return out


# ---------- views ----------

def render_rank(d: dict, all_rows: list[dict] | None = None) -> str:
    what = f"{d['scope']} QDII 可申购额度"
    lines = [title_line(d, what), ""] + timing(d)
    if d["channel"] == "tiantian" and d["currency"] == "USD":
        lines.append("天天基金不销售美元份额。可改查人民币份额，或加 `--channel agency` 查看基金公告对代销渠道的美元份额额度。")
        return "\n".join(lines)
    extra = []
    if d.get("unverified_products"):
        extra.append(f"另有 {d['unverified_products']} 只状态未核实，未列入")
    if d.get("unlimited_excluded"):
        extra.append(f"按要求未列入 {d['unlimited_excluded']} 只不限额产品")
    if not d["products"]:
        lines.append(f"**没有可申购的产品**（候选 {d['candidate_products']} 只）。")
        lines += unavailable(all_rows or [], set(), d["as_of"])
        return "\n".join(lines + footnote(d["channel"], extra))
    top = [p for p in d["products"] if p["rank"] == 1]
    best = "不限额" if top[0]["unlimited"] else money(top[0]["value"], d["currency"]) + "/日"
    names = []
    for p in top:
        names.append(base_name([r["share_name"] for r in p["rows"]], p["product_name"]))
    shown_note = f"，下表列前 {d['n']}" if d["ranked"] > len(d["products"]) else ""
    tie = f"（第 {d['n']} 名并列，多列 {d['ties_beyond_n']} 只）" if d.get("ties_beyond_n") else ""
    lines += [f"**{d['ranked']} 只可买**，最高 {best}（{'、'.join(names)}）{shown_note}{tie}。", "",
              "| # | 基金 | 可买份额 | 单日上限 | 依据 |", "| --- | --- | --- | --- | --- |"]
    for p in d["products"]:
        base = base_name([r["share_name"] for r in p["rows"]], p["product_name"])
        buyable = [r for r in p["rows"] if r["day_status"] == "open"]
        for i, (shares, q, e) in enumerate(merged(buyable, base, d["as_of"])):
            lines.append(f"| {p['rank'] if i == 0 else ''} | {base if i == 0 else ''} | {shares} | {q} | {e} |")
    shown = {p["product_id"] for p in d["products"]}
    lines += unavailable(all_rows if all_rows is not None else [r for p in d["products"] for r in p["rows"]],
                         shown, d["as_of"])
    return "\n".join(lines + footnote(d["channel"], extra))


def render_table(d: dict) -> str:
    lines = [title_line(d, "QDII 申购额度完整表"), ""] + timing(d)
    order = {"open": 0, "closed": 1, "suspended": 2, "not_sold": 3, "unknown": 4}
    stat: dict[str, int] = {}
    for r in d["rows"]:
        stat[r["day_status_label"]] = stat.get(r["day_status_label"], 0) + 1
    lines += ["共 {} 个份额：{}".format(len(d["rows"]), "，".join(f"{k} {v}" for k, v in stat.items())), "",
              "| 基金 | 份额 | 状态 / 单日上限 | 依据 |", "| --- | --- | --- | --- |"]
    products = sorted(by_product(d["rows"]).values(),
                      key=lambda rs: (min(order.get(r["day_status"], 9) for r in rs), -max(map(sort_value, rs))))
    for rs in products:
        base = base_name([r["share_name"] for r in rs], rs[0]["product_name"])
        for i, (shares, q, e) in enumerate(merged(rs, base, d["as_of"])):
            lines.append(f"| {base if i == 0 else ''} | {shares} | {q} | {e} |")
    return "\n".join(lines + footnote(d["channel"], []))


# ---------- public edition: agency and direct side by side ----------

def pairs(agency: list[dict], direct: list[dict]) -> list[tuple[dict, dict | None, dict | None]]:
    """按份额对齐两个渠道：[(参考行, 代销行, 直销行)]，任一渠道缺该份额时为 None。"""
    a, d = {r["share_code"]: r for r in agency}, {r["share_code"]: r for r in direct}
    codes = list(a) + [c for c in d if c not in a]
    return [(a.get(c) or d[c], a.get(c), d.get(c)) for c in codes]


def pair_open(p: tuple) -> bool:
    return any(r and r["day_status"] == "open" for r in p[1:])


def pair_value(p: tuple) -> float:
    return max(sort_value(r) if r else -1 for r in p[1:])


def pair_evidence(a: dict | None, d: dict | None, as_of: str) -> str:
    ea, ed = (evidence(r, as_of) if r else None for r in (a, d))
    if ea and ed and ea != ed:
        return f"代销 {ea}；直销 {ed}"
    return ea or ed or "—"


def merged_pairs(ps: list[tuple], base: str, as_of: str) -> list[tuple[str, str, str, str]]:
    """两个渠道额度和依据都相同的份额合并成一行：[(份额, 代销, 直销, 依据)]，按较高额度从高到低。"""
    groups: dict[tuple[str, str, str], list[tuple]] = {}
    for p in ps:
        key = tuple(quota(r) if r else "—" for r in p[1:]) + (pair_evidence(p[1], p[2], as_of),)
        groups.setdefault(key, []).append(p)
    items = sorted(groups.items(), key=lambda kv: -max(map(pair_value, kv[1])))
    return [(" · ".join(share_label(p[0], base) for p in g), qa, qd, e) for (qa, qd, e), g in items]


def both_title(d: dict, what: str) -> str:
    return f"### {what}｜代销 / 直销 · {CURRENCY[d['currency']]}份额"


def render_rank_both(da: dict, dd: dict, rows_a: list[dict], rows_d: list[dict], n: int) -> str:
    """da / dd 是两个渠道的榜单（取满 50 名），按两个渠道中较高的额度重新排名。"""
    lines = [both_title(da, f"{da['scope']} QDII 可申购额度"), ""] + timing(da)
    extra = []
    if max(da.get("unverified_products") or 0, dd.get("unverified_products") or 0):
        extra.append(f"另有 {max(da['unverified_products'], dd['unverified_products'])} 只状态未核实，未列入")
    if max(da.get("unlimited_excluded") or 0, dd.get("unlimited_excluded") or 0):
        extra.append(f"按要求未列入 {max(da['unlimited_excluded'], dd['unlimited_excluded'])} 只不限额产品")
    ranked = {p["product_id"] for p in da["products"] + dd["products"]}
    all_pairs = pairs(rows_a, rows_d)
    products = []
    for pid, ps in by_product([p[0] for p in all_pairs]).items():
        if pid in ranked:
            mine = [p for p in all_pairs if p[0]["product_id"] == pid and pair_open(p)]
            if mine:
                products.append((max(map(pair_value, mine)), pid, mine))
    products.sort(key=lambda x: -x[0])
    if not products:
        lines.append(f"**没有可申购的产品**（候选 {da['candidate_products']} 只）。")
        lines += unavailable([p[1] or p[2] for p in all_pairs if not pair_open(p)], set(), da["as_of"])
        return "\n".join(lines + footnote("both", extra))
    ranks = [1 + sum(1 for o in products if o[0] > v) for v, _, _ in products]
    cut = ranks[min(n, len(products)) - 1]
    shown = [(r, *x) for r, x in zip(ranks, products) if r <= cut]
    complete = all(len(d["products"]) >= d["ranked"] for d in (da, dd))
    # same "产品（份额）" count as the full table; share classes of one product are counted once in 只
    total = (f"{len(products)} 只（{sum(len(x[2]) for x in products)} 个份额）" if complete
             else f"至少 {max(da['ranked'], dd['ranked'])} 只")
    v0 = shown[0][1]
    best = "不限额" if v0 == float("inf") else money(str(v0), da["currency"]) + "/日"
    names = [base_name([p[0]["share_name"] for p in ps], ps[0][0]["product_name"]) for r, _, _, ps in shown if r == 1]
    shown_note = f"，下表列前 {n}" if len(products) > len(shown) else ""
    tie = f"（第 {n} 名并列，多列 {len(shown) - n} 只）" if len(shown) > n else ""
    who = "、".join(names) if len(names) <= 3 else "、".join(names[:3]) + f" 等 {len(names)} 只"
    lines += [f"代销或直销任一渠道可买 **{total}**，最高 {best}（{who}），"
              f"按两个渠道中较高的额度排序{shown_note}{tie}。", "",
              "| # | 基金 | 份额 | 代销 | 直销 | 依据 |", "| --- | --- | --- | --- | --- | --- |"]
    for rank, _, _, ps in shown:
        base = base_name([p[0]["share_name"] for p in ps], ps[0][0]["product_name"])
        for i, (shares, qa, qd, e) in enumerate(merged_pairs(ps, base, da["as_of"])):
            lines.append(f"| {rank if i == 0 else ''} | {base if i == 0 else ''} | {shares} | {qa} | {qd} | {e} |")
    lines += unavailable([p[1] or p[2] for p in all_pairs if not pair_open(p)], {x[2] for x in shown}, da["as_of"])
    return "\n".join(lines + footnote("both", extra))


def render_table_both(da: dict, dd: dict) -> str:
    lines = [both_title(da, "QDII 申购额度完整表"), ""] + timing(da)
    ps = pairs(da["rows"], dd["rows"])
    # products with share counts in brackets, the same wording as rank's summary line
    open_a, open_d = ({r["product_id"] for r in d["rows"] if r["day_status"] == "open"} for d in (da, dd))
    open_ps = [p for p in ps if pair_open(p)]
    total = {p[0]["product_id"] for p in ps}
    lines += [f"共 {len(total)} 只产品（{len(ps)} 个份额），代销或直销任一渠道可买 "
              f"**{len({p[0]['product_id'] for p in open_ps})} 只（{len(open_ps)} 个份额）**；"
              f"其中代销可买 {len(open_a)} 只、直销可买 {len(open_d)} 只", "",
              "| 基金 | 份额 | 代销 | 直销 | 依据 |", "| --- | --- | --- | --- | --- |"]
    groups: dict[str, list[tuple]] = {}
    for p in ps:
        groups.setdefault(p[0]["product_id"], []).append(p)
    for g in sorted(groups.values(), key=lambda g: (not any(map(pair_open, g)), -max(map(pair_value, g)))):
        base = base_name([p[0]["share_name"] for p in g], g[0][0]["product_name"])
        for i, (shares, qa, qd, e) in enumerate(merged_pairs(g, base, da["as_of"])):
            lines.append(f"| {base if i == 0 else ''} | {shares} | {qa} | {qd} | {e} |")
    return "\n".join(lines + footnote("both", []))


def platform_note(rows: list[dict], platform: list[dict], base: str) -> list[str]:
    """Public edition: the sales-platform check as one line under the table, without naming the platform."""
    agency = {r["share_code"]: r for r in rows if r["channel"] == "agency"}
    notes = []  # only differences; agreement is the normal case and goes unsaid
    for p in platform:
        if p["day_status"] == "exchange_only":
            continue
        a, cls = agency.get(p["share_code"]), share_label(p, base)
        if p["day_status"] == "not_sold":
            notes.append(f"{cls} 该平台暂不销售")
        elif a is None or quota(a) != quota(p):
            notes.append(f"{cls} 平台显示{quota(p)}（公告口径：{quota(a) if a else '无'}）")
    return ["", "补充（与主流代销平台页面核对）：" + "；".join(notes) + "。"] if notes else []


def render_fund(d: dict) -> str:
    p = d["product"]
    lines = [f"### {p['name']}：各份额、各渠道申购额度", ""] + timing(d)
    lines.append(f"管理人 {p['manager'] or '—'}；业绩比较基准：{p['benchmark'] or '未披露'}；主要地区 {p['region'] or '—'}")
    base = base_name([r["share_name"] for r in d["rows"]], p["name"])
    rows, platform = d["rows"], []
    if public():
        platform = [r for r in rows if r["channel"] == "tiantian" and r["investor"] == "individual"]
        rows = [r for r in rows if r["channel"] in PUBLIC_CHANNEL_LABEL and r["investor"] == "individual"]
        lines += ["", "| 渠道 | 份额 | 状态 / 单日上限 | 依据 |", "| --- | --- | --- | --- |"]
    else:
        lines += ["", "| 渠道 | 投资者 | 份额 | 状态 / 单日上限 | 依据 |", "| --- | --- | --- | --- | --- |"]
    keys: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        if r["day_status"] == "exchange_only" and (r["channel"], r["investor"]) != ("agency", "individual"):
            continue
        keys.setdefault((channel_name(r["channel"], r["channel_label"]), r["investor"]), []).append(r)
    for (ch, inv), rs in keys.items():
        for i, (shares, q, e) in enumerate(merged(rs, base, d["as_of"])):
            first = ch if i == 0 else ""
            lines.append(f"| {first} | {shares} | {q} | {e} |" if public() else
                         f"| {first} | {INVESTOR[inv] if i == 0 else ''} | {shares} | {q} | {e} |")
    lines += platform_note(rows, platform, base)
    if d["upcoming_closures"]:
        lines += ["", "近期休市（暂停申购）日：" + "、".join(c["day"] for c in d["upcoming_closures"][:10])]
    if d["recent_quota_announcements"]:
        lines += ["", "近期额度类公告："] + [f"- {a['publish_date']} [{a['title']}]({a['url']})"
                                         for a in d["recent_quota_announcements"][:5]]
    return "\n".join(lines + footnote(d["default_channel"], []))


def render_coverage(d: dict) -> str:
    lines = [f"### 数据覆盖与时点（{d['channel']}）", ""] + timing(d)
    if d.get("view_missing"):
        lines += [f"**{md(d['as_of'], d['as_of'])} 的额度视图尚未生成**，以下为 {md(d['view_as_of'], d['as_of'])} 的数据。",
                  ""]
    sched = d.get("scheduler")
    if sched:
        last = (sched["last_cycle_at"] or "无记录").replace("T", " ")
        lines.append(f"最近一轮调度 {last}" + ("（**超过 12 小时未运行，数据可能已停止更新**）" if sched["stale"] else ""))
    lines += [f"额度视图计算于 {d['view_computed_at'].replace('T', ' ')}" + ("（**已过期**）" if d["stale"] else ""),
              f"份额 {d['shares']} 个、产品 {d['products']} 个；个人投资者状态分布：{d['day_status']}",
              f"证据等级：{d['evidence']}；待复核：{d['open_review_tasks']}"]
    for job, j in d["jobs"].items():
        lines.append(f"- {job}：{j['status']}，{(j['finished_at'] or '').replace('T', ' ')}"
                     + (f"，{j['error']}" if j["error"] else ""))
    return "\n".join(lines)


def fetch(path: str, params: dict, explicit_date: bool) -> dict:
    """未指定日期时按“现在下单哪天受理”取数：已过 15:00 或今天非交易日，自动改查下一交易日。"""
    d = call(path, params)
    if explicit_date or not d.get("next_view_available"):
        return d
    reason = "cutoff" if d.get("cutoff_passed") else "non_trading" if not d.get("trading_day") else None
    if reason is None:
        return d
    d = call(path, {**params, "as_of": d["next_trading_day"]})
    d["_shift"] = reason
    return d


def utf8_stdout() -> None:
    """Windows 控制台默认 GBK，表格里的 ✓ ⚠ · 会触发 UnicodeEncodeError；统一按 UTF-8 输出。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main() -> None:
    utf8_stdout()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("rank", "table"):
        s = sub.add_parser(name)
        s.add_argument("--region")
        s.add_argument("--asset", choices=["equity", "bond", "commodity"])
        s.add_argument("--q")
        s.add_argument("--currency", default="CNY", choices=["CNY", "USD"])
        s.add_argument("--investor", default="individual", choices=["individual", "institution"])
        s.add_argument("--channel", choices=["tiantian", "agency", "direct"])
        s.add_argument("--as-of", help="额度日期；不填 = 现在下单的受理日（15:00 后、非交易日自动顺延）")
        s.add_argument("--json", action="store_true")
        if name == "rank":
            s.add_argument("--n", type=int, default=10)
            s.add_argument("--limited-only", action="store_true", help="只排有数值上限的产品")
        else:
            s.add_argument("--codes")
    f = sub.add_parser("fund")
    f.add_argument("code")
    f.add_argument("--as-of")
    f.add_argument("--json", action="store_true")
    c = sub.add_parser("coverage")
    c.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.cmd in ("rank", "table"):
        filters = {"region": a.region, "asset": a.asset, "q": a.q, "currency": a.currency, "investor": a.investor,
                   "channel": a.channel, "as_of": a.as_of}
        if public() and not a.channel:
            # public edition: agency and direct as two columns of one table, for the same order date
            if a.cmd == "rank":
                rk = {"n": 50, "include_unlimited": "false" if a.limited_only else None}
                da = fetch("/rankings", {**filters, **rk, "channel": "agency"}, bool(a.as_of))
                same = {**filters, "as_of": da["as_of"]}
                dd = call("/rankings", {**same, **rk, "channel": "direct"})
                out = render_rank_both(da, dd, call("/quotas", {**same, "channel": "agency"})["rows"],
                                       call("/quotas", {**same, "channel": "direct"})["rows"], a.n)
            else:
                da = fetch("/quotas", {**filters, "codes": a.codes, "channel": "agency"}, bool(a.as_of))
                dd = call("/quotas", {**filters, "codes": a.codes, "channel": "direct", "as_of": da["as_of"]})
                out = render_table_both(da, dd)
            d = {"agency": da, "direct": dd}
        elif a.cmd == "rank":
            d = fetch("/rankings", {**filters, "n": a.n, "include_unlimited": "false" if a.limited_only else None},
                      bool(a.as_of))
            all_rows = call("/quotas", {**filters, "as_of": d["as_of"]})["rows"]
            out = render_rank(d, all_rows)
        else:
            d = fetch("/quotas", {**filters, "codes": a.codes}, bool(a.as_of))
            out = render_table(d)
    elif a.cmd == "fund":
        d = fetch(f"/funds/{a.code}", {"as_of": a.as_of}, bool(a.as_of))
        out = render_fund(d)
    else:
        d = call("/meta/coverage", {})
        out = render_coverage(d)
    print(json.dumps(d, ensure_ascii=False, indent=1) if a.json else out)


if __name__ == "__main__":
    main()

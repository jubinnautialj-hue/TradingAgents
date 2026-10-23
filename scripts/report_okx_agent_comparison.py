"""Render completed D.4 audit artifacts without any model or market request."""
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tradingagents.reporting import write_report_tree


def text(call):
    if call.get("parsed"):
        return json.dumps(call["parsed"], ensure_ascii=False, indent=2)
    value = call.get("output", "")
    if isinstance(value, str): return value
    return "\n".join(v.get("text", "") for v in value if isinstance(v, dict))


def render(root):
    protocol = json.loads((root/"protocol.json").read_text(encoding="utf-8"))
    result = json.loads((root/"comparison.json").read_text(encoding="utf-8"))
    report = root/"report.md"
    lines = ["# D.4 多 Agent 与纯规则筛选对照", "", "execution_enabled=False；全部历史仍为开发集。", "",
        "固定全部候选按时间排序的首个、中间、末个样本；不按盈亏挑选。完整 B 组尚未评估。",
        "绩效固定原账本已准入机会，拒绝后不补入其他交易，不重新分配资金。ABSTAIN/故障不计成功避险。", "",
        f"候选 {result['all_candidates']}；实际调用模型的候选 {result['agent_attempted']}；完成 {result['completed']}；未研究 {result['unresearched_candidates']}。",
        f"决定统计：{json.dumps(result['decision_counts'])}；状态：{json.dumps(result['status_counts'])}。", "",
        "| UTC cutoff | 筛选 | 原账本成交 | 原交易净损益 USDT | 解释 |", "|---|---|---|---:|---|"]
    for row in result["cases"]:
        pnl = "—" if row["A_net_pnl"] is None else f"{row['A_net_pnl']:.8f}"
        lines.append(f"| {row['cutoff_utc']} | {row['decision'] or row['status']} | {row['A_original_trade']} | {pnl} | {row['interpretation']} |")
    lines += ["", "| 比较范围 | 交易数 | 净损益 USDT | 手续费 | 胜率 | 小时收盘权益最大回撤 |",
              "|---|---:|---:|---:|---:|---:|"]
    for name, group in result["groups"].items():
        m = group["metrics"]
        win = "不适用" if m["win_rate"] is None else f"{m['win_rate']:.4%}"
        lines.append(f"| {name} | {m['trades']} | {m['net_pnl']:.8f} | {m['fees_usdt']:.8f} | {win} | {m['max_drawdown']:.4%} |")
        path = root/(name+"_equity.csv")
        if not path.exists():
            with path.open("x", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=["ts", "utc", "equity"])
                writer.writeheader(); writer.writerows(group["equity_curve"])
    lines += ["", "A_all 是全部原策略历史结果；不能与仅研究三个候选的 B_accepted_only 混作总体效果比较。",
        "A_decisive_only 与 B_accepted_only 才是明确决定样本的配对比较。零交易不代表策略安全或模型避险成功。", "",
        "## 逐案例理由", ""]
    for row in result["cases"]:
        lines += ["### "+row["cutoff_utc"], "", row.get("rationale") or "未获得有效模型结论。", ""]
        cid = row["candidate_id"]
        case = json.loads((root/(cid+".result.json")).read_text(encoding="utf-8"))
        directory = root/"native_reports"/cid
        if case["status"] == "COMPLETED" and not directory.exists():
            calls = case["calls"]
            state = {"trade_date": row["cutoff_utc"][:10], "market_report": text(calls[0]),
                "investment_debate_state": {"bull_history": text(calls[1]), "bear_history": text(calls[2])},
                "investment_plan": text(calls[3]), "trader_investment_plan": text(calls[4]),
                "risk_debate_state": {"aggressive_history": text(calls[5]), "conservative_history": text(calls[6]), "neutral_history": text(calls[7])},
                "final_trade_decision": "D.4 candidate filter only; not a live trade plan.\n\n"+text(calls[8]),
                "final_rating": "REVIEW", "llm_status": "AVAILABLE", "workflow_status": "COMPLETED",
                "execution_enabled": False, "allow_new_risk": False,
                "memory_note": "D.4 Memory disabled. Historical preflight passed under disclosed assumptions. Header REVIEW is the legacy execution rating; filter choice is separate."}
            # Export completed OFFLINE text through native renderer. No live
            # bundle is manufactured, and no live-plan authorization is issued.
            write_report_tree(state, "BTC-USDT-SWAP (D.4 historical filter)", directory,
                settings={**protocol["model_config"], "version": "0.7.0", "analysts": ["market"],
                          "max_debate_rounds": 1, "max_risk_discuss_rounds": 1}, html=True)
    lines += ["## 调用与费用", "", "```json", json.dumps(result["model_usage"], indent=2), "```", "",
        "实际金额未知；订阅模型未采用 max_tokens 硬上限，按调用次数、无重试和固定样本限制预算。", "",
        "## 限制及结论", "",
        "历史参数连续性、结算瞬间标记价和资金持仓归属仍继承 D.3.1 的不确定性。模型训练中的事后知识不能通过输入截断完全消除。",
        "费用敏感性保留较低/基准/较高三组，完整明细在 comparison.json 的 cost_sensitivity。",
        "本样本不能证明多 Agent 优势；若没有原成交样本，则连过滤亏损/错失盈利也无法评价。下一步应先预注册覆盖更多可配对候选的协议，不依据本轮模型结论挑选有利案例。",
        "Memory/Reflection 只建议后续建立独立模拟命名空间和 resolved_at_ms 时间过滤，本轮未实现、未运行 D.5。"]
    if not report.exists():
        with report.open("x", encoding="utf-8") as f: f.write("\n".join(lines)+"\n")
    rows_path = root/"cases.csv"
    if not rows_path.exists():
        with rows_path.open("x", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(result["cases"][0]))
            writer.writeheader(); writer.writerows(result["cases"])
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, type=Path)
    print(render(parser.parse_args().experiment))

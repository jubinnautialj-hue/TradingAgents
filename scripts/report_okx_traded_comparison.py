"""Render sealed D.4.1 results offline, using the existing native report tree."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tradingagents import okx_traded_comparison as experiment
from tradingagents.reporting import write_report_tree
from scripts.report_okx_agent_comparison import text


def render(root, report_name="report.md"):
    if Path(report_name).name != report_name or not report_name.endswith(".md"):
        raise ValueError("Use a Markdown filename within the experiment directory")
    protocol = experiment.read(root/"protocol.json")
    cases = experiment.verified_cases(root, protocol)
    result = experiment.read(root/"analysis.json")
    if result["sealed_decisions_hash"] != experiment.bt.digest(experiment.read(root/"sealed_decisions.json")):
        raise ValueError("Analysis does not match independent sealed decisions")
    lines = ["# D.4.1 多 Agent 成交条件样本对照", "", "execution_enabled=False；全部历史仍为开发集。", "",
        "纳入原 279 个候选中全部 14 个原成交对应候选，不根据盈亏选择。此为回顾性的成交条件样本，不能代表全部候选或严格样本外预测。",
        "所有 Agent 结果先独立封存，再由另一个进程关联原交易结果。原九角色、提示词、策略、成本和候选条件保持冻结。", "",
        f"动作：{json.dumps(result['decision_counts'])}；状态：{json.dumps(result['status_counts'])}。",
        f"配对归属：{json.dumps(result['paired_classifications'])}。", "",
        "配对分类仅描述原 14 笔交易，不能将被拒绝原交易的盈亏直接当作组合收益变化。", "",
        "| UTC cutoff | 方向 | Agent 动作／故障 | 原交易净损益 USDT | 观察性归属 | B 实际成交 |",
        "|---|---|---|---:|---|---|"]
    for row in result["cases"]:
        lines.append(f"| {row['cutoff_utc']} | {row['direction']} | {row['decision'] or row['status']} | {row['original_net_pnl']:.8f} | {row['classification']} | {row['entered_in_B_continuous_replay']} |")
    lines += ["", "## 同一连续账本重放", "",
        "B 仅屏蔽明确 REJECT 的候选；ACCEPT 不改条件，ABSTAIN/失败和其他 265 个未研究候选继续按原规则。完整回放全部决策点及持仓冲突、后续机会和费用。",
        "B 是部分 Agent 覆盖的混合策略，不是完整 Agent 策略或真实收益。", "",
        "| 组别 | 交易数 | 多／空 | 胜率 | 净损益 USDT | 手续费 | 资金费现金流代理 | 内含滑点 | 最大回撤 |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|"]
    for name, key in (("A 原规则", "A_metrics"), ("B 混合连续重放", "B_hybrid_continuous_metrics")):
        m = result[key]; win = "不适用" if m["win_rate"] is None else f"{m['win_rate']:.2%}"
        lines.append(f"| {name} | {m['trades']} | {m['long_trades']}/{m['short_trades']} | {win} | {m['net_pnl']:.8f} | {m['fees_usdt']:.8f} | {m['funding_cashflow_usdt']:.8f} | {m['slippage_usdt']:.8f} | {m['max_drawdown']:.4%} |")
    difference = result["B_hybrid_continuous_metrics"]["net_pnl"]-result["A_metrics"]["net_pnl"]
    lines += ["", f"同假设重放的净损益差 B−A：{difference:.8f} USDT。仅是本开发区间混合政策的情景差异，不证明 Agent 因果优势或未来盈利能力。",
        f"B 新出现的未研究成交：{len(result['B_new_unstudied_trades'])} 笔；它们不是 Agent 新发现的机会。",
        f"B 未成交的原机会：{len(result['B_original_opportunities_not_filled'])} 笔；除了拒绝，还可能有组合路径产生的阻断，需对照 B_events.csv。",
        "最大回撤采用小时交易收盘权益，不是实盘标记保证金或柱内最坏回撤。滑点已进入成交价，不重复扣除。", "",
        "## 逐笔研究理由及成本", ""]
    by_id = {r["candidate_id"]: r for r in result["cases"]}
    for case in cases:
        row = by_id[case["candidate_id"]]
        lines += ["### "+row["cutoff_utc"], "", row["rationale"] or "没有有效 Agent 决定，不能计为成功拒绝。", "",
                  f"输入/输出 tokens：{row['input_tokens']}/{row['output_tokens']}；调用 {row['reported_calls']} 次；耗时 {row['elapsed_seconds']} 秒。", ""]
        directory = root/"native_reports"/case["candidate_id"]
        if case["status"] == "COMPLETED" and not directory.exists():
            calls = case["calls"]
            state = {"trade_date": row["cutoff_utc"][:10], "market_report": text(calls[0]),
                "investment_debate_state": {"bull_history": text(calls[1]), "bear_history": text(calls[2])},
                "investment_plan": text(calls[3]), "trader_investment_plan": text(calls[4]),
                "risk_debate_state": {"aggressive_history": text(calls[5]), "conservative_history": text(calls[6]), "neutral_history": text(calls[7])},
                "final_trade_decision": "D.4.1 fixed historical candidate filter only; no execution authorization.\n\n"+text(calls[8]),
                "final_rating": "REVIEW", "llm_status": "AVAILABLE", "workflow_status": "COMPLETED",
                "execution_enabled": False, "allow_new_risk": False,
                "memory_note": "D.4.1 Memory disabled; validated historical assumptions only. Legacy REVIEW is not the independent ACCEPT/REJECT/ABSTAIN choice."}
            write_report_tree(state, "BTC-USDT-SWAP (D.4.1 historical filter)", directory,
                settings={**protocol["model_config"], "version": "0.7.0", "analysts": ["market"],
                          "max_debate_rounds": 1, "max_risk_discuss_rounds": 1}, html=True)
    lines += ["## 总调用成本及局限", "", "```json", json.dumps(result["usage"], indent=2), "```", "",
        "实际账单金额 UNKNOWN，不将 Token 数冒充实际费用。模型服务别名不保证不可变权重版本。", ""]
    failed = [row for row in result["cases"] if row["status"] != "COMPLETED"]
    completed = [row for row in result["cases"] if row["status"] == "COMPLETED"]
    lines += [f"完整研究 {len(completed)}/14，失败 {len(failed)}/14；失败不等于 ABSTAIN 或 REJECT。",
        f"失败样本中原交易亏损 {sum(r['original_net_pnl'] < 0 for r in failed)} 笔、盈利 {sum(r['original_net_pnl'] > 0 for r in failed)} 笔；缺失不是随机的，不能删除这些失败样本后评价筛选收益。",
        f"已完成研究方向：LONG {sum(r['direction'] == 'LONG' for r in completed)}，SHORT {sum(r['direction'] == 'SHORT' for r in completed)}；没有完成的方向不能声称被验证。",
        "失败调用未返回 Token 用量时，其记录值并不等于零消耗；总 Token 仅为已获报告部分，不能认证完整账单。",
        "安全日志仅能确认 APIError / MODEL_SERVICE_ERROR 且 partial_response=True，不能据此武断归因到代理或订阅额度；没有重试部分响应。", ""]
    lines += ["- "+item for item in result["limitations"]]
    lines += ["", "历史手续费为明确假设；资金费实际费率采用原档案，但标记价及整点持仓归属仍是已披露代理。没有杠杆、保证金或强平验证。",
        "本轮不修改参数、不重跑挑选答案、不写 Memory/Reflection、不进入 D.5。进一步评价需要另行预注册更广覆盖与真正未来验证。"]
    target = root/report_name
    if not target.exists():
        with target.open("x", encoding="utf-8") as f: f.write("\n".join(lines)+"\n")
    manifest = root/(Path(report_name).stem+"_export.json")
    if not manifest.exists():
        experiment.immutable(manifest, {"exporter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "analysis_hash": experiment.bt.digest(result), "native_report_count": sum(c["status"] == "COMPLETED" for c in cases),
            "execution_enabled": False})
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, type=Path)
    parser.add_argument("--report-name", default="report.md")
    args = parser.parse_args()
    print(render(args.experiment, args.report_name))

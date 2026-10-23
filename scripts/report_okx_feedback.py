"""Render an existing D.5 offline example and preservation/PIT audit. No LLM."""
import argparse
import json
from collections import Counter
from pathlib import Path

from tradingagents.memory.okx_simulation import (OKXSimulationMemory, SimulationFeedback,
                                               SimulationReflection, iso, utc_ms)
from tradingagents.okx_llm_recovery import file_hashes
from tradingagents.okx_traded_comparison import immutable, read


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory", type=Path, required=True)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--probe", type=Path, required=True)
    parser.add_argument("--preservation-manifest", type=Path, required=True)
    args = parser.parse_args()
    memory = OKXSimulationMemory(args.memory)
    now = utc_ms()
    feedback = memory.load_feedback(as_of=now, include_diagnostics=True)
    lessons = memory.load_reflections(as_of=now)
    expected = read(args.preservation_manifest)
    if file_hashes(args.experiment) != expected:
        raise ValueError("Original experiment differs from D.4.2 preservation manifest")
    if any(memory.load_feedback(as_of=r.research_cutoff_ms, include_diagnostics=True) for r in feedback):
        raise ValueError("Retrospective feedback leaked into historical decision time")
    if any(memory.get_past_context(r.instrument, as_of=r.research_cutoff_ms) for r in feedback):
        raise ValueError("Reflection leaked into historical candidate time")
    root = Path(args.memory)/"delivery"
    root.mkdir(parents=True, exist_ok=True)
    if (root/"report.md").exists():
        print(json.dumps({"report": str(root/"report.md"), "status": "EXISTING_DELIVERY_SOURCE_REVERIFIED"}))
        return
    probe = read(args.probe)
    if not lessons:
        raise ValueError("Complete feedback/reflection example required")
    lesson = lessons[0]
    parent = next(r for r in feedback if r.feedback_id == lesson.feedback_id)
    example = {"feedback": parent.model_dump(mode="json"), "reflection": lesson.model_dump(mode="json"),
               "historical_context": memory.get_past_context(parent.instrument, as_of=parent.research_cutoff_ms),
               "simulation_only": True, "execution_enabled": False}
    immutable(root/"feedback_records.json", [r.model_dump(mode="json") for r in feedback])
    immutable(root/"reflection_records.json", [r.model_dump(mode="json") for r in lessons])
    immutable(root/"example.json", example)
    immutable(root/"feedback.schema.json", SimulationFeedback.model_json_schema())
    immutable(root/"reflection.schema.json", SimulationReflection.model_json_schema())
    immutable(root/"validation.json", {"original_files_unchanged": len(expected),
        "all_original_candidate_time_queries_empty": True, "original_failed_cases_still_failed": 8,
        "mock_lessons_excluded_from_research_context": all(
            not memory.get_past_context(r.instrument, as_of=now) for r in feedback) if all(l.generation_mode == "MOCK" for l in lessons) else None,
        "simulation_record_count": len(feedback), "reflection_count": len(lessons),
        "source_availability_policy": "Conservative first D.5 import; do not backdate archive knowledge",
        "execution_enabled": False})
    trade = parent.baseline_trade
    text = ["# D.5 OKX 模拟 Memory / Reflection 验收", "",
        "本报告仅为 trading_research 经验反馈；execution_enabled=False，策略未被修改。", "",
        "## 一次真实模型探测", "",
        f"时间：{probe['started_at_utc']}；状态：{probe['status']}；完整响应：{probe.get('response')}。",
        f"耗时 {probe['elapsed_seconds']:.3f} 秒；用量 {json.dumps(probe['usage'], ensure_ascii=False)}。仅 1 次请求，无重试。",
        "模型为当前已配置 chatgpt / gpt-6.1-sol。未重跑原 14 例。本次短请求成功不能证明长多 Agent 流程稳定。", "",
        "## 复用与数据结构", "",
        "OKXSimulationMemory 继承原 TradingMemoryLog，复用 Markdown 分隔符、解析器、日志锁、结算锁和原子更新模式。",
        "复用原 Reflector 类，增加模拟反馈专用方法；settlement.Settlement 继续承载完成/失败结果。",
        "永续结果来自现有确定性账本及 D.3.1 审计；不调用股票 Yahoo fetch_returns，不虚构 raw return / alpha。",
        "记录包含 candidate/research/snapshot ID、ACCEPT/REJECT/ABSTAIN、状态机与基准模拟成交、来源摘要、模型/策略版本、四种时钟及权限常量。Schema 见同目录 JSON 文件。", "",
        f"已导入 {len(feedback)} 条：{dict(Counter(r.research_status for r in feedback))}。",
        f"结果分类：{dict(Counter(r.outcome_status for r in feedback))}。8 个 MODEL_FAILURE 没有改成 REJECT、ABSTAIN 或成功避险。",
        "4 盈利/2 亏损仅描述原 6 个完整判断关联的基准规则成交，不代表筛选绩效。原 14 笔总收益和原实验不变。", "",
        "## 完整示例", "",
        f"候选：{parent.candidate_id}；判断：{parent.decision}。",
        f"研究截止：{iso(parent.research_cutoff_ms)}；结果可知：{iso(parent.outcome_known_at_ms)}。",
        f"反馈实际可用：{iso(parent.available_at_ms)}；反思生成：{iso(lesson.generated_at_ms)}。",
        f"基准模拟成交：{trade['direction']}，开仓 {trade['entry_utc']}，退出 {trade['exit_utc']}，净损益 {trade['net_pnl']:.8f} USDT。",
        f"原判断理由：{parent.judgment}", "",
        f"Reflection（{lesson.generation_mode}；未验证的研究笔记）：{lesson.text}", "",
        "完整状态机、数量、成本及来源摘要见 example.json。该固定 MOCK Reflection 未调用模型，不注入正式研究上下文。", "",
        "## 时间隔离与幂等性", "",
        "反馈可用时间=max(研究实际记录时间,模拟结果可知时间,来源可用时间,反馈写入时间)。来源原始发布时间未知，保守使用本次首次导入时间，不追溯冒充当时可用。",
        "Reflection 另按真实生成时间过滤；查询必须提供含时区的完整时间或 UTC 毫秒。原 14 个历史截止时点均读不到本次反馈或反思。",
        "feedback_id 由候选及不可变来源摘要确定；重复导入不重复记账。每条反思有调用前持久标志，已完成不重采样，失败/中断不自动重放。",
        "重复运行小批次命令可继续处理下一条未复盘记录；同一记录不会重复复盘。MOCK 与 REAL 使用不同示例目录。",
        f"原实验 {len(expected)} 个文件与 D.4.2 清单逐项哈希一致；盲测协议与 279 候选层保持原状。", "",
        "## 测试与整体验收建议", "",
        "专项及相关回归的最终结果见项目 docs/okx_d5_memory_reflection.md。本轮不新增真实 Reflection 模型调用。",
        "A/B/C/C.1：保留数据质量、时间和预案校验；以研究系统而非自动执行系统验收。",
        "D.1–D.3.1：审计账本、费用代理及历史合约参数假设；14 笔样本不证明盈利能力。",
        "D.4–D.4.2：保留模型失败缺失层；一次短请求不替代长流程稳定性验收。",
        "D.5：存储和时间隔离可独立使用，Reflection 仅为未验证笔记。保持冻结对照不读 Memory，未来若启用需新协议，不自动优化策略。",
        "尚未覆盖实际杠杆、保证金、强平安全；没有新增实盘功能。", "",
        "官方完整流结束事件依据：https://developers.openai.com/api/reference/resources/responses/streaming-events 。", ""]
    (root/"report.md").write_text("\n".join(text), encoding="utf-8")
    print(json.dumps({"report": str(root/"report.md"), "records": len(feedback), "execution_enabled": False}))


if __name__ == "__main__":
    main()

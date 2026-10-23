"""Offline D.4.2 audit and real-source checkpoint reconstruction; no LLM calls."""
import argparse
import json
from pathlib import Path

from tradingagents import okx_llm_recovery as recovery
from tradingagents import okx_traded_comparison as original
from tradingagents.llm_clients.recovery import RecoveryController
from tradingagents.llm_clients.reliability import LLMUnavailable


class OfflineSentinel:
    """Stops at the first uncached role, never returns a synthetic decision."""
    def invoke(self, prompt):
        raise LLMUnavailable("OFFLINE_TEST_SENTINEL")
    def with_structured_output(self, schema, **kwargs):
        return self


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root, output = args.experiment.resolve(), args.output.resolve()
    if output == root or root in output.parents or output in root.parents:
        raise ValueError("Separate output required")
    before = recovery.file_hashes(root)
    diagnosis = recovery.audit(root)
    output.mkdir(parents=True, exist_ok=True)
    original.immutable(output/"diagnosis.json", diagnosis)
    validation_file = output/"checkpoint_validation.json"
    if not validation_file.exists():
        rows = []
        for case in diagnosis["cases"]:
            if not case["failures"]:
                continue
            model = OfflineSentinel()
            result = recovery.recover(root, output/"mock_checkpoints", case["candidate_id"],
                backend_factory=lambda _: (model, model), restart_incomplete=True,
                controller=RecoveryController(min_interval=0, sleep=lambda _: None))
            run = result["result"]
            expected = case["completed_roles"]
            if run["nodes"] != expected or run.get("failure", {}).get("kind") != "OFFLINE_TEST_SENTINEL":
                raise ValueError("Real-source checkpoint reconstruction failed")
            rows.append({"candidate_id": case["candidate_id"], "validated_complete_roles": expected,
                         "stopped_at_first_uncached_role": True, "mock_attempts": result["new_attempt_count"],
                         "real_model_calls": 0, "decision": None, "execution_enabled": False})
        original.immutable(validation_file, {"scope": "MOCK_ONLY_NO_RECOVERED_DECISIONS", "cases": rows,
                           "real_model_calls": 0, "execution_enabled": False})
    validation = original.read(validation_file)
    if before != recovery.file_hashes(root):
        raise ValueError("Original experiment changed")
    original.immutable(output/"original_artifact_hashes.json", before)
    reused = sum(len(r["validated_complete_roles"]) for r in validation["cases"])
    lines = ["# D.4.2 LLM 服务诊断与安全恢复", "",
        "范围：探索性工程验证；不属于原 D.4.1 冻结实验。execution_enabled=False。", "",
        "## 已证实的事实", "",
        f"原实验 14 例，6 例完成，8 例失败。8 例均为 APIError / MODEL_SERVICE_ERROR，partial_response=True。",
        "原始 calls.json 与封存 result.json 完全一致。HTTP 状态、服务错误码、请求标识未被旧分类器保留；无法追溯具体网络、额度或服务原因。",
        "APIError 本身不证明网络故障。安装的 SDK 也会将 SSE error 对象转换为 APIError；旧分类器随后丢失其 body 中的结构化 code/type。", "",
        "| 失败案例前缀 | 已完成角色数 | 失败角色 | 原因结论 |", "|---|---:|---|---|"]
    for row in diagnosis["cases"]:
        if row["failures"]:
            lines.append(f"| {row['candidate_id'][:12]} | {len(row['completed_roles'])} | {row['failed_role']} | 服务细因未知；禁止自动重放部分响应 |")
    lines += ["", "## 客户端与修复", "",
        "原客户端：流式 Responses、120 秒超时、SDK 重试 0、原案例并发 2。显式模型代理使用匹配 SDK 的 httpx2 客户端、TLS 验证开启、trust_env=False；模型代理未配置时使用原有环境处理。",
        "SDK 默认连接池上限 1000、保活 100；两个并发案例本身不足以证明连接池耗尽。保持连接复用，不更换依赖或网络配置。120 秒为传输超时，不是严格的整个流总时限。",
        "新恢复：单请求串行、最小间隔 2 秒、每角色生命周期最多 3 次尝试、指数退避加抖动、单次等待最多 30 秒；更长 Retry-After 暂停而非提前请求。",
        "仅重试连接建立失败、明确瞬时限流、无部分响应的明确暂时服务错误。未知 429、读取超时、连接读写中断、部分流、上下文限制、格式错误均不自动重试。额度/权限等终止错误立即停止并持久化 campaign_stop。",
        "调用前落盘 STARTED；异常终止后禁止自动重放该调用。完整响应通过 Schema 后才保存角色检查点。原角色提示词、tier、Schema、模型配置及源数据/代码指纹必须一致；不继续部分文本。",
        "所有失败 usage 保留 null（未知费用），完整调用记录实际返回 usage；缓存复用与新增尝试分开记录。", "",
        "## 离线真实源检查点验证", "",
        f"8 个原失败案例全部通过安全前缀重建，共验证 {reused} 个完整角色（7+6）；首个未完成角色由模拟哨兵阻断，未生成新 ACCEPT/REJECT/ABSTAIN。真实模型调用 0。",
        "单元测试另外验证完整九角色恢复及缓存复用。真实源检查点记录见 checkpoint_validation.json；专项/回归结果见项目 docs/okx_d4_2_llm_recovery.md。",
        f"原实验 {len(before)} 个文件逐项 SHA-256 核对未变化；未关联交易后续结果，也未改写 A/B 净收益。", "",
        "## 剩余限制与下一步", "",
        "安全恢复机制已通过模拟验证，但未证明当前真实服务稳定；8 例仍然失败，6 个原 ACCEPT 保留。未将故障或弃权记作成功过滤。",
        "如要实际恢复，应先做一次低成本服务诊断，再显式为单个案例开启新的完整角色生成；与原实验分开保存。不能凭本轮模拟测试宣称已解决远端服务故障。",
        "冻结历史协议、费用及未来盲测范围保持不变；未进入 D.5。", "",
        "官方依据：https://developers.openai.com/api/docs/guides/error-codes （区分瞬时限流、额度及认证错误；API 规则并不能证明本次订阅服务错误原因）。", ""]
    report = output/"report.md"
    if not report.exists():
        report.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"report": str(report), "failed_cases_validated": len(validation["cases"]),
                      "complete_roles_reused": reused, "real_model_calls": 0, "execution_enabled": False}))


if __name__ == "__main__":
    main()
